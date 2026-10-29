"""Текущая цена у брокера FxPro через cTrader Open API (JSON по WebSocket).

Нужен счёт FxPro cTrader (подойдёт и демо) и приложение на openapi.ctrader.com.
Без этих данных цена не берётся, и бот пишет «Вход по рынку» — цену другого
брокера мы не подставляем.
"""
import asyncio
import json
import logging
import time
from decimal import Decimal

import aiohttp

log = logging.getLogger("prices")

# payloadType из OpenApiMessages.proto
HEARTBEAT = 51
APP_AUTH_REQ, APP_AUTH_RES = 2100, 2101
ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES = 2102, 2103
SYMBOLS_LIST_REQ, SYMBOLS_LIST_RES = 2114, 2115
SUBSCRIBE_SPOTS_REQ = 2127
SPOT_EVENT = 2131
ERROR_RES = 2142

PRICE_SCALE = Decimal(100000)  # цены в Open API — целые в 1/100000
MAX_AGE = 15 * 60              # котировка старше 15 минут считается неизвестной


class PriceUnavailable(Exception):
    pass


class NoPrices:
    """Заглушка, пока FxPro не настроен."""

    async def start(self):
        log.warning("цена FxPro не настроена (CTRADER_* в .env) — вход будет «по рынку»")

    async def get(self, symbol: str, direction: str) -> Decimal:
        raise PriceUnavailable("FxPro не настроен")


class CTraderPrices:
    def __init__(self, client_id, client_secret, access_token, account_id,
                 symbol="XAUUSD", live=True):
        host = "live" if live else "demo"
        self.url = f"wss://{host}.ctraderapi.com:5036"
        self.client_id, self.client_secret = client_id, client_secret
        self.access_token, self.account_id = access_token, int(account_id)
        self.symbol = symbol.upper()
        self.bid = self.ask = None
        self.updated = 0.0

    async def start(self):
        asyncio.create_task(self._run())

    async def get(self, symbol: str, direction: str) -> Decimal:
        """Цена входа: ask для BUY, bid для SELL."""
        if symbol.upper() not in (self.symbol, "XAUUSD", "GOLD"):
            raise PriceUnavailable(f"котировка {symbol} не подписана")
        price = self.ask if direction == "BUY" else self.bid
        price = price or self.bid or self.ask
        if price is None or time.time() - self.updated > MAX_AGE:
            raise PriceUnavailable("нет свежей котировки FxPro")
        return price

    async def _run(self):
        while True:
            try:
                await self._session()
            except Exception as e:
                log.warning("cTrader: %s, переподключение через 10 с", e)
            await asyncio.sleep(10)

    async def _session(self):
        async with aiohttp.ClientSession() as http, http.ws_connect(self.url, heartbeat=25) as ws:
            async def send(ptype, **payload):
                await ws.send_str(json.dumps({"payloadType": ptype, "payload": payload}))

            async def wait(ptype):
                async for m in ws:
                    data = json.loads(m.data)
                    if data.get("payloadType") == ERROR_RES:
                        raise RuntimeError(data.get("payload"))
                    if data.get("payloadType") == ptype:
                        return data.get("payload", {})
                raise RuntimeError("соединение закрыто")

            await send(APP_AUTH_REQ, clientId=self.client_id, clientSecret=self.client_secret)
            await wait(APP_AUTH_RES)
            await send(ACCOUNT_AUTH_REQ, ctidTraderAccountId=self.account_id,
                       accessToken=self.access_token)
            await wait(ACCOUNT_AUTH_RES)
            await send(SYMBOLS_LIST_REQ, ctidTraderAccountId=self.account_id)
            symbols = (await wait(SYMBOLS_LIST_RES)).get("symbol", [])
            sid = next((s["symbolId"] for s in symbols
                        if s.get("symbolName", "").upper() == self.symbol), None)
            if sid is None:
                raise RuntimeError(f"символ {self.symbol} не найден на счёте")
            await send(SUBSCRIBE_SPOTS_REQ, ctidTraderAccountId=self.account_id, symbolId=[sid])
            log.info("cTrader: подписка на %s (id %s)", self.symbol, sid)

            async def ping():
                while True:
                    await asyncio.sleep(10)
                    await send(HEARTBEAT)

            pinger = asyncio.create_task(ping())
            try:
                async for m in ws:
                    data = json.loads(m.data)
                    if data.get("payloadType") != SPOT_EVENT:
                        continue
                    p = data.get("payload", {})
                    if "bid" in p:
                        self.bid = Decimal(p["bid"]) / PRICE_SCALE
                    if "ask" in p:
                        self.ask = Decimal(p["ask"]) / PRICE_SCALE
                    self.updated = time.time()
            finally:
                pinger.cancel()


def from_config(config):
    if config.CTRADER_CLIENT_ID and config.CTRADER_ACCESS_TOKEN and config.CTRADER_ACCOUNT_ID:
        return CTraderPrices(config.CTRADER_CLIENT_ID, config.CTRADER_CLIENT_SECRET,
                             config.CTRADER_ACCESS_TOKEN, config.CTRADER_ACCOUNT_ID,
                             config.FXPRO_SYMBOL, config.CTRADER_LIVE)
    return NoPrices()
