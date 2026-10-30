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

    async def symbol_ids(self, account_id: int) -> dict:
        """{'XAUUSD': id, …} — все символы счёта."""
        symbols = (await self.request(SYMBOLS_LIST_REQ, SYMBOLS_LIST_RES,
                                      ctidTraderAccountId=account_id)).get("symbol", [])
        return {s.get("symbolName", "").upper(): s["symbolId"] for s in symbols}

    async def find_symbol(self, account_id: int, name: str):
        return (await self.symbol_ids(account_id)).get(name)


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
                 symbols=("XAUUSD",), live=True, token_file=None):
        self.url = ws_url(live)
        self.client_id, self.client_secret = client_id, client_secret
        self.account_id = int(account_id)
        self.symbols = [s.upper() for s in symbols]
        self.token_file = Path(token_file) if token_file else None
        # Свежие токены после обновления лежат в data/, а не в .env
        self.token = {"accessToken": access_token, "refreshToken": refresh, "expiresAt": 0}
        if self.token_file and self.token_file.exists():
            self.token.update(json.loads(self.token_file.read_text()))
        self.quotes = {}  # 'XAUUSD' -> {'bid', 'ask', 'updated'}

    async def start(self):
        asyncio.create_task(self._run())

    async def get(self, symbol: str, direction: str) -> Decimal:
        """Цена входа: ask для BUY, bid для SELL."""
        q = self.quotes.get(symbol.upper())
        if q is None:
            raise PriceUnavailable(f"котировка {symbol} у FxPro не подписана")
        price = (q.get("ask") if direction == "BUY" else q.get("bid")) or q.get("bid") or q.get("ask")
        if price is None or time.time() - q["updated"] > MAX_AGE:
            raise PriceUnavailable(f"нет свежей котировки {symbol} у FxPro")
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
            ids = await api.symbol_ids(self.account_id)
            names = {ids[s]: s for s in self.symbols if s in ids}
            missing = [s for s in self.symbols if s not in ids]
            if missing:
                log.warning("cTrader: на счёте нет символов %s", ", ".join(missing))
            if not names:
                raise RuntimeError("ни одного символа для подписки")
            await api.send(SUBSCRIBE_SPOTS_REQ, ctidTraderAccountId=self.account_id, symbolId=list(names))
            log.info("cTrader: подписка на %s", ", ".join(names.values()))

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
                    name = names.get(p.get("symbolId"))
                    if name is None:
                        continue
                    q = self.quotes.setdefault(name, {})
                    for side in ("bid", "ask"):
                        if side in p:
                            q[side] = Decimal(p[side]) / PRICE_SCALE
                    q["updated"] = time.time()
            finally:
                pinger.cancel()


class OandaPrices:
    """Цена OANDA через v20 REST API (токен из личного кабинета, подойдёт демо-счёт).

    Это НЕ цена FxPro: включается, только если задан OANDA_TOKEN, и пока не настроен cTrader.
    """

    def __init__(self, token, live=False):
        host = "api-fxtrade.oanda.com" if live else "api-fxpractice.oanda.com"
        self.base = f"https://{host}/v3"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.account = None

    async def start(self):
        log.info("цена берётся у OANDA, не у FxPro")

    async def get(self, symbol: str, direction: str) -> Decimal:
        """Цена входа: ask для BUY, bid для SELL."""
        if not pair_ok(symbol):
            raise PriceUnavailable(f"котировка {symbol} не настроена")
        instrument = f"{symbol[:3]}_{symbol[3:]}".upper()
        try:
            async with aiohttp.ClientSession(headers=self.headers,
                                             timeout=aiohttp.ClientTimeout(total=15)) as http:
                if self.account is None:
                    async with http.get(f"{self.base}/accounts") as r:
                        self.account = (await r.json())["accounts"][0]["id"]
                async with http.get(f"{self.base}/accounts/{self.account}/pricing",
                                    params={"instruments": instrument}) as r:
                    data = await r.json()
            p = data["prices"][0]
            side = p["asks"] if direction == "BUY" else p["bids"]
            return Decimal(side[0]["price"])
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError) as e:
            raise PriceUnavailable(f"OANDA: {e!r}")


def pair_ok(symbol: str) -> bool:
    """Инструмент вида XAUUSD / GBPJPY — шесть букв, base+quote."""
    return len(symbol) == 6 and symbol.isalpha()


class TwelveDataPrices:
    """Цена у Twelve Data (общая рыночная, не конкретного брокера).

    Это НЕ цена FxPro: включается, только если задан TWELVEDATA_KEY, и пока не настроен cTrader.
    Отдаёт одну цену без bid/ask, поэтому для BUY и SELL она одинаковая.
    """
    URL = "https://api.twelvedata.com/price"

    def __init__(self, key):
        self.headers = {"Authorization": f"apikey {key}"}

    async def start(self):
        log.info("цена берётся у Twelve Data, не у FxPro")

    async def get(self, symbol: str, direction: str) -> Decimal:
        if not pair_ok(symbol):
            raise PriceUnavailable(f"котировка {symbol} не настроена")
        try:
            async with aiohttp.ClientSession(headers=self.headers,
                                             timeout=aiohttp.ClientTimeout(total=15)) as http:
                async with http.get(self.URL, params={"symbol": f"{symbol[:3]}/{symbol[3:]}".upper()}) as r:
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PriceUnavailable(f"Twelve Data: {e!r}")
        if "price" not in data:
            raise PriceUnavailable(f"Twelve Data: {data.get('message') or data}")
        return Decimal(data["price"])


def from_config(config):
    if config.CTRADER_CLIENT_ID and config.CTRADER_ACCESS_TOKEN and config.CTRADER_ACCOUNT_ID:
        return CTraderPrices(config.CTRADER_CLIENT_ID, config.CTRADER_CLIENT_SECRET,
                             config.CTRADER_ACCESS_TOKEN, config.CTRADER_REFRESH_TOKEN,
                             config.CTRADER_ACCOUNT_ID, config.FXPRO_SYMBOLS, config.CTRADER_LIVE,
                             config.CTRADER_TOKEN_FILE)
    if config.OANDA_TOKEN:
        return OandaPrices(config.OANDA_TOKEN, config.OANDA_LIVE)
    if config.TWELVEDATA_KEY:
        return TwelveDataPrices(config.TWELVEDATA_KEY)
    return NoPrices()
