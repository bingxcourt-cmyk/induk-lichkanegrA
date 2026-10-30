"""Правила «Forex Bulls» (источник 2) без Telegram: python3 tests/test_source2.py"""
import asyncio, os, sys, tempfile, types
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from signalbot.db import DB
from signalbot.forexbulls import (BOT_LINK, STOP_TEXT, classify_reply, format_close, format_signal,
                                  parse_signal, profit_pct)
from signalbot.prices import NoPrices
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
    # форматы из истории канала
    cases = {
        "SELLS ON XAUUSD.\n\nSL 4596.01\nTP 4512.12\n\nEnsure proper risk management": ("SELL", "4596.01", "4512.12"),
        "XAUUSD BUYS\nSL 4408.25\nTP 4435.00": ("BUY", "4408.25", "4435.00"),
        "One take for the week.\n\n\nBuy XAUUSD now\nSL 4052\nTP OPEN": ("BUY", "4052", None),
        "Let’s buy XAUUSD \nStop loss 4306.47\nTake profit; 4326.66": ("BUY", "4306.47", "4326.66"),
        "Short term buys on XAUUSD \nSL 4023.17\nTP 4035.12": ("BUY", "4023.17", "4035.12"),
        "xauusd sells\nsl 4070.63\n\nensure proper risk managment": ("SELL", "4070.63", None),
        "Buy gold\nSL 4351.687\n\nEnsure proper risk management": ("BUY", "4351.687", None),
        "PPI NEWS COMING UP SOON.\n\nExpecting sells\n\nStoploss 4160.576\nTP 4118.976": ("SELL", "4160.576", "4118.976"),
    }
    for text, (d, sl, tp) in cases.items():
        s = parse_signal(text)
        assert s and (s.direction, s.sl, s.tp) == (d, Decimal(sl), tp and Decimal(tp)), (text, s)
    # другие пары
    s = parse_signal("Sell GBPJPY \nStop loss ~ 215.738\ntake profit  ~215.15")
    assert (s.symbol, s.direction, s.sl, s.tp) == ("GBPJPY", "SELL", Decimal("215.738"), Decimal("215.15"))
    s = parse_signal("SELL GBPUSD\nSL 1.35466\nTP 1.34919")
    assert (s.symbol, s.sl, s.tp) == ("GBPUSD", Decimal("1.35466"), Decimal("1.34919"))
    assert parse_signal("GBPJPY buys.\n\nSL 216.622\nTP 217.195").symbol == "GBPJPY"
    for junk in ("HELLO TEAM", "BUY XAUUSD AGAIN NOW", "Close off profits", "", "BUY XAUUSD",
                 "Recovery setup 📈\n\nBUY XAGUSD (SILVER)\nSP 68.99489\nTP 69.96029",
                 "Expecting sells on XAUUSD", "BUY 4018.76\n\nTP 4100.88"):
        assert parse_signal(junk) is None, junk


def test_classify():
    assert classify_reply("BUY XAUUSD AGAIN NOW") == ("again", "BUY", None, None)
    assert classify_reply("SELL XAUUSD AGAIN NOW") == ("again", "SELL", None, None)
    assert classify_reply("Let’s buy XAUUSD again now")[:2] == ("again", "BUY")
    for text in ("Close off profits", "Close profits 💪🔥", "Close off on profits \n1:2.26RR",
                 "Close GBPUSD in profits", "Close off profits on GBPJPY",
                 "we may close off profits now (1:3RR) secured", "Close the trade in profits now 🚀🚀"):
        assert classify_reply(text) == ("close",), text
    for text in ("XAUUSD Went the opposite side.\n\nClose off", "setup invalidated on gold. Close off",
                 "Close the buys on XAUUSD and hold GJ BUYS", "our XAUUSD setup was invalidated"):
        assert classify_reply(text) == ("closeoff",), text
    for text in ("Our sl was hit", "SL triggered.\n\nLet’s go again next week 🤎💪!",
                 "Gold takes out with a loss.", "We’re taken out.\n\nWe shall go harder tomorrow"):
        assert classify_reply(text) == ("sl",), text
    for text in ("OUR TAKE PROFIT WAS HIT 🔥", "Take profit smashed 💪🔥", "fire trade; tp hit", "Our TP was hit 🔥"):
        assert classify_reply(text) == ("tp",), text
    assert classify_reply("Adjust the stop loss to 4188.24\nTP 4242.70") == ("move", Decimal("4188.24"), Decimal("4242.70"))
    assert classify_reply("Extend the SL to 4013.15 and hold the sells") == ("move", Decimal("4013.15"), None)
    assert classify_reply("Move SL to entry") == ("move", None, None)
    assert classify_reply("Re-entry;\n\nSell XAUUSD\n\nSame SL and TP") == ("reentry", "SELL", None, None)
    assert classify_reply("RE-ENTRY @current price \nStop loss: 4015.59") == ("reentry", None, Decimal("4015.59"), None)
    assert classify_reply("Sells re-entry")[:2] == ("reentry", "SELL")
    for text in ("hold the buys", "Running in profits 💪🔥", "Re-entry coming up in an hour 🙏🔥",
                 "Good morning team. Sending in a recovery setup.",
                 "Let’s close off partial profits team"):
        assert classify_reply(text) == ("other",), text
    for junk in ("join us https://t.me/xxx", "GOT PAID TODAY IN THE VIP", "Join VIP now", "DM me for premium",
                 "VIP members got 300 pips", "Getting paid 💯", "🔥🔥🔥", ""):
        assert classify_reply(junk) is None, junk


def test_format():
    s = parse_signal(SIGNAL)
    text = format_signal(s.direction, s.symbol, Decimal("4031.4"), s.sl, s.tp, "Заголовок <1>")
    assert text == ("Заголовок &lt;1&gt;\n\n#GOLD LONG\n\nВход 4031.40\n\nSL: 4019.96\n\nTakeprofit: 4068.64\n\n" + LINK)
    short = format_signal("SELL", "XAUUSD", Decimal("4050.005"), Decimal("4080.5"), Decimal("4001.123"))
    assert short.startswith("#GOLD SHORT\n\nВход 4050.01\n\nSL: 4080.50\n\nTakeprofit: 4001.12\n\n")
    assert "Вход по рынку" in format_signal("BUY", "XAUUSD", None, s.sl, s.tp)
    gj = format_signal("SELL", "GBPJPY", Decimal("215.4567"), Decimal("215.738"), Decimal("215.15"))
    assert gj.startswith("#GBPJPY SHORT\n\nВход 215.457\n\nSL: 215.738\n\nTakeprofit: 215.150\n\n"), gj
    gu = format_signal("BUY", "GBPUSD", Decimal("1.351234"), Decimal("1.34919"), None)
    assert gu.startswith("#GBPUSD LONG\n\nВход 1.35123\n\nSL: 1.34919\n\nTakeprofit: открытый"), gu


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

    async def send(self, chat, text, reply_to=None, markup=None, html=False):
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


def make_route(db, header=""):
    rid = db.add_route(-1001, "Forex Bulls Vip Trades", -1002, "мой канал", datetime.now().isoformat())
    db.update_route(rid, rules="forexbulls", header=header)
    return db.route(rid)


def test_flow():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    route = make_route(db, "ЗАГОЛОВОК")
    bot, fx = FakeBot(), FakePrices()
    s2 = Source2(bot, db, fx, FakeTranslator(), datetime.now)

    async def run():
        for msg in [Msg(1, "HELLO TEAM"),
                    Msg(2, "", media=object()),
                    Msg(3, SIGNAL),                       # -> 101
                    Msg(3, SIGNAL),                       # дубль не шлём
                    Msg(4, "hold the buys", 3),           # -> 102
                    Msg(5, "Good morning team", 3),       # перевод вернул None -> пропуск
                    Msg(6, "look", 3, media=object())]:   # медиа в реплае
            await s2.handle(route, msg)
        fx.price = Decimal("4010")
        await s2.handle(route, Msg(7, "BUY XAUUSD AGAIN NOW", 3))  # стоп -> 103, новый сигнал -> 104
        fx.price = Decimal("4040")
        await s2.handle(route, Msg(8, "Close off profits", 7))     # -> 105 на новый сигнал
        await s2.handle(route, Msg(9, "Close off profits", 3))     # уже закрыт
    asyncio.run(run())

    assert [(c, r) for c, r, _ in bot.sent] == [(-1002, None), (-1002, 101), (-1002, 101),
                                                 (-1002, None), (-1002, 104)], bot.sent
    assert bot.sent[0][2].startswith("ЗАГОЛОВОК\n\n#GOLD LONG\n\nВход 4031.40\n\nSL: 4019.96\n\nTakeprofit: 4068.64")
    assert bot.sent[1][2] == "держим покупки"
    assert bot.sent[2][2] == STOP_TEXT
    assert bot.sent[3][2].startswith("ЗАГОЛОВОК\n\n#GOLD LONG\n\nВход 4010.00\n\nSL: 4019.96\n\nTakeprofit: 4068.64")
    # вход 4010, SL 4019.957: риск 9.957, прибыль 30 -> 3.01%
    assert bot.sent[4][2] == "закрываем сделку в профит по текущей цене. +3.01% к депозиту"
    row = db.s2_by_src(route["id"], 3)
    assert row["dst_msg_id"] == 101 and row["entry"] == "4031.4" and row["status"] == "stopped"
    latest = db.s2_latest(route["id"], 3)
    assert latest["dst_msg_id"] == 104 and latest["status"] == "profit"


def test_threads_and_updates():
    """Реплай на реплай, сообщения без реплая, перенос стопа, тейк, перезаход."""
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    route = make_route(db)
    bot, fx = FakeBot(), FakePrices()
    s2 = Source2(bot, db, fx, FakeTranslator(), datetime.now)
    fx.price = Decimal("4000")

    async def run():
        await s2.handle(route, Msg(1, "BUY XAUUSD\nSL 3990\nTP 4030"))          # 101, вход 4000, риск 10
        await s2.handle(route, Msg(2, "Running in profits 💪", 1))                # перевод None -> тишина
        fx.price = Decimal("4015")
        await s2.handle(route, Msg(3, "Close profits now", 2))                    # реплай на реплай -> 102
        await s2.handle(route, Msg(4, "Sell XAUUSD\nSL 4010\nTP open"))         # 103, вход 4015
        await s2.handle(route, Msg(5, "Adjust the SL to 4015 and hold", 4))      # 104 (перевода нет)
        await s2.handle(route, Msg(6, "Our SL was hit"))                          # без реплая -> 105, 0%
        await s2.handle(route, Msg(7, "Buy gold\nSL 4005"))                      # 106
        await s2.handle(route, Msg(8, "Re-entry;\n\nBuy XAUUSD\n\nSame SL and TP", 7))  # 107 реплаем на 106
        fx.price = Decimal("4035")
        await s2.handle(route, Msg(9, "Take profit hit 🔥", 8))                   # тейк открытый -> по цене, 108
        await s2.handle(route, Msg(10, "Close profits"))                          # открытых нет -> тишина
    asyncio.run(run())
    # статистика в % депозита: +1.5, 0 (стоп в безубыток), +2 (тейк перезахода)
    res = db.results("2000-01-01", "2100-01-01", route["id"])
    assert sorted(str(v["r"]) for v in res.values()) == ["0.00", "1.50", "2.00"], res
    texts = [(r, t.split("\n")[0]) for _, r, t in bot.sent]
    assert texts == [
        (None, "#GOLD LONG"),
        (101, "закрываем сделку в профит по текущей цене. +1.5% к депозиту"),
        (None, "#GOLD SHORT"),
        (103, "переносим стоп-лосс на 4015.00"),
        (103, "сработал стоп-лосс в безубыток. 0% к депозиту"),
        (None, "#GOLD LONG"),
        (106, "#GOLD LONG"),
        (107, "🔥 тейк-профит взят. +2% к депозиту"),  # перезаход по 4015, SL 4005, тейк по цене 4035
    ], texts


def test_no_price():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    route = make_route(db)
    bot = FakeBot()
    s2 = Source2(bot, db, NoPrices(), FakeTranslator(), datetime.now, admin_id=7)

    async def run():
        await s2.handle(route, Msg(1, SIGNAL))
        await s2.handle(route, Msg(2, "Close off profits", 1))
    asyncio.run(run())
    targets = [m for m in bot.sent if m[0] == -1002]
    assert targets[0][2].startswith("#GOLD LONG\n\nВход по рынку\n\n")
    assert targets[1][2] == "закрываем сделку в профит по текущей цене."
    assert any(m[0] == 7 for m in bot.sent)  # админ предупреждён


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
