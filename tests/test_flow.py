"""Прогон сценария из ТЗ без Telegram: python3 tests/test_flow.py"""
import asyncio, json, os, sqlite3, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("telethon", types.SimpleNamespace(TelegramClient=None, events=None, utils=None))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from signalbot import config
tmp = tempfile.mkdtemp()

# база старого формата (один источник/канал в kv) должна перейти в перенаправление №1
old = sqlite3.connect(os.path.join(tmp, "t.db"))
old.executescript("""
CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, src_chat INTEGER NOT NULL, src_msg_id INTEGER NOT NULL,
 dst_chat INTEGER NOT NULL, dst_msg_id INTEGER, data TEXT NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', tps_hit TEXT NOT NULL DEFAULT '[]', be_moved INTEGER NOT NULL DEFAULT 0,
 cancel_due TEXT, UNIQUE (src_chat, src_msg_id));
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL, kind TEXT NOT NULL,
 r TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO kv VALUES ('source','-1002'),('source_title','ладес'),('target','-1001'),('target_title','аврум');
""")
old.commit(); old.close()
config.DB_PATH = os.path.join(tmp, "t.db")
from signalbot import main as m
from signalbot.logic import classify_reply

assert [(r["id"], r["source"], r["target"]) for r in m.db.routes()] == [(1, -1002, -1001)]
SRC = -1002

sent = []
class FakeBot:
    n = 100
    async def send(self, chat, text, reply_to=None, markup=None):
        FakeBot.n += 1; sent.append((chat, reply_to, text)); return FakeBot.n
    async def edit(self, *a): pass
m.bot = FakeBot()

class Msg:
    def __init__(self, id, text, reply=None):
        self.id, self.message = id, text
        self.reply_to = types.SimpleNamespace(reply_to_msg_id=reply) if reply else None

SIGNAL = """SELL XAUUSD 

📍4018-4020

✅Take profit 1: 4014
✅Take profit 2: 4010
✅Take profit 3: 4005
✅Take profit 4: 3980

❗️Stop loss (SL): 4030"""
BE = "Позицию переставляем в безубыточную цену ( на вашу изначальную точку входа ) не жадничаем\n\nКому баланс и риск-менеджмент позволяет, можете не ставить БУ🫱🏻‍🫲🏼"

async def run():
    for msg in [Msg(1, SIGNAL), Msg(2, "✅TP1  - 4014 XAUUSD, +400 point", 1), Msg(3, BE, 1),
                Msg(4, "✅TP2  - 4010 XAUUSD, +800 points", 1),
                Msg(41, "🚨Измените TP3 4000", 1), Msg(42, "✅TP3  - 4000 XAUUSD, +1900 points", 1),
                Msg(5, "🚨 Отстопило 4000", 1),
                Msg(6, SIGNAL.replace("SELL", "BUY").replace("4030", "4008")),  # будет отменён
                Msg(7, "смотрите какой график"),
                Msg(8, SIGNAL), Msg(9, "🚨 Отстопило 4030", 8),
                Msg(10, SIGNAL), Msg(11, BE, 10)]:  # безубыток без тейков — не отменяется
        await m.handle_new(SRC, msg)
    await m.daily_job(m.now().date())
    print(m.admin_command("/period 01.10.2026 31.12.2026"))
    for chat, rep, text in sent:
        print(f"--- в {chat}, ответ на {rep}\n{text}")
    assert [r["src_msg_id"] for r in m.db.due_cancels("9999")] == [6]
    assert ("-1001", "🚨 меняем TP3 на 4000") in [(str(c), t) for c, _, t in sent]
    assert any(t == "🔥 TP3 взят - 4000 GOLD, +0.43R" for _, _, t in sent)  # 19/11=1.73 -> /4 = 0.43
    assert classify_reply("🚨Измените TP3 4106") == ("chg", 3, 4106)
    assert classify_reply("🚨Измените TP3 - 4106")[0] == "chg"

    # второе перенаправление из того же источника в другой канал
    r2 = m.db.add_route(SRC, "ладес", -1005, "второй", m.now().isoformat())
    sent.clear()
    await m.handle_new(SRC, Msg(20, SIGNAL))
    await m.handle_new(SRC, Msg(21, "✅TP1  - 4014 XAUUSD, +400 point", 20))
    assert sorted(c for c, _, _ in sent) == [-1005, -1005, -1001, -1001], sent
    await m.handle_new(-1009, Msg(22, SIGNAL))  # чужой канал — ничего
    assert len(sent) == 4

    # смена источника у №1 обнуляет его статистику, №2 не трогает
    async def fake_src(ref): return ref, "новый"
    m.resolve_source = fake_src
    before2 = m.db.results("2000-01-01", "2100-01-01", r2)
    text, _ = await m.apply_channel(777, "src", "1", -1003)
    assert "обнулена" in text, text
    routes = {r["source"]: r for r in m.db.routes()}
    assert set(routes) == {-1002, -1003} and routes[-1003]["target"] == -1001
    assert m.db.results("2000-01-01", "2100-01-01", routes[-1003]["id"]) == {}
    assert m.db.results("2000-01-01", "2100-01-01", r2) == before2
    assert m.db.open_signals(1) == []  # старые сигналы №1 больше не отслеживаются
    try:
        await m.apply_channel(777, "src", str(r2), -1005)
        raise AssertionError("источник = канал публикации")
    except m.Refuse:
        pass

    for ref, want in [("-1001234567890", -1001234567890), ("1234567890", -1001234567890),
                      ("https://t.me/c/1234567890/55", -1001234567890), ("@goldsignals", "@goldsignals"),
                      ("t.me/goldsignals", "@goldsignals"), ("привет", None)]:
        assert m.parse_chat_ref(ref) == want, (ref, m.parse_chat_ref(ref))
    print(m.panel()[0])
    print(m.route_screen(r2)[0])
    from signalbot.logic import parse_signal, format_signal
    nfp = parse_signal("""⚠️ NEWS ALERT⚠️
High volatility expected. Once TP2 is hit, secure your trade and move SL to BE fast

NFP BUY STOP LIMIT XAUUSD 

📍4160

☑ Take profit 1: 4155
☑ Take profit 2: 4150
☑ Take profit 3: 4140
☑ Take profit 4: 4110

❗️Stop loss (SL): 4180""")
    assert (nfp.symbol, nfp.direction, nfp.entry, nfp.tps[3]) == ("XAUUSD", "SELL", 4160, 4110), nfp
    print(format_signal(nfp))
    assert parse_signal("XAUUSD BUY 4000\nTP1: 4010\nSL: 3990").symbol == "XAUUSD"
    assert parse_signal(SIGNAL).direction == "SELL"
    assert parse_signal("Измените TP3 - 4106") is None
    print("OK")

asyncio.run(run())
