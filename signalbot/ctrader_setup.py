"""Однократная настройка цены FxPro: python -m signalbot.ctrader_setup

Спрашивает токены из Playground на openapi.ctrader.com, показывает ваши счета cTrader,
проверяет котировку золота и записывает CTRADER_* в .env.
"""
import asyncio
import re
from decimal import Decimal

import aiohttp

from . import config
from .prices import (ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES, ACCOUNTS_BY_TOKEN_REQ, ACCOUNTS_BY_TOKEN_RES,
                     APP_AUTH_REQ, APP_AUTH_RES, PRICE_SCALE, SPOT_EVENT, SUBSCRIBE_SPOTS_REQ,
                     OpenAPI, ws_url)


def write_env(values: dict):
    env = config.BASE / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    for k, v in values.items():
        line = f"{k}={v}"
        if re.search(rf"^{k}=.*$", text, re.M):
            text = re.sub(rf"^{k}=.*$", lambda _: line, text, flags=re.M)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    env.write_text(text, encoding="utf-8")


async def connect(http, live):
    ws = await http.ws_connect(ws_url(live), heartbeat=25)
    api = OpenAPI(ws)
    await api.request(APP_AUTH_REQ, APP_AUTH_RES,
                      clientId=config.CTRADER_CLIENT_ID, clientSecret=config.CTRADER_CLIENT_SECRET)
    return ws, api


async def main():
    if not (config.CTRADER_CLIENT_ID and config.CTRADER_CLIENT_SECRET):
        raise SystemExit("Сначала впишите CTRADER_CLIENT_ID и CTRADER_CLIENT_SECRET в .env "
                         "(openapi.ctrader.com -> Applications -> Credentials)")
    print("На openapi.ctrader.com -> Applications -> Playground выберите scope «accounts»,\n"
          "нажмите Get token и войдите под cTrader ID, к которому привязан счёт FxPro.\n")
    access = input("Access token: ").strip()
    refresh = input("Refresh token: ").strip()

    async with aiohttp.ClientSession() as http:
        ws, api = await connect(http, True)
        res = await api.request(ACCOUNTS_BY_TOKEN_REQ, ACCOUNTS_BY_TOKEN_RES, accessToken=access)
        await ws.close()
        accounts = res.get("ctidTraderAccount", [])
        if not accounts:
            raise SystemExit("К этому cTrader ID не привязано ни одного счёта")
        print("\nСчета:")
        for i, a in enumerate(accounts, 1):
            kind = "реальный" if a.get("isLive") else "демо"
            print(f"  {i}. {a.get('brokerTitleShort', '?')}  счёт {a.get('traderLogin')}  ({kind})")
        n = int(input("\nНомер счёта FxPro: ").strip())
        acc = accounts[n - 1]
        if "fxpro" not in str(acc.get("brokerTitleShort", "")).lower():
            print("⚠️ Похоже, это не FxPro — бот будет брать цену этого брокера.")
        account_id, live = int(acc["ctidTraderAccountId"]), bool(acc.get("isLive"))

        ws, api = await connect(http, live)
        await api.request(ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES,
                          ctidTraderAccountId=account_id, accessToken=access)
        symbol = config.FXPRO_SYMBOL.upper()
        sid = await api.find_symbol(account_id, symbol)
        if sid is None:
            await ws.close()
            raise SystemExit(f"Символ {symbol} не найден на счёте — укажите другой в FXPRO_SYMBOL")
        await api.send(SUBSCRIBE_SPOTS_REQ, ctidTraderAccountId=account_id, symbolId=[sid])
        print(f"\nЖду котировку {symbol} (в выходные рынок закрыт — может не прийти)...")
        try:
            p = await asyncio.wait_for(api.wait(SPOT_EVENT), 30)
            bid = Decimal(p.get("bid", 0)) / PRICE_SCALE
            ask = Decimal(p.get("ask", 0)) / PRICE_SCALE
            print(f"{symbol} у FxPro: bid {bid}  ask {ask}")
        except asyncio.TimeoutError:
            print("Котировка за 30 с не пришла; подписка при этом работает.")
        await ws.close()

    write_env({"CTRADER_ACCESS_TOKEN": access, "CTRADER_REFRESH_TOKEN": refresh,
               "CTRADER_ACCOUNT_ID": account_id, "CTRADER_LIVE": int(live)})
    token_file = config.BASE / "data" / "ctrader_token.json"
    if token_file.exists():
        token_file.unlink()  # старые токены больше не нужны
    print("\nГотово: данные записаны в .env. Перезапустите бота: systemctl restart signalbot")


if __name__ == "__main__":
    asyncio.run(main())
