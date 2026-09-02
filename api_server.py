"""HTTP ingest for desk-automated trades (0DTE Command → Discord alerts + PnL)."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn is already serving this app — don't bind a second HTTP server in on_ready.
    bot.api_server_started = True
    bot.ensure_discord_started()
    yield


app = FastAPI(title="Chief Trade Alert Bot API", version="1.0.0", lifespan=lifespan)


class TradeIn(BaseModel):
    action: str = Field(description="BTO | STO | STC | BTC")
    qty: int = 1
    ticker: str
    strike: str
    expiry: str
    price: float = Field(gt=0)
    idempotency_key: Optional[str] = None
    author_name: Optional[str] = None


async def _run_on_bot_loop(coro, timeout: float = 30.0):
    """Discord.py requires coroutines on the bot's event loop (uvicorn runs elsewhere)."""
    if not bot.bot.loop or not bot.bot.loop.is_running():
        raise HTTPException(status_code=503, detail="Discord bot event loop not running")
    future = asyncio.run_coroutine_threadsafe(coro, bot.bot.loop)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except asyncio.TimeoutError:
        future.cancel()
        raise HTTPException(status_code=504, detail="Discord bot request timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/health")
async def health():
    diag = bot.desk_diagnostics()
    return {
        "ok": True,
        "bot_ready": bot.is_desk_ready(),
        "discord_is_ready": bool(bot.bot and bot.bot.is_ready()),
        **diag,
    }


@app.post("/api/trade")
async def post_trade(body: TradeIn, authorization: Optional[str] = Header(None)):
    secret = (bot.API_SECRET or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="API not configured")
    if authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not bot.is_desk_ready():
        raise HTTPException(status_code=503, detail="Discord bot not ready")

    channel_id = bot.API_CHANNEL_ID
    if not channel_id:
        raise HTTPException(status_code=503, detail="CHIEF_TRADE_CHANNEL_ID not set")

    async def _execute():
        channel = bot.bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.bot.fetch_channel(channel_id)
        if channel is None:
            return {"ok": False, "error": f"Channel {channel_id} not found", "status": 503}

        user_id = bot.resolve_desk_user_id()
        if not user_id:
            return {"ok": False, "error": "Discord bot user not available", "status": 503}

        price_text = f"{body.price:.2f}".rstrip("0").rstrip(".")
        author_name = (body.author_name or os.getenv("CHIEF_TRADE_AUTHOR_NAME", "Night-Hawk-Bot")).strip()

        result = await bot.execute_structured_trade(
            user_id=user_id,
            channel=channel,
            action=body.action.upper().strip(),
            qty=max(1, int(body.qty)),
            ticker=body.ticker.upper().strip(),
            strike=body.strike.upper().strip(),
            expiry=body.expiry.strip(),
            price_text=price_text,
            author_name=author_name,
            author_icon_url=os.getenv("CHIEF_TRADE_AUTHOR_ICON") or None,
            idempotency_key=body.idempotency_key,
        )
        return result

    result = await _run_on_bot_loop(_execute())
    if not result.get("ok"):
        status = result.pop("status", 400)
        raise HTTPException(status_code=status, detail=result.get("error", "trade rejected"))
    return result
