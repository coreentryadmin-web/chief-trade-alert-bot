import os
import re
import json
import time
import asyncio
import discord
from discord.ext import commands
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

TOKEN = os.getenv("DISCORD_TOKEN")
DELETE_CONFIRM_TTL = 30  # second
# For Railway persistence: change to "/data/bot_data.json" after adding volume
DATA_FILE = "/data/bot_data.json"
CONTRACT_MULTIPLIER = 100  # Options are worth $100 per contract

# End-of-day automation times use Pacific time (PST/PDT automatically).
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
AUTO_EXPIRE_ENABLED = os.getenv("AUTO_EXPIRE_ENABLED", "true").lower() == "true"
AUTO_EXPIRE_HOUR_PT = int(os.getenv("AUTO_EXPIRE_HOUR_PT", "13"))
AUTO_EXPIRE_MINUTE_PT = int(os.getenv("AUTO_EXPIRE_MINUTE_PT", "0"))
OPEN_POSITION_ALERT_ENABLED = os.getenv("OPEN_POSITION_ALERT_ENABLED", "true").lower() == "true"
OPEN_POSITION_ALERT_HOUR_PT = int(os.getenv("OPEN_POSITION_ALERT_HOUR_PT", "13"))
OPEN_POSITION_ALERT_MINUTE_PT = int(os.getenv("OPEN_POSITION_ALERT_MINUTE_PT", "15"))
# Optional fallback if a user has open trades from old data before channel tracking existed.
OPEN_POSITION_FALLBACK_CHANNEL_ID = int(os.getenv("OPEN_POSITION_FALLBACK_CHANNEL_ID", "0"))

# Monthly tournament settings
TOURNAMENT_MIN_CLOSED_TRADES = int(os.getenv("TOURNAMENT_MIN_CLOSED_TRADES", "5"))
# TOURNAMENT_MAX_GAIN_PER_TRADE: 0 (or negative) = no cap, use actual % gains
# Positive number = cap per trade (e.g., 100 = max +100% per trade)
_max_gain_env = os.getenv("TOURNAMENT_MAX_GAIN_PER_TRADE", "0")
TOURNAMENT_MAX_GAIN_PER_TRADE = Decimal(_max_gain_env) if _max_gain_env else Decimal("0")

# Automatic month-end tournament results.
# If TOURNAMENT_RESULTS_CHANNEL_ID is not set, the bot falls back to MEMBER_ALERTS_CHANNEL_ID,
# then ADMIN_ALERTS_CHANNEL_ID, then the user's last known trade channel if available.
AUTO_TOURNAMENT_RESULTS_ENABLED = os.getenv("AUTO_TOURNAMENT_RESULTS_ENABLED", "true").lower() == "true"
TOURNAMENT_RESULTS_CHANNEL_ID = int(os.getenv("TOURNAMENT_RESULTS_CHANNEL_ID", "0"))
TOURNAMENT_RESULTS_HOUR_PT = int(os.getenv("TOURNAMENT_RESULTS_HOUR_PT", "23"))
TOURNAMENT_RESULTS_MINUTE_PT = int(os.getenv("TOURNAMENT_RESULTS_MINUTE_PT", "55"))

# Registration announcement daily during registration window (25th-2nd)
REGISTRATION_ANNOUNCEMENT_ENABLED = os.getenv("REGISTRATION_ANNOUNCEMENT_ENABLED", "true").lower() == "true"
REGISTRATION_ANNOUNCEMENT_HOUR_PT = int(os.getenv("REGISTRATION_ANNOUNCEMENT_HOUR_PT", "12"))
REGISTRATION_ANNOUNCEMENT_MINUTE_PT = int(os.getenv("REGISTRATION_ANNOUNCEMENT_MINUTE_PT", "0"))

# Only these channels will accept trade entries.
# If both are 0, trade parsing is allowed in all channels.
ADMIN_ALERTS_CHANNEL_ID = int(os.getenv("ADMIN_ALERTS_CHANNEL_ID", "0"))
MEMBER_ALERTS_CHANNEL_ID = int(os.getenv("MEMBER_ALERTS_CHANNEL_ID", "0"))
VIP_ALERTS_CHANNEL_ID = int(os.getenv("VIP_ALERTS_CHANNEL_ID", "0"))
SWING_ALERTS_CHANNEL_ID = int(os.getenv("SWING_ALERTS_CHANNEL_ID", "0"))
LEAP_ALERTS_CHANNEL_ID = int(os.getenv("LEAP_ALERTS_CHANNEL_ID", "0"))
ALLOWED_TRADE_CHANNEL_IDS = {
   cid for cid in (
        ADMIN_ALERTS_CHANNEL_ID,
        MEMBER_ALERTS_CHANNEL_ID,
        VIP_ALERTS_CHANNEL_ID,
        SWING_ALERTS_CHANNEL_ID,
        LEAP_ALERTS_CHANNEL_ID,
    ) if cid
}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

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
last_trade_channels = {}
tournament_players = {}
tournament_meta = {}


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
    """Convert M/D, M/D/YY, or M/D/YYYY into YYYY-MM-DD."""
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
            "`bto spx 7130c 5/15 @ 1.25`\n"
            "`bto 5 spx 7130c 5/15 @ 1.25`\n"
            "Use: `[action] [qty optional] [ticker] [strike][c/p] [expiry] @ [price]`"
        )

    if parts[0] not in ["bto", "stc", "sto", "btc"]:
        return "❌ Invalid action. Use one of: `bto`, `stc`, `sto`, `btc`"

    if len(parts) < 5:
        return (
            "❌ Invalid trade format.\n"
            "Examples:\n"
            "`bto spx 7130c 5/15 @ 1.25`\n"
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
            "`bto spx 7130c 5/15 @ 1.25`\n"
            "`bto 5 spx 7130c 5/15 @ 1.25`"
        )

    strike = parts[idx + 1]
    if not re.match(r'^\d+(?:\.\d+)?[cp]$', strike, re.IGNORECASE):
        return "❌ Invalid strike format. Use strike like `7130c` or `7130p`."

    expiry = parts[idx + 2]
    if not re.match(r'^\d{1,2}/\d{1,2}(?:/\d{2,4})?$', expiry):
        return "❌ Invalid expiry format. Use date like `5/15`, `04/17`, `01/15/28`, or `01/15/2028`."

    return (
        "❌ Invalid trade format.\n"
        "Examples:\n"
        "`bto spx 7130c 5/15 @ 1.25`\n"
        "`bto 5 spx 7130c 5/15 @ 1.25`\n"
        "`@m` is currently disabled. Use manual prices like `@ 1.25`."
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
        "pending_deletes": {},
        "last_trade_channels": {},
        "tournament_players": {},
        "tournament_meta": {}
    }

    # Convert positions, preserving Decimal values as strings
    for key, lots in positions.items():
        key_str = contract_key_to_str(key)
        data["positions"][key_str] = [
            {
                "qty": lot["qty"],
                "entry_price": str(lot["entry_price"]) if lot["entry_price"] is not None else None,
                "opened_at": lot.get("opened_at")
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
                "pnl_pct": float(trade["pnl_pct"]) if trade["pnl_pct"] is not None else None,
                "opened_at": trade.get("opened_at"),
                "matched_opened_at": trade.get("matched_opened_at", [])
            }
            for trade in history
        ]

    # Convert pending_deletes only
    for user_id, value in pending_deletes.items():
        data["pending_deletes"][str(user_id)] = value

    for user_id, channel_id in last_trade_channels.items():
        data["last_trade_channels"][str(user_id)] = int(channel_id)

    for user_id, value in tournament_players.items():
        data["tournament_players"][str(user_id)] = {
            "joined_at": value.get("joined_at"),
            "effective_month": value.get("effective_month"),
            "active": bool(value.get("active", True))
        }

    data["tournament_meta"] = dict(tournament_meta)

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
    global positions, trade_stats, user_stats, trade_history, pending_deletes, last_trade_channels, tournament_players, tournament_meta

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
                "entry_price": Decimal(lot["entry_price"]) if lot["entry_price"] is not None else None,
                "opened_at": lot.get("opened_at")
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
                "pnl_pct": Decimal(str(trade["pnl_pct"])) if trade["pnl_pct"] is not None else None,
                "opened_at": trade.get("opened_at"),
                "matched_opened_at": trade.get("matched_opened_at", [])
            })
        loaded_trade_history[int(user_id_str)] = loaded_trades
    trade_history = loaded_trade_history

    pending_deletes = {
        int(user_id_str): value
        for user_id_str, value in data.get("pending_deletes", {}).items()
    }

    last_trade_channels = {
        int(user_id_str): int(channel_id)
        for user_id_str, channel_id in data.get("last_trade_channels", {}).items()
    }

    tournament_players = {
        int(user_id_str): {
            "joined_at": value.get("joined_at"),
            "effective_month": value.get("effective_month") or _current_tournament_month(),
            "active": bool(value.get("active", True))
        }
        for user_id_str, value in data.get("tournament_players", {}).items()
    }

    tournament_meta = dict(data.get("tournament_meta", {}))


def log_trade(
    user_id,
    action,
    qty,
    ticker,
    strike,
    expiry,
    price_text,
    total_pnl=None,
    pnl_pct=None,
    opened_at=None,
    matched_opened_at=None
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
        "pnl_pct": pnl_pct,
        "opened_at": opened_at,
        "matched_opened_at": matched_opened_at or []
    })


def add_open_position(user_id, ticker, strike, expiry, side, qty, entry_price):
    """Add an open position (BTO/STO)."""
    key = contract_key(user_id, ticker, strike, expiry, side)
    positions[key].append({
        "qty": qty,
        "entry_price": entry_price,  # Will be Decimal or None
        "opened_at": datetime.utcnow().isoformat()
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
            "opened_at": None,
            "matched_opened_at": [],
            "error_message": "No open position found."
        }

    total_open_before_close = sum(lot["qty"] for lot in opens)
    remaining_to_close = close_qty

    matched_qty = 0
    total_entry_value = Decimal('0.00')
    total_pnl = Decimal('0.00')
    pnl_available = True
    matched_opened_at_values = []

    i = 0
    while i < len(opens) and remaining_to_close > 0:
        lot = opens[i]
        lot_qty = lot["qty"]
        lot_entry = lot["entry_price"]

        qty_used = min(lot_qty, remaining_to_close)

        matched_qty += qty_used
        remaining_to_close -= qty_used

        lot_opened_at = lot.get("opened_at")
        if lot_opened_at:
            matched_opened_at_values.append(lot_opened_at)

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
        "opened_at": matched_opened_at_values[0] if matched_opened_at_values else None,
        "matched_opened_at": matched_opened_at_values,
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




def parse_expiry_date_pt(expiry: str):
    """Return expiry date object using the same expiry parsing logic."""
    try:
        return datetime.fromisoformat(parse_expiry_to_iso(expiry)).date()
    except Exception:
        return None


def get_user_alert_channel(user_id: int):
    """Return the last channel where this user traded, with optional fallback."""
    channel_id = last_trade_channels.get(user_id) or OPEN_POSITION_FALLBACK_CHANNEL_ID
    if not channel_id:
        return None
    return bot.get_channel(int(channel_id))


def format_position_lines(user_positions: list[dict]) -> list[str]:
    lines = []
    for pos in user_positions:
        avg_entry_text = f"${pos['avg_entry']}" if pos["avg_entry"] is not None else "Market/Unknown"
        lines.append(
            f"**{pos['ticker']} {pos['strike']} {pos['expiry']}** | "
            f"{pos['side']} | Qty {pos['qty']} | Avg Entry {avg_entry_text}"
        )
    return lines


async def auto_expire_positions_once() -> int:
    """Auto-close expired open contracts at $0.00 and post a notice."""
    today_pt = datetime.now(PACIFIC_TZ).date()
    keys_to_expire = []

    for key, lots in list(positions.items()):
        user_id, ticker, strike, expiry, side = key
        exp_date = parse_expiry_date_pt(expiry)
        if exp_date is None:
            continue
        if exp_date <= today_pt and sum(lot["qty"] for lot in lots) > 0:
            keys_to_expire.append(key)

    expired_count = 0

    for key in keys_to_expire:
        if key not in positions:
            continue

        user_id, ticker, strike, expiry, side = key
        lots = positions.get(key, [])
        qty_to_close = sum(lot["qty"] for lot in lots)
        if qty_to_close <= 0:
            continue

        close_action = "STC" if side == "LONG" else "BTC"
        result = close_position(
            user_id=user_id,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            side=side,
            close_qty=qty_to_close,
            exit_price=Decimal("0.00"),
        )

        if result["matched_qty"] <= 0:
            continue

        log_trade(
            user_id=user_id,
            action=close_action,
            qty=result["matched_qty"],
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            price_text="0.00",
            total_pnl=result["total_pnl"],
            pnl_pct=result["pnl_pct"],
            opened_at=result.get("opened_at"),
            matched_opened_at=result.get("matched_opened_at", [])
        )
        save_data()
        expired_count += 1

        channel = get_user_alert_channel(user_id)
        if channel is None:
            print(f"[auto-expire] no channel for user {user_id}; expired {ticker} {strike} {expiry}")
            continue

        user = bot.get_user(user_id)
        user_text = user.mention if user else f"<@{user_id}>"
        avg_entry = result["avg_entry_price"]
        avg_entry_text = f"${avg_entry}" if avg_entry is not None else "Unknown"
        pnl_text = fmt_money(result["total_pnl"]) if result["total_pnl"] is not None else "Unavailable"

        embed = discord.Embed(
            title="⏰ Auto-Expired Position",
            description=(
                f"{user_text}, this contract expired and was still open.\n\n"
                f"**{ticker} {strike} {expiry}** | {side} | Qty {result['matched_qty']} | Avg Entry {avg_entry_text}\n\n"
                f"Auto-closed at **$0.00**\n"
                f"Realized P/L: **{pnl_text}**"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Core Entry Alerts")
        await channel.send(embed=embed)
        await asyncio.sleep(1)

    return expired_count


async def post_open_position_reminders_once() -> int:
    """Post one open-position reminder per user in their last trade channel."""
    user_ids = sorted({key[0] for key in positions.keys()})
    sent_count = 0

    for user_id in user_ids:
        user_positions = summarize_user_positions(user_id)
        if not user_positions:
            continue

        channel = get_user_alert_channel(user_id)
        if channel is None:
            print(f"[open-alert] no channel for user {user_id}")
            continue

        lines = format_position_lines(user_positions)
        user = bot.get_user(user_id)
        user_text = user.mention if user else f"<@{user_id}>"

        embed = discord.Embed(
            title="🔔 End-of-Day Open Positions Reminder",
            description=f"{user_text}, you still have these open:\n\n" + "\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Core Entry Alerts")
        await channel.send(embed=embed)
        await asyncio.sleep(1)
        sent_count += 1

    return sent_count


async def end_of_day_task_loop():
    """Run auto-expiry at 1:00 PM PT and open-position reminders at 1:15 PM PT."""
    await bot.wait_until_ready()

    last_auto_expire_date = None
    last_open_alert_date = None

    print(
        f"[eod] auto-expire {AUTO_EXPIRE_HOUR_PT:02d}:{AUTO_EXPIRE_MINUTE_PT:02d} PT | "
        f"open reminders {OPEN_POSITION_ALERT_HOUR_PT:02d}:{OPEN_POSITION_ALERT_MINUTE_PT:02d} PT"
    )

    while not bot.is_closed():
        now_pt = datetime.now(PACIFIC_TZ)
        today_key = now_pt.strftime("%Y-%m-%d")

        if (
            AUTO_EXPIRE_ENABLED
            and now_pt.hour == AUTO_EXPIRE_HOUR_PT
            and now_pt.minute == AUTO_EXPIRE_MINUTE_PT
            and last_auto_expire_date != today_key
        ):
            try:
                count = await auto_expire_positions_once()
                print(f"[auto-expire] completed for {today_key}; expired_groups={count}")
            except Exception as e:
                print(f"[auto-expire] error: {e!r}")
            last_auto_expire_date = today_key

        if (
            OPEN_POSITION_ALERT_ENABLED
            and now_pt.hour == OPEN_POSITION_ALERT_HOUR_PT
            and now_pt.minute == OPEN_POSITION_ALERT_MINUTE_PT
            and last_open_alert_date != today_key
        ):
            try:
                count = await post_open_position_reminders_once()
                print(f"[open-alert] completed for {today_key}; users_notified={count}")
            except Exception as e:
                print(f"[open-alert] error: {e!r}")
            last_open_alert_date = today_key

        await asyncio.sleep(30)


# ---------- Help + Tournament Commands ----------

def _month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


def _month_bounds_utc_from_pt(now_pt: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Return UTC-naive month bounds based on Pacific calendar month.

    Trade timestamps are stored as UTC-naive strings, but tournament months should
    follow the server/business calendar in Pacific time.
    """
    now_pt = now_pt or datetime.now(PACIFIC_TZ)
    start_pt = datetime(now_pt.year, now_pt.month, 1, tzinfo=PACIFIC_TZ)

    if now_pt.month == 12:
        next_start_pt = datetime(now_pt.year + 1, 1, 1, tzinfo=PACIFIC_TZ)
    else:
        next_start_pt = datetime(now_pt.year, now_pt.month + 1, 1, tzinfo=PACIFIC_TZ)

    start_utc = start_pt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = next_start_pt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    label = start_pt.strftime("%B %Y")
    return start_utc, end_utc, label


def _is_last_day_of_month_pt(now_pt: datetime | None = None) -> bool:
    now_pt = now_pt or datetime.now(PACIFIC_TZ)
    tomorrow = now_pt + timedelta(days=1)
    return tomorrow.month != now_pt.month


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _eligible_tournament_trades(
    user_id: int,
    month_start: datetime | None = None,
    month_end: datetime | None = None,
) -> list[dict]:
    """Closed tournament trades eligible for scoring.

    Rules:
    - User must be registered and active.
    - Only STC/BTC closes count.
    - Close timestamp must be inside the tournament month.
    - The matched opening lot(s) must also have been opened inside the same month.
    - Old carried runners from a prior month do not count in the new month.
    """
    player = tournament_players.get(user_id)
    if not player or not player.get("active", True):
        return []

    effective_month = player.get("effective_month") or _current_tournament_month()

    if month_start is None or month_end is None:
        month_start, month_end, _ = _month_bounds_utc_from_effective_month(effective_month)

    trades = []
    for trade in trade_history.get(user_id, []):
        if trade.get("action") not in ("STC", "BTC"):
            continue
        if trade.get("pnl_pct") is None:
            continue

        trade_time = _parse_iso_datetime(trade.get("timestamp"))
        if trade_time is None or not (month_start <= trade_time < month_end):
            continue

        matched_opened_at = trade.get("matched_opened_at") or []
        if not matched_opened_at and trade.get("opened_at"):
            matched_opened_at = [trade.get("opened_at")]

        # New tournament rule: the opening lot must also belong to this same month.
        # Old records with no opened_at are intentionally excluded from scoring.
        if not matched_opened_at:
            continue

        all_opened_in_month = True
        for opened_at_raw in matched_opened_at:
            opened_at = _parse_iso_datetime(opened_at_raw)
            if opened_at is None or not (month_start <= opened_at < month_end):
                all_opened_in_month = False
                break

        if not all_opened_in_month:
            continue

        trades.append(trade)

    return trades


def _tournament_score(
    user_id: int,
    month_start: datetime | None = None,
    month_end: datetime | None = None,
) -> dict:
    trades = _eligible_tournament_trades(user_id, month_start, month_end)
    closed_trades = len(trades)
    best_trade_pct = None
    wins = losses = flat = 0

    for trade in trades:
        pct = Decimal(str(trade.get("pnl_pct") or "0"))
        # Only apply cap if TOURNAMENT_MAX_GAIN_PER_TRADE > 0 (0 or negative = no cap)
        if TOURNAMENT_MAX_GAIN_PER_TRADE > 0 and pct > TOURNAMENT_MAX_GAIN_PER_TRADE:
            pct = TOURNAMENT_MAX_GAIN_PER_TRADE

        if best_trade_pct is None or pct > best_trade_pct:
            best_trade_pct = pct

        if pct > 0:
            wins += 1
        elif pct < 0:
            losses += 1
        else:
            flat += 1

    score = best_trade_pct if best_trade_pct is not None else Decimal("0.00")
    decided = wins + losses
    win_rate = (wins / decided * 100) if decided else 0.0

    return {
        "score": score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "win_rate": win_rate,
        "qualified": closed_trades >= TOURNAMENT_MIN_CLOSED_TRADES,
    }


def _leaderboard_rows(
    month_start: datetime | None = None,
    month_end: datetime | None = None,
    effective_month: str | None = None,
) -> list[tuple[int, dict]]:
    rows = []
    effective_month = effective_month or _current_tournament_month()

    for user_id, player in tournament_players.items():
        if not player.get("active", True):
            continue
        if player.get("effective_month") != effective_month:
            continue
        stats = _tournament_score(user_id, month_start, month_end)
        rows.append((user_id, stats))

    rows.sort(key=lambda row: (row[1]["qualified"], row[1]["score"], row[1]["closed_trades"]), reverse=True)
    return rows


def _resolve_tournament_results_channel():
    channel_id = (
        TOURNAMENT_RESULTS_CHANNEL_ID
        or MEMBER_ALERTS_CHANNEL_ID
        or ADMIN_ALERTS_CHANNEL_ID
    )
    if channel_id:
        ch = bot.get_channel(int(channel_id))
        if ch is not None:
            return ch

    for channel_id in last_trade_channels.values():
        ch = bot.get_channel(int(channel_id))
        if ch is not None:
            return ch

    return None


def _build_monthly_results_embed(month_start: datetime, month_end: datetime, month_label: str) -> discord.Embed:
    rows = _leaderboard_rows(month_start, month_end, month_start.strftime("%Y-%m"))
    qualified = [(uid, stats) for uid, stats in rows if stats["qualified"]]

    if not rows:
        return discord.Embed(
            title=f"🏆 BLACKOUT Monthly Results — {month_label}",
            description="No tournament participants joined this month.",
            color=discord.Color.dark_grey(),
        )

    medal = ["🥇", "🥈", "🥉"]
    lines = []

    if qualified:
        for idx, (user_id, stats) in enumerate(qualified[:10], start=1):
            user = bot.get_user(user_id)
            name = user.display_name if user else f"User {user_id}"
            prefix = medal[idx - 1] if idx <= 3 else f"#{idx}"
            prize_note = " 🏆" if idx <= 3 else ""
            lines.append(
                f"{prefix} **{name}** — **{fmt_pct(stats['score'])}** "
                f"| {stats['closed_trades']} closed | Win Rate {stats['win_rate']:.1f}%{prize_note}"
            )
    else:
        lines.append("No traders met the minimum closed-trade requirement this month.")
        lines.append("")
        lines.append("**Top activity:**")
        for idx, (user_id, stats) in enumerate(rows[:5], start=1):
            user = bot.get_user(user_id)
            name = user.display_name if user else f"User {user_id}"
            lines.append(
                f"#{idx} **{name}** — {fmt_pct(stats['score'])} "
                f"| {stats['closed_trades']} closed"
            )

    embed = discord.Embed(
        title=f"🏆 BLACKOUT Monthly Results — {month_label}",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=f"Minimum {TOURNAMENT_MIN_CLOSED_TRADES} closed trades to qualify · New month starts after reset"
    )
    return embed


async def post_monthly_tournament_results_once(month_start: datetime, month_end: datetime, month_label: str) -> bool:
    channel = _resolve_tournament_results_channel()
    if channel is None:
        print("[tournament] no results channel found")
        return False

    embed = _build_monthly_results_embed(month_start, month_end, month_label)
    await channel.send(embed=embed)
    print(f"[tournament] monthly results posted for {month_label} → #{channel.name}")
    return True


async def tournament_results_task_loop():
    """Post final monthly winners on the last calendar day at configured PT time."""
    await bot.wait_until_ready()

    print(
        f"[tournament] auto-results "
        f"{TOURNAMENT_RESULTS_HOUR_PT:02d}:{TOURNAMENT_RESULTS_MINUTE_PT:02d} PT "
        f"| enabled={AUTO_TOURNAMENT_RESULTS_ENABLED}"
    )

    while not bot.is_closed():
        now_pt = datetime.now(PACIFIC_TZ)
        month_start, month_end, month_label = _month_bounds_utc_from_pt(now_pt)
        month_key = now_pt.strftime("%Y-%m")

        should_post = (
            AUTO_TOURNAMENT_RESULTS_ENABLED
            and _is_last_day_of_month_pt(now_pt)
            and now_pt.hour == TOURNAMENT_RESULTS_HOUR_PT
            and now_pt.minute == TOURNAMENT_RESULTS_MINUTE_PT
            and tournament_meta.get("last_results_month") != month_key
        )

        if should_post:
            try:
                posted = await post_monthly_tournament_results_once(month_start, month_end, month_label)
                if posted:
                    tournament_meta["last_results_month"] = month_key
                    save_data()
            except Exception as e:
                print(f"[tournament] monthly results post failed: {e!r}")

        await asyncio.sleep(30)


async def announce_registration_window_once() -> bool:
    """Announce registration window opening on the 25th of each month."""
    channel = _resolve_tournament_results_channel()
    if channel is None:
        print("[registration-announce] no announcement channel found")
        return False

    now_pt = datetime.now(PACIFIC_TZ)
    upcoming_month = _registration_effective_month(now_pt)
    month_label = _month_bounds_utc_from_effective_month(upcoming_month)[2]

    max_gain_line = (
        f"• Max gain per trade capped at {TOURNAMENT_MAX_GAIN_PER_TRADE}%\n"
        if TOURNAMENT_MAX_GAIN_PER_TRADE > 0
        else "• No cap on gains — unlimited upside\n"
    )
    
    embed = discord.Embed(
        title="🎉 Tournament Registration Opens",
        description=(
            f"Registration for the **{month_label}** BLACKOUT Monthly Tournament is now open!\n\n"
            f"**Registration Window:** Today through {(now_pt + timedelta(days=8)).strftime('%B %d')}\n\n"
            f"**How to Join:**\n"
            f"Type `!join` to register for the {month_label} challenge.\n\n"
            f"**Tournament Details:**\n"
            f"• Trades count from the 1st to the last day of {month_label.split()[0]}\n"
            f"• Only closed trades count toward your score\n"
            f"• Minimum {TOURNAMENT_MIN_CLOSED_TRADES} closed trades required to qualify\n"
            f"• Score = sum of all closed trade % gains\n"
            f"{max_gain_line}\n"
            f"Type `!tournament` for full rules and scoring details."
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="BLACKOUT Monthly Tournament")
    await channel.send(embed=embed)
    print(f"[registration-announce] announced {month_label} registration opening")
    return True


async def registration_announcement_task_loop():
    """Announce registration window daily during the registration period (25th-2nd)."""
    await bot.wait_until_ready()

    print(
        f"[registration-announce] enabled={REGISTRATION_ANNOUNCEMENT_ENABLED} "
        f"daily during registration window (25th-2nd) at {REGISTRATION_ANNOUNCEMENT_HOUR_PT:02d}:{REGISTRATION_ANNOUNCEMENT_MINUTE_PT:02d} PT"
    )

    last_announcement_date = None

    while not bot.is_closed():
        now_pt = datetime.now(PACIFIC_TZ)
        today_key = now_pt.strftime("%Y-%m-%d")

        # Registration window is 25th-31st and 1st-2nd
        in_registration_window = now_pt.day >= 25 or now_pt.day <= 2

        if (
            REGISTRATION_ANNOUNCEMENT_ENABLED
            and in_registration_window
            and now_pt.hour == REGISTRATION_ANNOUNCEMENT_HOUR_PT
            and now_pt.minute == REGISTRATION_ANNOUNCEMENT_MINUTE_PT
            and last_announcement_date != today_key
        ):
            try:
                await announce_registration_window_once()
            except Exception as e:
                print(f"[registration-announce] error: {e!r}")
            last_announcement_date = today_key

        await asyncio.sleep(30)


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="📘 BLACKOUT Trade Bot Help",
        description=(
            "Log option trades, track open positions, calculate P/L, and compete in the monthly tournament.\n\n"
            "**Trade Format**\n"
            "`[action] [qty optional] [ticker] [strike][c/p] [expiry] @ [price]`\n\n"
            "**Examples**\n"
            "`bto spy 590c 5/16 @ 1.25`\n"
            "`bto 5 spy 590c 5/16 @ 1.25`\n"
            "`stc spy 590c 5/16 @ 2.10`\n"
            "`bto spx 7130c 5/15 @ 1.25`\n\n"
            "`@m` is disabled. Please enter a manual price like `@ 1.25`."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Actions",
        value="`BTO` Buy To Open\n`STC` Sell To Close\n`STO` Sell To Open\n`BTC` Buy To Close",
        inline=False,
    )
    embed.add_field(
        name="Tracking Commands",
        value=(
            "`!current` → open positions\n"
            "`!stats` → trader stats\n"
            "`!profit` → all-time realized P/L\n"
            "`!daily` → today's trades + P/L\n"
            "`!history` or `!trades` → last 30 days\n"
            "`!delete TICKER` → delete open positions\n"
            "`!confirm` → confirm delete request"
        ),
        inline=False,
    )
    embed.add_field(
        name="Tournament Commands",
        value=(
            "`!join` → join the monthly tournament\n"
            "`!leaderboard` → monthly rankings\n"
            "`!rank` → your current tournament rank\n"
            "`!tournament` → rules and scoring"
        ),
        inline=False,
    )
    embed.set_footer(text="BLACKOUT Trade Alerts")
    await ctx.send(embed=embed)



def _registration_effective_month(now_pt: datetime | None = None) -> str:
    """Return YYYY-MM tournament month for a registration.

    Registration window is 25th through 2nd:
      - 25th-end of month registers for next month
      - 1st-2nd registers for current month
    """
    now_pt = now_pt or datetime.now(PACIFIC_TZ)

    if now_pt.day >= 25:
        if now_pt.month == 12:
            return f"{now_pt.year + 1}-01"
        return f"{now_pt.year}-{now_pt.month + 1:02d}"

    return f"{now_pt.year}-{now_pt.month:02d}"


def _month_bounds_utc_from_effective_month(effective_month: str) -> tuple[datetime, datetime, str]:
    """Return UTC-naive month bounds for an effective YYYY-MM tournament month."""
    year_s, month_s = effective_month.split("-", 1)
    year = int(year_s)
    month = int(month_s)

    start_pt = datetime(year, month, 1, tzinfo=PACIFIC_TZ)

    if month == 12:
        next_start_pt = datetime(year + 1, 1, 1, tzinfo=PACIFIC_TZ)
    else:
        next_start_pt = datetime(year, month + 1, 1, tzinfo=PACIFIC_TZ)

    start_utc = start_pt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = next_start_pt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    label = start_pt.strftime("%B %Y")

    return start_utc, end_utc, label


def _current_tournament_month() -> str:
    """Current tournament month in Pacific time, formatted YYYY-MM."""
    now_pt = datetime.now(PACIFIC_TZ)
    return f"{now_pt.year}-{now_pt.month:02d}"



@bot.command(name="join")
async def join_command(ctx):
    now_pt = datetime.now(PACIFIC_TZ)

    # Registration window: 25th-31st and 1st-2nd
    if not (now_pt.day >= 25 or now_pt.day <= 2):
        await ctx.send(
            "❌ Tournament registration is closed for this month.\n"
            "Registration opens again at month end."
        )
        return

    user_id = ctx.author.id
    effective_month = _registration_effective_month(now_pt)

    # Check if ALREADY registered for THIS month
    if user_id in tournament_players and tournament_players[user_id].get("active", True):
        player_month = tournament_players[user_id].get("effective_month") or _current_tournament_month()
        
        if player_month == effective_month:
            # Already registered for this month
            joined_at = tournament_players[user_id].get("joined_at", "already joined")
            month_label = _month_bounds_utc_from_effective_month(effective_month)[2]
            await ctx.send(
                f"✅ {ctx.author.mention}, you are already registered for the **{month_label}** BLACKOUT Monthly Tournament. "
                f"Joined: `{joined_at}`"
            )
            return
        # Otherwise allow re-registration for new month (don't return)

    tournament_players[user_id] = {
        "joined_at": datetime.utcnow().isoformat(),
        "effective_month": effective_month,
        "active": True,
    }
    save_data()

    month_label = _month_bounds_utc_from_effective_month(effective_month)[2]
    embed = discord.Embed(
        title="✅ Tournament Joined",
        description=(
            f"{ctx.author.mention}, you joined the **BLACKOUT Monthly Tournament**.\n\n"
            f"Your registration is for **{month_label}**.\n"
            f"Trades start counting from **{month_label.split()[0]} 1st**.\n"
            "Only **closed trades** count toward the leaderboard.\n\n"
            "Good luck. 🔥"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="BLACKOUT Monthly Tournament")
    await ctx.send(embed=embed)


@bot.command(name="tournament")
async def tournament_command(ctx):
    _, _, month_label = _month_bounds_utc_from_pt()
    
    max_gain_text = (
        f"`{TOURNAMENT_MAX_GAIN_PER_TRADE}%` per trade" 
        if TOURNAMENT_MAX_GAIN_PER_TRADE > 0 
        else "Unlimited (no cap)"
    )
    
    embed = discord.Embed(
        title=f"🏆 BLACKOUT Monthly Tournament — {month_label}",
        description=(
            "**How to enter**\n"
            "Type `!join` before posting tournament trades.\n\n"
            "**Scoring**\n"
            "• Only bot-logged trades count\n"
            "• Only closed trades count\n"
            "• Score = highest single realized trade % for the month\n"
            "• Trade must be opened and closed in the same tournament month\n"
            f"• Minimum `{TOURNAMENT_MIN_CLOSED_TRADES}` closed trades required to qualify\n"
            f"• Max gain per trade: {max_gain_text}\n\n"
            "**Commands**\n"
            "`!leaderboard` → rankings\n"
            "`!rank` → your rank\n"
            "`!current` → open positions"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Leaderboard resets on the 1st of each month")
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_command(ctx):
    # During registration window (25th-2nd), show upcoming month
    # Otherwise show current calendar month
    now_pt = datetime.now(PACIFIC_TZ)
    
    if now_pt.day >= 25 or now_pt.day <= 2:
        # Registration window - show the month being registered for
        effective_month = _registration_effective_month(now_pt)
    else:
        # Regular tournament period - show current calendar month
        effective_month = _current_tournament_month()
    
    rows = _leaderboard_rows(effective_month=effective_month)
    month_label = _month_bounds_utc_from_effective_month(effective_month)[2]

    if not rows:
        await ctx.send(
            f"No one has joined the **{month_label}** tournament yet. "
            "Type `!join` to enter."
        )
        return

    medal = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (user_id, stats) in enumerate(rows[:10], start=1):
        user = bot.get_user(user_id)
        name = user.display_name if user else f"User {user_id}"
        prefix = medal[idx - 1] if idx <= 3 else f"#{idx}"
        q = "✅" if stats["qualified"] else f"Needs {max(0, TOURNAMENT_MIN_CLOSED_TRADES - stats['closed_trades'])} more"
        lines.append(
            f"{prefix} **{name}** — **{fmt_pct(stats['score'])}** "
            f"| {stats['closed_trades']} closed | Win Rate {stats['win_rate']:.1f}% | {q}"
        )

    embed = discord.Embed(
        title=f"🏆 BLACKOUT Monthly Leaderboard — {month_label}",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Minimum {TOURNAMENT_MIN_CLOSED_TRADES} closed trades to qualify · Closed trades only")
    await ctx.send(embed=embed)


@bot.command(name="rank")
async def rank_command(ctx):
    user_id = ctx.author.id
    if user_id not in tournament_players or not tournament_players[user_id].get("active", True):
        await ctx.send("You are not in the tournament yet. Type `!join` to enter.")
        return

    # Get the user's registered tournament month
    player = tournament_players[user_id]
    effective_month = player.get("effective_month") or _current_tournament_month()
    
    # Get leaderboard for their tournament month (not current calendar month)
    rows = _leaderboard_rows(effective_month=effective_month)
    rank = next((i for i, (uid, _) in enumerate(rows, start=1) if uid == user_id), None)
    
    stats = _tournament_score(user_id)
    needed = max(0, TOURNAMENT_MIN_CLOSED_TRADES - stats["closed_trades"])
    
    month_label = _month_bounds_utc_from_effective_month(effective_month)[2]

    description = (
        f"**Tournament:** {month_label}\n"
        f"**Rank:** #{rank if rank else '—'}\n"
        f"**Monthly Score:** {fmt_pct(stats['score'])}\n"
        f"**Closed Trades:** {stats['closed_trades']}\n"
        f"**Win Rate:** {stats['win_rate']:.1f}%\n"
        f"**Wins/Losses/Flat:** {stats['wins']} / {stats['losses']} / {stats['flat']}\n"
    )
    if stats["qualified"]:
        description += "\n✅ You are qualified for prizes."
    else:
        description += f"\n⚠️ You need `{needed}` more closed trade(s) to qualify."

    embed = discord.Embed(
        title="📊 Your Tournament Rank",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    embed.set_footer(text="BLACKOUT Monthly Tournament")
    await ctx.send(embed=embed)


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

    if not hasattr(bot, "eod_task_started"):
        bot.eod_task_started = True
        bot.loop.create_task(end_of_day_task_loop())

    if not hasattr(bot, "tournament_results_task_started"):
        bot.tournament_results_task_started = True
        bot.loop.create_task(tournament_results_task_loop())

    if not hasattr(bot, "registration_announcement_task_started"):
        bot.registration_announcement_task_started = True
        bot.loop.create_task(registration_announcement_task_loop())


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

    # Block users from opening already-expired contracts.
    # Same-day 0DTE trades are still allowed.
    expiry_date = parse_expiry_date_pt(expiry)
    today_pt = datetime.now(PACIFIC_TZ).date()

    if action in ["BTO", "STO"] and expiry_date is not None and expiry_date < today_pt:
        await message.channel.send(
            f"❌ Cannot open expired contract: {ticker} {strike} {expiry}.",
            delete_after=8,
        )
        return True

    # Remember where this user trades so EOD alerts post in the same channel.
    last_trade_channels[message.author.id] = message.channel.id

    numeric_price = parse_price(price_text)

    if is_market_price(price_text):
        await message.channel.send(
            "❌ Market price `@m` is disabled. Please enter a manual price like `@ 1.25`.",
            delete_after=10,
        )
        return True

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
            pnl_pct=pnl_pct,
            opened_at=result.get("opened_at"),
            matched_opened_at=result.get("matched_opened_at", [])
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

    # Only process trade entries in approved alert channels, when configured.
    # Commands like !current, !stats, !daily still work in other channels.
    if ALLOWED_TRADE_CHANNEL_IDS and message.channel.id not in ALLOWED_TRADE_CHANNEL_IDS:
        await bot.process_commands(message)
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

    if ALLOWED_TRADE_CHANNEL_IDS and after.channel.id not in ALLOWED_TRADE_CHANNEL_IDS:
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
