"""HTTP ingest for desk-automated trades (0DTE Command → Discord alerts + PnL)."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import bot

app = FastAPI(title="Chief Trade Alert Bot API", version="1.0.0")


class TradeIn(BaseModel):
    action: str = Field(description="BTO | STO | STC | BTC")
    qty: int = 1
    ticker: str
    strike: str
    expiry: str
    price: float = Field(gt=0)
    idempotency_key: Optional[str] = None
    author_name: Optional[str] = None


@app.get("/health")
async def health():
    return {"ok": True, "bot_ready": bot.bot.is_ready() if bot.bot else False}


@app.post("/api/trade")
async def post_trade(body: TradeIn, authorization: Optional[str] = Header(None)):
    secret = (bot.API_SECRET or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="API not configured")
    if authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not bot.bot.is_ready():
        raise HTTPException(status_code=503, detail="Discord bot not ready")

    channel_id = bot.API_CHANNEL_ID
    if not channel_id:
        raise HTTPException(status_code=503, detail="CHIEF_TRADE_CHANNEL_ID not set")

    channel = bot.bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.bot.fetch_channel(channel_id)
    if channel is None:
        raise HTTPException(status_code=503, detail=f"Channel {channel_id} not found")

    user_id = bot.resolve_desk_user_id()
    if not user_id:
        raise HTTPException(status_code=503, detail="Discord bot user not available")

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

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "trade rejected"))

    return result
