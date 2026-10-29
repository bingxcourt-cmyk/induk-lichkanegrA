"""Текущая цена у брокера FxPro через cTrader Open API (JSON по WebSocket).

Нужен счёт FxPro cTrader (подойдёт и демо) и приложение на openapi.ctrader.com.
Настройка: python -m signalbot.ctrader_setup. Без этих данных цена не берётся,
и бот пишет «Вход по рынку» — цену другого брокера мы не подставляем.
"""
import asyncio
import json
import logging
import time
from decimal import Decimal
from pathlib import Path

import aiohttp

log = logging.getLogger("prices")

# payloadType из OpenApiMessages.proto / OpenApiCommonMessages.proto
HEARTBEAT = 51
APP_AUTH_REQ, APP_AUTH_RES = 2100, 2101
ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES = 2102, 2103
SYMBOLS_LIST_REQ, SYMBOLS_LIST_RES = 2114, 2115
SUBSCRIBE_SPOTS_REQ = 2127
SPOT_EVENT = 2131
ERROR_RES = 2142
TOKEN_INVALIDATED = 2147
ACCOUNTS_BY_TOKEN_REQ, ACCOUNTS_BY_TOKEN_RES = 2149, 2150

TOKEN_URL = "https://openapi.ctrader.com/apps/token"
PRICE_SCALE = Decimal(100000)  # цены в Open API — целые в 1/100000
MAX_AGE = 15 * 60              # котировка старше 15 минут считается неизвестной
REFRESH_BEFORE = 3 * 86400     # обновляем токен за 3 дня до истечения


class PriceUnavailable(Exception):
    pass


def ws_url(live: bool) -> str:
    return f"wss://{'live' if live else 'demo'}.ctraderapi.com:5036"


class OpenAPI:
    """Соединение с cTrader Open API: отправка запроса и ожидание ответа нужного типа."""

    def __init__(self, ws):
        self.ws = ws

    async def send(self, ptype, **payload):
        await self.ws.send_str(json.dumps({"payloadType": ptype, "payload": payload}))

    async def wait(self, ptype) -> dict:
        async for m in self.ws:
            data = json.loads(m.data)
            if data.get("payloadType") in (ERROR_RES, TOKEN_INVALIDATED):
                raise RuntimeError(data.get("payload"))
            if data.get("payloadType") == ptype:
                return data.get("payload", {})
        raise RuntimeError("соединение закрыто")

    async def request(self, ptype, res_type, **payload) -> dict:
        await self.send(ptype, **payload)
        return await self.wait(res_type)

    async def find_symbol(self, account_id: int, name: str):
        symbols = (await self.request(SYMBOLS_LIST_REQ, SYMBOLS_LIST_RES,
                                      ctidTraderAccountId=account_id)).get("symbol", [])
        return next((s["symbolId"] for s in symbols if s.get("symbolName", "").upper() == name), None)


async def refresh_token(http, client_id, client_secret, refresh: str) -> dict:
    """{'accessToken', 'refreshToken', 'expiresIn', ...}; refresh-токен бессрочный."""
    params = {"grant_type": "refresh_token", "refresh_token": refresh,
              "client_id": client_id, "client_secret": client_secret}
    async with http.get(TOKEN_URL, params=params) as r:
        data = await r.json(content_type=None)
    if not data.get("accessToken"):
        raise RuntimeError(f"не удалось обновить токен: {data.get('description') or data}")
    return data


class NoPrices:
    """Заглушка, пока FxPro не настроен."""

    async def start(self):
        log.warning("цена FxPro не настроена (python -m signalbot.ctrader_setup) — вход будет «по рынку»")

    async def get(self, symbol: str, direction: str) -> Decimal:
        raise PriceUnavailable("FxPro не настроен")


class CTraderPrices:
    def __init__(self, client_id, client_secret, access_token, refresh, account_id,
                 symbol="XAUUSD", live=True, token_file=None):
        self.url = ws_url(live)
        self.client_id, self.client_secret = client_id, client_secret
        self.account_id = int(account_id)
        self.symbol = symbol.upper()
        self.token_file = Path(token_file) if token_file else None
        # Свежие токены после обновления лежат в data/, а не в .env
        self.token = {"accessToken": access_token, "refreshToken": refresh, "expiresAt": 0}
        if self.token_file and self.token_file.exists():
            self.token.update(json.loads(self.token_file.read_text()))
        self.bid = self.ask = None
        self.updated = 0.0

    async def start(self):
        asyncio.create_task(self._run())

    async def get(self, symbol: str, direction: str) -> Decimal:
        """Цена входа: ask для BUY, bid для SELL."""
        if symbol.upper() not in (self.symbol, "XAUUSD", "GOLD"):
            raise PriceUnavailable(f"котировка {symbol} не подписана")
        price = (self.ask if direction == "BUY" else self.bid) or self.bid or self.ask
        if price is None or time.time() - self.updated > MAX_AGE:
            raise PriceUnavailable("нет свежей котировки FxPro")
        return price

    async def _refresh(self, http, force=False):
        if not self.token.get("refreshToken"):
            return
        if not force and self.token.get("expiresAt", 0) - time.time() > REFRESH_BEFORE:
            return
        data = await refresh_token(http, self.client_id, self.client_secret, self.token["refreshToken"])
        self.token = {"accessToken": data["accessToken"],
                      "refreshToken": data.get("refreshToken") or self.token["refreshToken"],
                      "expiresAt": time.time() + int(data.get("expiresIn") or 2628000)}
        if self.token_file:
            self.token_file.write_text(json.dumps(self.token))
        log.info("cTrader: токен обновлён")

    async def _run(self):
        failures = 0
        while True:
            try:
                async with aiohttp.ClientSession() as http:
                    await self._refresh(http, force=failures >= 2)
                    await self._session(http)
                failures = 0
            except Exception as e:
                failures += 1
                log.warning("cTrader: %s, переподключение через 10 с", e)
            await asyncio.sleep(10)

    async def _session(self, http):
        async with http.ws_connect(self.url, heartbeat=25) as ws:
            api = OpenAPI(ws)
            await api.request(APP_AUTH_REQ, APP_AUTH_RES,
                              clientId=self.client_id, clientSecret=self.client_secret)
            await api.request(ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES, ctidTraderAccountId=self.account_id,
                              accessToken=self.token["accessToken"])
            sid = await api.find_symbol(self.account_id, self.symbol)
            if sid is None:
                raise RuntimeError(f"символ {self.symbol} не найден на счёте")
            await api.send(SUBSCRIBE_SPOTS_REQ, ctidTraderAccountId=self.account_id, symbolId=[sid])
            log.info("cTrader: подписка на %s (id %s)", self.symbol, sid)

            async def ping():
                while True:
                    await asyncio.sleep(10)
                    await api.send(HEARTBEAT)
                    if (self.token.get("refreshToken")
                            and self.token.get("expiresAt", 0) - time.time() < REFRESH_BEFORE):
                        await ws.close()  # переподключимся уже с новым токеном

            pinger = asyncio.create_task(ping())
            try:
                async for m in ws:
                    data = json.loads(m.data)
                    if data.get("payloadType") in (ERROR_RES, TOKEN_INVALIDATED):
                        raise RuntimeError(data.get("payload"))
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
                             config.CTRADER_ACCESS_TOKEN, config.CTRADER_REFRESH_TOKEN,
                             config.CTRADER_ACCOUNT_ID, config.FXPRO_SYMBOL, config.CTRADER_LIVE,
                             config.CTRADER_TOKEN_FILE)
    return NoPrices()
