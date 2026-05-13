import os
import re
import json
import time
import asyncio
import aiohttp
import discord
from discord.ext import commands
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

TOKEN = os.getenv("DISCORD_TOKEN")
UW_API_KEY = os.getenv("UW_API_KEY")
UW_API_BASE = "https://api.unusualwhales.com"
DELETE_CONFIRM_TTL = 30  # second
# For Railway persistence: change to "/data/bot_data.json" after adding volume
DATA_FILE = "bot_data.json"
CONTRACT_MULTIPLIER = 100  # Options are worth $100 per contract

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Qty optional; defaults to 1 if omitted
pattern = re.compile(
    r'(?i)^\s*(bto|sto|stc|btc)\s+(?:(\d+)\s+)?([A-Z]+)\s+(\d+(?:\.\d+)?[cp])\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*@\s*\$?\s*(m|\d+(?:\.\d+)?)\s*$'
)

positions = defaultdict(list)
trade_stats = defaultdict(lambda: {
    "opened_qty": 0,
    "closed_qty": 0
})
user_stats = defaultdict(lambda: {
    "realized_pnl": Decimal('0.00'),
    "opened_qty": 0,
    "closed_qty": 0,
    "winning_closes": 0,
    "losing_closes": 0,
    "flat_closes": 0
})
trade_history = defaultdict(list)
pending_deletes = {}


def clean_price_text(price_text: str) -> str:
    """Remove whitespace and currency symbols from price text."""
    return price_text.strip().lower().replace("$", "")


def parse_price(price_text: str):
    """Parse price text to Decimal or None for market price."""
    price_text = clean_price_text(price_text)
    if price_text == "m":
        return None
    try:
        return Decimal(price_text).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except:
        return None



def is_market_price(price_text: str) -> bool:
    """Return True when user typed @m / market."""
    return clean_price_text(price_text) == "m"


def parse_expiry_to_iso(expiry: str) -> str:
    """Convert M/D, M/D/YY, or M/D/YYYY into YYYY-MM-DD for UW API."""
    raw = str(expiry or "").strip()
    parts = raw.split("/")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid expiry format")

    month = int(parts[0])
    day = int(parts[1])
    today = datetime.utcnow().date()

    if len(parts) == 2:
        year = today.year
        candidate = datetime(year, month, day).date()
        if candidate < today:
            candidate = datetime(year + 1, month, day).date()
        return candidate.isoformat()

    year_text = parts[2]
    year = int(year_text)
    if year < 100:
        year += 2000

    return datetime(year, month, day).date().isoformat()


def _option_side_from_strike(strike_text: str) -> tuple[Decimal, str]:
    """Parse 110c / 110p into (Decimal('110'), 'call'/'put')."""
    raw = str(strike_text or "").strip().lower()
    side_char = raw[-1]
    strike_num = Decimal(raw[:-1])
    side = "call" if side_char == "c" else "put"
    return strike_num, side


def _to_decimal_or_none(value):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", "").strip())
        if d <= 0:
            return None
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def _row_option_type(row: dict) -> str:
    value = str(
        row.get("option_type")
        or row.get("type")
        or row.get("call_put")
        or row.get("side")
        or ""
    ).lower()
    if value in ("c", "call", "calls"):
        return "call"
    if value in ("p", "put", "puts"):
        return "put"

    symbol = str(
        row.get("option_symbol")
        or row.get("option_chain")
        or row.get("option_chain_id")
        or row.get("chain")
        or ""
    ).upper()
    # OCC tail usually includes C/P before strike digits.
    m = re.search(r"\d{6}([CP])\d{8}$", symbol)
    if m:
        return "call" if m.group(1) == "C" else "put"
    return ""


def _row_strike(row: dict) -> Decimal | None:
    strike = _to_decimal_or_none(row.get("strike"))
    if strike is not None:
        return strike

    symbol = str(
        row.get("option_symbol")
        or row.get("option_chain")
        or row.get("option_chain_id")
        or row.get("chain")
        or ""
    ).upper()
    m = re.search(r"\d{6}[CP](\d{8})$", symbol)
    if not m:
        return None
    try:
        return (Decimal(int(m.group(1))) / Decimal("1000")).quantize(Decimal("0.001"))
    except Exception:
        return None


def _pick_contract_price(row: dict, action: str) -> tuple[Decimal | None, str]:
    """Pick a practical market price from UW contract row.

    For closes:
      STC = sell to close, prefer bid.
      BTC = buy to close, prefer ask.
    For opens:
      BTO = buy to open, prefer ask.
      STO = sell to open, prefer bid.
    Then fallback to mid/mark/last-style fields.
    """
    action = action.upper()
    bid_keys = ("bid", "bid_price", "nbbo_bid", "best_bid")
    ask_keys = ("ask", "ask_price", "nbbo_ask", "best_ask")
    mid_keys = ("mid", "mark", "mark_price", "mid_price")
    last_keys = ("last", "last_price", "price", "close", "close_price")

    def first(keys):
        for key in keys:
            d = _to_decimal_or_none(row.get(key))
            if d is not None:
                return d, key
        return None, ""

    if action in ("STC", "STO"):
        d, key = first(bid_keys)
        if d is not None:
            return d, key
    elif action in ("BTC", "BTO"):
        d, key = first(ask_keys)
        if d is not None:
            return d, key

    # Fallback: calculate midpoint from bid/ask if both exist.
    bid, _ = first(bid_keys)
    ask, _ = first(ask_keys)
    if bid is not None and ask is not None:
        return ((bid + ask) / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "mid(bid/ask)"

    for keys in (mid_keys, last_keys, bid_keys, ask_keys):
        d, key = first(keys)
        if d is not None:
            return d, key

    return None, ""


async def fetch_uw_market_option_price(action: str, ticker: str, strike: str, expiry: str) -> tuple[Decimal, str]:
    """Fetch a market option price from UW option contracts for @m.

    Returns (price, source_field). Raises RuntimeError if price cannot be found.
    """
    if not UW_API_KEY:
        raise RuntimeError("UW_API_KEY is not set. Add it in Railway or enter a manual price.")

    expiry_iso = parse_expiry_to_iso(expiry)
    target_strike, target_side = _option_side_from_strike(strike)

    url = f"{UW_API_BASE}/api/stock/{ticker.upper()}/option-contracts"
    headers = {"Authorization": f"Bearer {UW_API_KEY}"}
    params = {"expiry": expiry_iso, "limit": "500"}

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"UW option-contracts returned {resp.status}: {body[:160]}")
            payload = await resp.json()

    rows = list(payload.get("data") or [])
    if not rows:
        raise RuntimeError(f"No UW option contracts found for {ticker.upper()} {expiry_iso}.")

    matched = []
    for row in rows:
        row_strike = _row_strike(row)
        row_side = _row_option_type(row)
        if row_strike is None or not row_side:
            continue
        if row_side == target_side and row_strike == target_strike:
            matched.append(row)

    if not matched:
        raise RuntimeError(f"No matching UW contract found for {ticker.upper()} {strike.upper()} {expiry_iso}.")

    for row in matched:
        price, source = _pick_contract_price(row, action)
        if price is not None:
            return price, source

    raise RuntimeError(f"UW contract found, but no usable bid/ask/mark/last price for {ticker.upper()} {strike.upper()} {expiry_iso}.")

def fmt_money(value) -> str:
    """Format Decimal value as money string."""
    if isinstance(value, float):
        value = Decimal(str(value))
    sign = "+" if value > 0 else ""
    return f"{sign}${value:,.2f}"


def fmt_pct(value) -> str:
    """Format percentage value."""
    if isinstance(value, Decimal):
        value = float(value)
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def fmt_price_text(price_text: str) -> str:
    """Format price for display in embeds."""
    price_text = clean_price_text(price_text)
    if price_text == "m":
        return "Market Price"
    return f"${price_text}"


def fmt_price_short(price_text: str) -> str:
    """Format price for compact display."""
    price_text = clean_price_text(price_text)
    if price_text == "m":
        return "MKT"
    return f"${price_text}"


def get_color(action: str) -> discord.Color:
    """Get embed color based on action (green for buys, red for sells)."""
    action = action.upper()
    if action in ["BTO", "BTC"]:
        return discord.Color.green()
    return discord.Color.red()


def looks_like_trade_command(message_text: str) -> bool:
    """Check if message looks like a trade command."""
    text = message_text.strip().lower()
    return text.startswith(("bto", "stc", "sto", "btc"))


def get_format_help_message(message_text: str) -> str:
    """Generate helpful error message for invalid trade format."""
    text = message_text.strip().lower()
    parts = text.split()

    if not parts:
        return (
            "❌ Invalid trade format.\n"
            "Examples:\n"
            "`bto spx 7130c 5/15 @m`\n"
            "`bto 5 spx 7130c 5/15 @ 1.25`\n"
            "Use: `[action] [qty optional] [ticker] [strike][c/p] [expiry] @ [price]`"
        )

    if parts[0] not in ["bto", "stc", "sto", "btc"]:
        return "❌ Invalid action. Use one of: `bto`, `stc`, `sto`, `btc`"

    if len(parts) < 5:
        return (
            "❌ Invalid trade format.\n"
            "Examples:\n"
            "`bto spx 7130c 5/15 @m`\n"
            "`bto 5 spx 7130c 5/15 @ 1.25`\n"
            "Use: `[action] [qty optional] [ticker] [strike][c/p] [expiry] @ [price]`"
        )

    idx = 1
    if len(parts) > 1 and parts[1].isdigit():
        idx = 2

    if len(parts) <= idx + 2:
        return (
            "❌ Missing fields.\n"
            "Examples:\n"
            "`bto spx 7130c 5/15 @m`\n"
            "`bto 5 spx 7130c 5/15 @ 1.25`"
        )

    strike = parts[idx + 1]
    if not re.match(r'^\d+(?:\.\d+)?[cp]$', strike, re.IGNORECASE):
        return "❌ Invalid strike format. Use strike like `7130c` or `7130p`."

    expiry = parts[idx + 2]
    if not re.match(r'^\d{1,2}/\d{1,2}$', expiry):
        return "❌ Invalid expiry format. Use date like `5/15`, `04/17`, `01/15/28`, or `01/15/2028`."

    return (
        "❌ Invalid trade format.\n"
        "Examples:\n"
        "`bto spx 7130c 5/15 @m`\n"
        "`bto 5 spx 7130c 5/15 @ 1.25`\n"
        "You can use `@m` for market price."
    )


def contract_key(user_id, ticker, strike, expiry, side):
    """Create a unique key for a contract."""
    return (user_id, ticker, strike, expiry, side)


def contract_key_to_str(key):
    """Convert contract key tuple to string for JSON serialization."""
    user_id, ticker, strike, expiry, side = key
    return f"{user_id}|{ticker}|{strike}|{expiry}|{side}"


def str_to_contract_key(key_str):
    """Convert string back to contract key tuple."""
    user_id, ticker, strike, expiry, side = key_str.split("|", 4)
    return (int(user_id), ticker, strike, expiry, side)


def save_data():
    """Atomically save all data to JSON file using temp file + replace."""
    data = {
        "positions": {},
        "trade_stats": {},
        "user_stats": {},
        "trade_history": {},
        "pending_deletes": {}
    }

    # Convert positions, preserving Decimal values as strings
    for key, lots in positions.items():
        key_str = contract_key_to_str(key)
        data["positions"][key_str] = [
            {
                "qty": lot["qty"],
                "entry_price": str(lot["entry_price"]) if lot["entry_price"] is not None else None
            }
            for lot in lots
        ]

    # Convert trade_stats
    for key, value in trade_stats.items():
        data["trade_stats"][contract_key_to_str(key)] = value

    # Convert user_stats, converting Decimal to string
    for user_id, value in user_stats.items():
        data["user_stats"][str(user_id)] = {
            "realized_pnl": str(value["realized_pnl"]),
            "opened_qty": value["opened_qty"],
            "closed_qty": value["closed_qty"],
            "winning_closes": value["winning_closes"],
            "losing_closes": value["losing_closes"],
            "flat_closes": value["flat_closes"]
        }

    # Convert trade_history, converting Decimal to string
    for user_id, history in trade_history.items():
        data["trade_history"][str(user_id)] = [
            {
                "timestamp": trade["timestamp"],
                "action": trade["action"],
                "qty": trade["qty"],
                "ticker": trade["ticker"],
                "strike": trade["strike"],
                "expiry": trade["expiry"],
                "price_text": trade["price_text"],
                "total_pnl": str(trade["total_pnl"]) if trade["total_pnl"] is not None else None,
                "pnl_pct": float(trade["pnl_pct"]) if trade["pnl_pct"] is not None else None
            }
            for trade in history
        ]

    # Convert pending_deletes only
    for user_id, value in pending_deletes.items():
        data["pending_deletes"][str(user_id)] = value

    # Write atomically to temp file, then replace
    temp_file = f"{DATA_FILE}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        print(f"Error saving data: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)


def load_data():
    """Load data from JSON file, converting strings back to Decimal."""
    global positions, trade_stats, user_stats, trade_history, pending_deletes

    if not os.path.exists(DATA_FILE):
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error loading data: {e}")
        return

    loaded_positions = defaultdict(list)
    for key_str, lots in data.get("positions", {}).items():
        loaded_lots = []
        for lot in lots:
            loaded_lots.append({
                "qty": lot["qty"],
                "entry_price": Decimal(lot["entry_price"]) if lot["entry_price"] is not None else None
            })
        loaded_positions[str_to_contract_key(key_str)] = loaded_lots
    positions = loaded_positions

    loaded_trade_stats = defaultdict(lambda: {
        "opened_qty": 0,
        "closed_qty": 0
    })
    for key_str, value in data.get("trade_stats", {}).items():
        loaded_trade_stats[str_to_contract_key(key_str)] = value
    trade_stats = loaded_trade_stats

    loaded_user_stats = defaultdict(lambda: {
        "realized_pnl": Decimal('0.00'),
        "opened_qty": 0,
        "closed_qty": 0,
        "winning_closes": 0,
        "losing_closes": 0,
        "flat_closes": 0
    })
    for user_id_str, value in data.get("user_stats", {}).items():
        loaded_user_stats[int(user_id_str)] = {
            "realized_pnl": Decimal(value["realized_pnl"]),
            "opened_qty": value["opened_qty"],
            "closed_qty": value["closed_qty"],
            "winning_closes": value["winning_closes"],
            "losing_closes": value["losing_closes"],
            "flat_closes": value["flat_closes"]
        }
    user_stats = loaded_user_stats

    loaded_trade_history = defaultdict(list)
    for user_id_str, history in data.get("trade_history", {}).items():
        loaded_trades = []
        for trade in history:
            loaded_trades.append({
                "timestamp": trade["timestamp"],
                "action": trade["action"],
                "qty": trade["qty"],
                "ticker": trade["ticker"],
                "strike": trade["strike"],
                "expiry": trade["expiry"],
                "price_text": trade["price_text"],
                "total_pnl": Decimal(trade["total_pnl"]) if trade["total_pnl"] is not None else None,
                "pnl_pct": Decimal(str(trade["pnl_pct"])) if trade["pnl_pct"] is not None else None
            })
        loaded_trade_history[int(user_id_str)] = loaded_trades
    trade_history = loaded_trade_history

    pending_deletes = {
        int(user_id_str): value
        for user_id_str, value in data.get("pending_deletes", {}).items()
    }


def log_trade(
    user_id,
    action,
    qty,
    ticker,
    strike,
    expiry,
    price_text,
    total_pnl=None,
    pnl_pct=None
):
    """Log a trade to history."""
    trade_history[user_id].append({
        "timestamp": datetime.utcnow().isoformat(),
        "action": action.upper(),
        "qty": qty,
        "ticker": ticker.upper(),
        "strike": strike.upper(),
        "expiry": expiry,
        "price_text": clean_price_text(price_text),
        "total_pnl": total_pnl,
        "pnl_pct": pnl_pct
    })


def add_open_position(user_id, ticker, strike, expiry, side, qty, entry_price):
    """Add an open position (BTO/STO)."""
    key = contract_key(user_id, ticker, strike, expiry, side)
    positions[key].append({
        "qty": qty,
        "entry_price": entry_price  # Will be Decimal or None
    })
    trade_stats[key]["opened_qty"] += qty
    user_stats[user_id]["opened_qty"] += qty


def get_remaining_entry_price(opens):
    """Calculate weighted average entry price for remaining open positions."""
    if not opens:
        return None

    numeric_lots = [lot for lot in opens if lot["entry_price"] is not None]
    total_qty = sum(lot["qty"] for lot in numeric_lots)

    if total_qty == 0:
        return None

    weighted_total = sum(lot["qty"] * lot["entry_price"] for lot in numeric_lots)
    return (weighted_total / total_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def close_position(user_id, ticker, strike, expiry, side, close_qty, exit_price):
    """Close part or all of an open position using FIFO matching."""
    key = contract_key(user_id, ticker, strike, expiry, side)
    opens = positions.get(key, [])

    previous_closed_qty = trade_stats[key]["closed_qty"]
    total_opened_overall = trade_stats[key]["opened_qty"]

    if not opens:
        return {
            "matched_qty": 0,
            "total_open_before_close": 0,
            "remaining_after_close": 0,
            "avg_entry_price": None,
            "avg_exit_price": exit_price,
            "remaining_entry_price": None,
            "total_pnl": None,
            "pnl_pct": None,
            "fully_closed": False,
            "partial_close": False,
            "previous_closed_qty": previous_closed_qty,
            "total_closed_overall": previous_closed_qty,
            "total_opened_overall": total_opened_overall,
            "error_message": "No open position found."
        }

    total_open_before_close = sum(lot["qty"] for lot in opens)
    remaining_to_close = close_qty

    matched_qty = 0
    total_entry_value = Decimal('0.00')
    total_pnl = Decimal('0.00')
    pnl_available = True

    i = 0
    while i < len(opens) and remaining_to_close > 0:
        lot = opens[i]
        lot_qty = lot["qty"]
        lot_entry = lot["entry_price"]

        qty_used = min(lot_qty, remaining_to_close)

        matched_qty += qty_used
        remaining_to_close -= qty_used

        if lot_entry is None or exit_price is None:
            pnl_available = False
        else:
            # Convert qty_used to Decimal for calculation
            qty_decimal = Decimal(qty_used)
            total_entry_value += lot_entry * qty_decimal

            if side == "LONG":
                total_pnl += (exit_price - lot_entry) * CONTRACT_MULTIPLIER * qty_decimal
            else:
                total_pnl += (lot_entry - exit_price) * CONTRACT_MULTIPLIER * qty_decimal

        if qty_used == lot_qty:
            opens.pop(i)
        else:
            opens[i]["qty"] -= qty_used
            i += 1

    remaining_after_close = sum(lot["qty"] for lot in opens)
    remaining_entry_price = get_remaining_entry_price(opens)

    if opens:
        positions[key] = opens
    elif key in positions:
        del positions[key]

    avg_entry_price = None
    avg_exit_price = None
    pnl_pct = None

    if matched_qty > 0:
        trade_stats[key]["closed_qty"] += matched_qty
        user_stats[user_id]["closed_qty"] += matched_qty

        if total_entry_value > 0:
            qty_decimal = Decimal(matched_qty)
            avg_entry_price = (total_entry_value / qty_decimal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if exit_price is not None:
            avg_exit_price = exit_price

    total_closed_overall = trade_stats[key]["closed_qty"]

    if pnl_available and matched_qty > 0 and total_entry_value > 0:
        # FIX: total_pnl is in dollars (already × CONTRACT_MULTIPLIER), but
        # total_entry_value is per-contract price × qty. Multiply the denominator
        # by CONTRACT_MULTIPLIER so the units match before computing the percent.
        pnl_pct = (
            total_pnl / (total_entry_value * CONTRACT_MULTIPLIER) * Decimal('100')
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        user_stats[user_id]["realized_pnl"] += total_pnl

        if total_pnl > 0:
            user_stats[user_id]["winning_closes"] += 1
        elif total_pnl < 0:
            user_stats[user_id]["losing_closes"] += 1
        else:
            user_stats[user_id]["flat_closes"] += 1
    else:
        total_pnl = None

    error_message = None
    if matched_qty == 0:
        error_message = "No matching quantity found."
    elif matched_qty < close_qty:
        error_message = f"Only matched {matched_qty} of {close_qty} contracts."

    return {
        "matched_qty": matched_qty,
        "total_open_before_close": total_open_before_close,
        "remaining_after_close": remaining_after_close,
        "avg_entry_price": avg_entry_price,
        "avg_exit_price": avg_exit_price,
        "remaining_entry_price": remaining_entry_price,
        "total_pnl": total_pnl,
        "pnl_pct": pnl_pct,
        "fully_closed": remaining_after_close == 0 and matched_qty > 0,
        "partial_close": remaining_after_close > 0 and matched_qty > 0,
        "previous_closed_qty": previous_closed_qty,
        "total_closed_overall": total_closed_overall,
        "total_opened_overall": total_opened_overall,
        "error_message": error_message
    }


def summarize_user_positions(user_id):
    """Get all open positions for a user."""
    summary = []

    for key, lots in positions.items():
        pos_user_id, ticker, strike, expiry, side = key

        if pos_user_id != user_id:
            continue

        total_qty = sum(lot["qty"] for lot in lots)

        numeric_lots = [lot for lot in lots if lot["entry_price"] is not None]
        if numeric_lots:
            total_cost = sum(lot["qty"] * lot["entry_price"] for lot in numeric_lots)
            total_numeric_qty = sum(lot["qty"] for lot in numeric_lots)
            avg_entry = (total_cost / total_numeric_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if total_numeric_qty > 0 else None
        else:
            avg_entry = None

        summary.append({
            "ticker": ticker,
            "strike": strike,
            "expiry": expiry,
            "side": side,
            "qty": total_qty,
            "avg_entry": avg_entry
        })

    summary.sort(key=lambda x: (x["ticker"], x["expiry"], x["strike"], x["side"]))
    return summary


def find_user_ticker_positions(user_id, ticker):
    """Find all positions for a user and ticker."""
    ticker = ticker.upper()
    matches = []

    for key, lots in positions.items():
        pos_user_id, pos_ticker, strike, expiry, side = key
        if pos_user_id == user_id and pos_ticker == ticker:
            matches.append((key, lots))

    return matches


@bot.command(name="current")
async def current_command(ctx, member: discord.Member = None):
    target = member or ctx.author
    user_positions = summarize_user_positions(target.id)

    if not user_positions:
        embed = discord.Embed(
            title="Current Open Trades",
            description="No open positions found.",
            color=discord.Color.blurple()
        )
        embed.set_author(
            name=target.display_name,
            icon_url=target.display_avatar.url
        )
        embed.set_footer(text="Core Entry Alerts")
        await ctx.send(embed=embed)
        return

    lines = []
    for pos in user_positions:
        avg_entry_text = f"${pos['avg_entry']}" if pos["avg_entry"] is not None else "Market/Unknown"
        lines.append(
            f"**{pos['ticker']} {pos['strike']} {pos['expiry']}** | {pos['side']} | Qty {pos['qty']} | Avg Entry {avg_entry_text}"
        )

    embed = discord.Embed(
        title="Current Open Trades",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    embed.set_author(
        name=target.display_name,
        icon_url=target.display_avatar.url
    )
    embed.set_footer(text="Core Entry Alerts")
    await ctx.send(embed=embed)


@bot.command(name="stats")
async def stats_command(ctx, member: discord.Member = None):
    target = member or ctx.author
    stats = user_stats[target.id]

    total_decided = stats["winning_closes"] + stats["losing_closes"]
    win_rate = (stats["winning_closes"] / total_decided * 100) if total_decided > 0 else 0.0

    description = (
        f"**Realized P/L:** {fmt_money(stats['realized_pnl'])}\n"
        f"**Contracts Opened:** {stats['opened_qty']}\n"
        f"**Contracts Closed:** {stats['closed_qty']}\n"
        f"**Winning Closes:** {stats['winning_closes']}\n"
        f"**Losing Closes:** {stats['losing_closes']}\n"
        f"**Flat Closes:** {stats['flat_closes']}\n"
        f"**Win Rate:** {win_rate:.1f}%"
    )

    embed = discord.Embed(
        title="Trader Stats",
        description=description,
        color=discord.Color.gold()
    )
    embed.set_author(
        name=target.display_name,
        icon_url=target.display_avatar.url
    )
    embed.set_footer(text="Core Entry Alerts")
    await ctx.send(embed=embed)


@bot.command(name="profit")
async def profit_command(ctx, member: discord.Member = None):
    target = member or ctx.author
    stats = user_stats[target.id]

    winning = stats["winning_closes"]
    losing = stats["losing_closes"]
    flat = stats["flat_closes"]
    total_pnl = stats["realized_pnl"]

    decided = winning + losing
    win_rate = (winning / decided * 100) if decided > 0 else 0.0

    embed = discord.Embed(
        title="Profit Summary",
        description=(
            f"**All-Time Realized P/L:** {fmt_money(total_pnl)}\n"
            f"**Winning Closes:** {winning}\n"
            f"**Losing Closes:** {losing}\n"
            f"**Flat Closes:** {flat}\n"
            f"**Win Rate:** {win_rate:.1f}%"
        ),
        color=discord.Color.green() if total_pnl >= 0 else discord.Color.red()
    )

    embed.set_author(
        name=target.display_name,
        icon_url=target.display_avatar.url
    )
    embed.set_footer(text="Core Entry Alerts")

    await ctx.send(embed=embed)


@bot.command(name="daily")
async def daily_command(ctx, member: discord.Member = None):
    target = member or ctx.author
    history = trade_history[target.id]

    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_trades = [t for t in history if t["timestamp"].startswith(today)]

    if not today_trades:
        embed = discord.Embed(
            title="Today's P/L",
            description="No trades today.",
            color=discord.Color.blurple()
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.set_footer(text="Core Entry Alerts")
        await ctx.send(embed=embed)
        return

    daily_pnl = Decimal('0.00')
    winning = losing = flat = opens_count = closes_count = 0
    trade_lines = []

    for trade in today_trades:
        if trade["action"] in ["BTO", "STO"]:
            opens_count += 1
        else:
            closes_count += 1
            if trade["total_pnl"] is not None:
                daily_pnl += trade["total_pnl"]
                if trade["total_pnl"] > 0:
                    winning += 1
                elif trade["total_pnl"] < 0:
                    losing += 1
                else:
                    flat += 1

        price_display = fmt_price_short(trade["price_text"])
        line = f"{trade['action']} {trade['qty']} {trade['ticker']} {trade['strike']} {trade['expiry']} @ {price_display}"
        if trade.get("total_pnl") is not None and trade.get("pnl_pct") is not None:
            line += f" | {fmt_money(trade['total_pnl'])} | {fmt_pct(trade['pnl_pct'])}"
        trade_lines.append(line)

    decided = winning + losing
    win_rate = (winning / decided * 100) if decided > 0 else 0.0

    summary = (
        f"**Today's P/L:** {fmt_money(daily_pnl)}\n"
        f"**Trades:** {len(today_trades)} ({opens_count} opens, {closes_count} closes)\n"
        f"**Winning:** {winning} | **Losing:** {losing} | **Flat:** {flat}\n"
        f"**Win Rate:** {win_rate:.1f}%"
    )

    trades_block = "\n".join(trade_lines)
    description = f"{summary}\n\n**Trades:**\n{trades_block}"

    # Discord embed description hard cap is 4096 chars
    if len(description) > 4000:
        description = description[:4000] + "\n…(truncated)"

    if daily_pnl > 0:
        color = discord.Color.green()
    elif daily_pnl < 0:
        color = discord.Color.red()
    else:
        color = discord.Color.blurple()

    embed = discord.Embed(
        title="Today's P/L",
        description=description,
        color=color
    )
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.set_footer(text="Core Entry Alerts")
    await ctx.send(embed=embed)


@bot.command(name="history", aliases=["trades"])
async def history_command(ctx, member: discord.Member = None):
    target = member or ctx.author
    history = trade_history[target.id]

    if not history:
        await ctx.send("No trades found.")
        return

    cutoff = datetime.utcnow() - timedelta(days=30)

    recent = [
        trade for trade in history
        if datetime.fromisoformat(trade["timestamp"]) >= cutoff
    ]

    if not recent:
        await ctx.send("No trades in the last 30 days.")
        return

    grouped = {}

    for trade in recent:
        dt = datetime.fromisoformat(trade["timestamp"])
        date_key = dt.strftime("%Y-%m-%d")
        display_date = dt.strftime("%m/%d")

        if date_key not in grouped:
            grouped[date_key] = {
                "display": display_date,
                "trades": [],
                "daily_pnl": Decimal('0.00')
            }

        price_display = fmt_price_short(trade["price_text"])
        line = f"{trade['action']} {trade['qty']} {trade['ticker']} {trade['strike']} {trade['expiry']} @ {price_display}"

        if trade.get("total_pnl") is not None and trade.get("pnl_pct") is not None:
            line += f" | {fmt_money(trade['total_pnl'])} | {fmt_pct(trade['pnl_pct'])}"
            grouped[date_key]["daily_pnl"] += trade["total_pnl"]

        grouped[date_key]["trades"].append(line)

    sorted_dates = sorted(grouped.keys())

    lines = []

    for i, date in enumerate(sorted_dates):
        lines.append(f"**{grouped[date]['display']} ({fmt_money(grouped[date]['daily_pnl'])})**")
        lines.extend(grouped[date]["trades"])

        if i != len(sorted_dates) - 1:
            lines.append("────────────")

    embed = discord.Embed(
        title=f"{target.display_name}'s Trade History (Last 30 Days)",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )

    embed.set_footer(text=f"Showing {len(recent)} trades from last 30 days")
    await ctx.send(embed=embed)


@bot.command(name="delete")
async def delete_command(ctx, ticker: str):
    ticker = ticker.upper()
    user_id = ctx.author.id

    matches = find_user_ticker_positions(user_id, ticker)

    if not matches:
        await ctx.send(f"No open positions found for **{ticker}**.")
        return

    total_qty = sum(sum(lot["qty"] for lot in lots) for _, lots in matches)

    pending_deletes[user_id] = {
        "ticker": ticker,
        "created_at": time.time()
    }
    save_data()

    embed = discord.Embed(
        title="Delete Confirmation Required",
        description=(
            f"You are about to remove all open positions for **{ticker}**.\n"
            f"Total Contracts: **{total_qty}**\n\n"
            f"Type `!confirm` within **{DELETE_CONFIRM_TTL} seconds** to proceed."
        ),
        color=discord.Color.orange()
    )
    embed.set_author(
        name=ctx.author.display_name,
        icon_url=ctx.author.display_avatar.url
    )
    embed.set_footer(text="Core Entry Alerts")

    await ctx.send(embed=embed)


@bot.command(name="confirm")
async def confirm_command(ctx):
    user_id = ctx.author.id
    pending = pending_deletes.get(user_id)

    if not pending:
        await ctx.send("No pending delete request found.")
        return

    age = time.time() - pending["created_at"]
    if age > DELETE_CONFIRM_TTL:
        del pending_deletes[user_id]
        save_data()
        await ctx.send("Delete request expired. Run `!delete <ticker>` again.")
        return

    ticker = pending["ticker"]
    matches = find_user_ticker_positions(user_id, ticker)

    if not matches:
        del pending_deletes[user_id]
        save_data()
        await ctx.send(f"No open positions found for **{ticker}**.")
        return

    total_deleted_qty = 0
    deleted_contracts = []

    for key, lots in matches:
        _, pos_ticker, strike, expiry, side = key
        qty = sum(lot["qty"] for lot in lots)
        total_deleted_qty += qty
        deleted_contracts.append(f"**{pos_ticker} {strike} {expiry}** | {side} | Qty {qty}")
        del positions[key]

    del pending_deletes[user_id]
    save_data()

    embed = discord.Embed(
        title="Positions Deleted",
        description=(
            f"Removed all open positions for **{ticker}**.\n"
            f"Total Contracts Removed: **{total_deleted_qty}**"
        ),
        color=discord.Color.orange()
    )
    embed.add_field(
        name="Deleted Contracts",
        value="\n".join(deleted_contracts[:10]),
        inline=False
    )
    embed.set_author(
        name=ctx.author.display_name,
        icon_url=ctx.author.display_avatar.url
    )
    embed.set_footer(text="Core Entry Alerts")

    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    load_data()
    print(f"Logged in as {bot.user}")


async def process_trade_message(message):
    """Parse a message as a trade and post the embed.

    Returns True if the message matched the trade pattern (so the caller knows
    not to fall through to the format-help reply). Returns False if the message
    is not a trade at all.
    """
    match = pattern.match(message.content)
    if not match:
        return False

    action, qty, ticker, strike, expiry, price_text = match.groups()

    action = action.upper().strip()
    qty = int(qty) if qty else 1  # Default to 1 if not provided
    ticker = ticker.upper().strip()
    strike = strike.upper().strip()
    expiry = expiry.strip()

    numeric_price = parse_price(price_text)
    market_source = None

    if is_market_price(price_text):
        try:
            numeric_price, market_source = await fetch_uw_market_option_price(
                action=action,
                ticker=ticker,
                strike=strike,
                expiry=expiry,
            )
        except Exception as e:
            await message.channel.send(
                f"❌ Could not fetch UW market price for {ticker} {strike} {expiry}: {e}",
                delete_after=10,
            )
            return True

    if is_market_price(price_text) and numeric_price is not None:
        display_price = f"Market (${numeric_price})"
        price_text_for_log = str(numeric_price)
    else:
        display_price = fmt_price_text(price_text)
        price_text_for_log = price_text

    color = get_color(action)

    title = f"{ticker} {strike} {expiry}"

    if action in ["BTO", "STO"]:
        side = "LONG" if action == "BTO" else "SHORT"

        log_trade(
            user_id=message.author.id,
            action=action,
            qty=qty,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            price_text=price_text_for_log,
            total_pnl=None,
            pnl_pct=None
        )

        add_open_position(
            user_id=message.author.id,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            side=side,
            qty=qty,
            entry_price=numeric_price
        )
        save_data()

        label = "Opened" if action == "BTO" else "Sold to Open"
        description = f"{label} {qty} {ticker} {strike} {expiry} @ {display_price}"

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )

    elif action in ["STC", "BTC"]:
        side = "LONG" if action == "STC" else "SHORT"

        result = close_position(
            user_id=message.author.id,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            side=side,
            close_qty=qty,
            exit_price=numeric_price
        )

        matched_qty = result["matched_qty"]
        total_open_before_close = result["total_open_before_close"]
        remaining_after_close = result["remaining_after_close"]
        avg_entry_price = result["avg_entry_price"]
        avg_exit_price = result["avg_exit_price"]
        remaining_entry_price = result["remaining_entry_price"]
        total_pnl = result["total_pnl"]
        pnl_pct = result["pnl_pct"]
        fully_closed = result["fully_closed"]
        partial_close = result["partial_close"]
        previous_closed_qty = result["previous_closed_qty"]
        total_closed_overall = result["total_closed_overall"]
        total_opened_overall = result["total_opened_overall"]

        if matched_qty == 0:
            await message.channel.send(
                f"❌ No open position found for {ticker} {strike} {expiry}",
                delete_after=5
            )
            # Pattern matched, just nothing to close against — return True so the
            # caller doesn't ALSO post the format-help reply.
            return True

        log_trade(
            user_id=message.author.id,
            action=action,
            qty=qty,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            price_text=price_text_for_log,
            total_pnl=total_pnl,
            pnl_pct=pnl_pct
        )
        save_data()

        headline_prefix = "Partially Closed" if partial_close else "Closed"

        if avg_entry_price is not None and pnl_pct is not None:
            description = (
                f"{headline_prefix} {matched_qty} {ticker} {strike} {expiry} @ {display_price} "
                f"(Entry: ${avg_entry_price}) | Gain: {fmt_pct(pnl_pct)}"
            )
        else:
            description = (
                f"{headline_prefix} {matched_qty} {ticker} {strike} {expiry} @ {display_price}"
            )

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )

        if partial_close:
            closed_pct = (matched_qty / total_open_before_close) * 100 if total_open_before_close else 0
            remaining_entry_text = (
                f"${remaining_entry_price}" if remaining_entry_price is not None else "Unknown"
            )

            partial_block = (
                f"Closed: {matched_qty}/{total_open_before_close} ({closed_pct:.0f}%)\n"
                f"Remaining: {remaining_after_close} @ {remaining_entry_text}"
            )
            embed.add_field(name="Partial Close", value=partial_block, inline=False)

        summary_lines = [
            f"This Close: {matched_qty}",
            f"Previously Closed: {previous_closed_qty}",
            f"Total Closed Overall: {total_closed_overall}/{total_opened_overall}"
        ]

        if avg_exit_price is not None:
            summary_lines.append(f"Average Close Price: ${avg_exit_price}")
        else:
            summary_lines.append("Average Close Price: Market")

        if total_pnl is not None:
            summary_lines.append(f"Total Profit This Close: {fmt_money(total_pnl)}")
        else:
            summary_lines.append("Total Profit This Close: Unavailable")

        embed.add_field(
            name="Trade Summary",
            value="\n".join(summary_lines),
            inline=False
        )

        if fully_closed:
            embed.add_field(name="Status", value="Position fully closed.", inline=False)

    if market_source:
        embed.add_field(name="Market Price Source", value=f"UW `{market_source}`", inline=False)

    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url
    )
    embed.set_footer(text="Core Entry Alerts")
    embed.timestamp = message.created_at

    try:
        await message.channel.send(embed=embed)
    except discord.Forbidden:
        print("Missing permission: Send Messages or Embed Links")
    except discord.HTTPException as e:
        print(f"Failed to send embed: {e}")

    return True


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    matched = await process_trade_message(message)

    if not matched and looks_like_trade_command(message.content):
        await message.reply(
            get_format_help_message(message.content),
            mention_author=False,
            delete_after=8
        )

    await bot.process_commands(message)


@bot.event
async def on_message_edit(before, after):
    """When a user edits a message, give it another chance to be parsed as a
    trade. We only act if the BEFORE version did NOT already match — otherwise
    editing a valid trade (e.g. fixing a typo in the price) would re-process it
    and double-count the position."""
    if after.author.bot:
        return

    # Already processed when first sent — ignore the edit.
    if pattern.match(before.content):
        return

    # Edit turned a non-trade (or malformed trade) into a valid one — process it.
    await process_trade_message(after)

# ---------- Entry point ----------

if not TOKEN:
    raise ValueError("DISCORD_TOKEN is not set in environment variables")

bot.run(TOKEN)
