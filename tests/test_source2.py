"""Источник 2 (Forex Bulls) без Telegram: python3 tests/test_source2.py"""
import asyncio, os, sys, tempfile, types
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from signalbot.db import DB
from signalbot.forexbulls import (BOT_LINK, STOP_TEXT, classify_reply, format_close, format_signal,
                                  parse_signal, profit_pct)
from signalbot.prices import NoPrices
from signalbot.settings import Settings
from signalbot.source2 import Source2

SIGNAL = "BUY XAUUSD\nSL 4019.957\nTP 4068.642\nEnsure proper risk managment"
LINK = f'в <a href="{BOT_LINK}">этом</a> боте отображено точное количество лотов, на которое ' \
       "нужно открыть позицию, дабы при стопе ты потерял ровно 1% от своего депозита"


def test_parse():
    for text in (SIGNAL, "BUY XAUUSD / SL 4019.957 / TP 4068.642 / Ensure proper risk managment"):
        s = parse_signal(text)
        assert (s.direction, s.symbol, s.sl, s.tp) == ("BUY", "XAUUSD", Decimal("4019.957"), Decimal("4068.642"))
    s = parse_signal("SELL XAUUSD\nSL 4080.5\nTP 4001.123")
    assert (s.direction, s.sl, s.tp) == ("SELL", Decimal("4080.5"), Decimal("4001.123"))
    for junk in ("HELLO TEAM", "BUY XAUUSD AGAIN NOW", "Close off profits", "", "BUY XAUUSD"):
        assert parse_signal(junk) is None, junk


def test_classify():
    assert classify_reply("BUY XAUUSD AGAIN NOW") == ("again", "BUY")
    assert classify_reply("SELL XAUUSD AGAIN NOW") == ("again", "SELL")
    assert classify_reply("Close off profits") == ("close",)
    assert classify_reply("hold the buys") == ("other",)
    assert classify_reply("join us https://t.me/xxx") is None
    assert classify_reply("🔥🔥🔥") is None
    assert classify_reply("") is None


def test_format():
    s = parse_signal(SIGNAL)
    text = format_signal(s.direction, s.symbol, Decimal("4031.4"), s.sl, s.tp, "Заголовок <1>")
    assert text == ("Заголовок &lt;1&gt;\n#GOLD LONG\nВход 4031.40\nSL: 4019.96\nTakeprofit: 4068.64\n\n" + LINK)
    short = format_signal("SELL", "XAUUSD", Decimal("4050.005"), Decimal("4080.5"), Decimal("4001.123"))
    assert short.startswith("#GOLD SHORT\nВход 4050.01\nSL: 4080.50\nTakeprofit: 4001.12\n")
    assert "Вход по рынку" in format_signal("BUY", "XAUUSD", None, s.sl, s.tp)


def test_profit():
    # вход 4030, SL 4020 (риск 10), цена 4045 -> 1.5R -> +1.5%
    assert profit_pct(Decimal(4030), Decimal(4020), Decimal(4045)) == Decimal("1.5")
    assert format_close(Decimal(4030), Decimal(4020), Decimal(4045)) == \
        "закрываем сделку в профит по текущей цене. +1.5% к депозиту"
    assert format_close(Decimal("4031.40"), Decimal("4019.957"), Decimal("4040")) == \
        "закрываем сделку в профит по текущей цене. +0.75% к депозиту"
    assert format_close(None, Decimal(4020), None) == "закрываем сделку в профит по текущей цене."


# ------------------------------------------------------- сценарий целиком

class FakeBot:
    def __init__(self):
        self.n, self.sent = 100, []

    async def send(self, chat, text, reply_to=None, html=False, markup=None):
        self.n += 1
        self.sent.append((chat, reply_to, text))
        return self.n


class FakePrices:
    price = Decimal("4031.4")

    async def get(self, symbol, direction):
        return self.price


class FakeTranslator:
    async def translate(self, text):
        return {"hold the buys": "держим покупки"}.get(text)


class Msg:
    def __init__(self, id, text, reply=None, media=None):
        self.id, self.message, self.media = id, text, media
        self.reply_to = types.SimpleNamespace(reply_to_msg_id=reply) if reply else None


def test_flow():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    settings = Settings(db)
    settings.set_chat(2, "target", -1002, "мой канал")
    settings.set_header("ЗАГОЛОВОК")
    bot, fx = FakeBot(), FakePrices()
    s2 = Source2(bot, db, settings, fx, FakeTranslator(), datetime.now)

    async def run():
        await s2.handle(Msg(1, "HELLO TEAM"))
        await s2.handle(Msg(2, "", media=object()))
        await s2.handle(Msg(3, SIGNAL))                       # -> 101
        await s2.handle(Msg(3, SIGNAL))                       # дубль не шлём
        await s2.handle(Msg(4, "hold the buys", 3))           # -> 102
        await s2.handle(Msg(5, "Good morning team", 3))       # перевод вернул None -> пропуск
        await s2.handle(Msg(6, "look", 3, media=object()))    # медиа в реплае
        fx.price = Decimal("4010")
        await s2.handle(Msg(7, "BUY XAUUSD AGAIN NOW", 3))    # стоп -> 103, новый сигнал -> 104
        fx.price = Decimal("4040")
        await s2.handle(Msg(8, "Close off profits", 7))       # -> 105 на новый сигнал
        await s2.handle(Msg(9, "Close off profits", 3))       # уже закрыт
    asyncio.run(run())

    assert [(c, r) for c, r, _ in bot.sent] == [(-1002, None), (-1002, 101), (-1002, 101),
                                                 (-1002, None), (-1002, 104)], bot.sent
    assert bot.sent[0][2].startswith("ЗАГОЛОВОК\n#GOLD LONG\nВход 4031.40\nSL: 4019.96\nTakeprofit: 4068.64")
    assert bot.sent[1][2] == "держим покупки"
    assert bot.sent[2][2] == STOP_TEXT
    assert bot.sent[3][2].startswith("ЗАГОЛОВОК\n#GOLD LONG\nВход 4010.00\nSL: 4019.96\nTakeprofit: 4068.64")
    # вход 4010, SL 4019.957: риск 9.957, прибыль 30 -> 3.01%
    assert bot.sent[4][2] == "закрываем сделку в профит по текущей цене. +3.01% к депозиту"
    row = db.s2_by_src(3)
    assert row["dst_msg_id"] == 101 and row["entry"] == "4031.4" and row["status"] == "stopped"
    assert db.s2_latest(3)["dst_msg_id"] == 104 and db.s2_latest(3)["status"] == "profit"


def test_no_price():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    settings = Settings(db)
    settings.set_chat(2, "target", -1002, "мой канал")
    bot = FakeBot()
    s2 = Source2(bot, db, settings, NoPrices(), FakeTranslator(), datetime.now, admin_id=7)

    async def run():
        await s2.handle(Msg(1, SIGNAL))
        await s2.handle(Msg(2, "Close off profits", 1))
    asyncio.run(run())
    targets = [m for m in bot.sent if m[0] == -1002]
    assert targets[0][2].startswith("#GOLD LONG\nВход по рынку\n")
    assert targets[1][2] == "закрываем сделку в профит по текущей цене."
    assert any(m[0] == 7 for m in bot.sent)  # админ предупреждён


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
