"""Прогон сценария из ТЗ без Telegram: python3 tests/test_flow.py"""
import asyncio, os, sys, tempfile, types
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("telethon", types.SimpleNamespace(TelegramClient=None, events=None, utils=None))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from signalbot import config
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
from signalbot import main as m
m.db.set("target", -1001)
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
                Msg(4, "✅TP2  - 4010 XAUUSD, +800 points", 1), Msg(5, "🚨 Отстопило 4000", 1),
                Msg(6, SIGNAL.replace("SELL", "BUY").replace("4030", "4008")),  # будет отменён
                Msg(7, "смотрите какой график"),
                Msg(8, SIGNAL), Msg(9, "🚨 Отстопило 4030", 8)]:
        await m.handle_new(SRC, msg)
    await m.daily_job(m.now().date())
    print(m.admin_command("/period 01.10.2026 31.12.2026"))
    for chat, rep, text in sent:
        print(f"--- в {chat}, ответ на {rep}\n{text}")
    print("отмены:", [r["id"] for r in m.db.due_cancels("9999")])
    # тот же номер сообщения в другом канале-источнике — это новый сигнал, а не дубль
    await m.handle_new(-1003, Msg(1, SIGNAL))
    assert m.db.by_src(-1003, 1) and m.db.by_src(SRC, 1)["id"] != m.db.by_src(-1003, 1)["id"]
    for ref, want in [("-1001234567890", -1001234567890), ("1234567890", -1001234567890),
                      ("https://t.me/c/1234567890/55", -1001234567890), ("@goldsignals", "@goldsignals"),
                      ("t.me/goldsignals", "@goldsignals"), ("привет", None)]:
        assert m.parse_chat_ref(ref) == want, (ref, m.parse_chat_ref(ref))
    print(m.panel_text())

asyncio.run(run())
