"""Копировщик сигналов: читает закрытый канал с аккаунта и публикует в свой канал через бота."""
import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, utils

from . import config
from .botapi import BotAPI
from .db import DB, row_signal, row_tps
from .logic import (
    CANCEL_TEXT, classify_reply, fmt_r, format_be, format_signal, format_stats,
    format_stop, format_tp_hit, parse_signal, ru_date, stop_result,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signalbot")

TZ = ZoneInfo(config.TZ)
db = DB(config.DB_PATH)
bot = BotAPI(config.BOT_TOKEN)
lock = asyncio.Lock()
user = None        # TelegramClient аккаунта-читателя (None, если не авторизован)


def now() -> datetime:
    return datetime.now(TZ)


# ------------------------------------------------- каналы (меняются в боте)

def source_id():
    v = db.get("source")
    return int(v) if v else None


def target_id():
    v = db.get("target")
    return int(v) if v else None


def reply_chat(row) -> int:
    return row["dst_chat"]


# ------------------------------------------------------ сообщения источника

def find_signal(src, msg, kind):
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        row = db.by_src(src, msg.reply_to.reply_to_msg_id)
        if row:
            return row
    # Нет реплая — ищем открытый сигнал, у которого совпадает цена тейка
    rows = db.open_signals(src)
    if kind[0] == "tp":
        for row in reversed(rows):
            sig = row_signal(row)
            if kind[1] <= sig.n and sig.tps[kind[1] - 1] == kind[2]:
                return row
    return rows[-1] if rows else None


async def handle_new(src, msg):
    text = msg.message or ""
    sig = parse_signal(text)
    if sig:
        if db.by_src(src, msg.id):
            return
        dst = target_id()
        if not dst:
            log.warning("сигнал пропущен: не выбран канал для публикации (/settings в боте)")
            return
        dst_msg = await bot.send(dst, format_signal(sig))
        db.add_signal(src, msg.id, dst, dst_msg, sig, now().isoformat())
        log.info("сигнал %s %s -> %s", sig.direction, sig.symbol, dst_msg)
        return

    kind = classify_reply(text)
    if not kind:
        return  # всё остальное (голосовые, фото, видео, болтовня) не копируем
    row = find_signal(src, msg, kind)
    if not row or row["status"] != "open":
        log.info("ответ %s без открытого сигнала, пропуск", kind[0])
        return
    sig, hits, ts = row_signal(row), row_tps(row), now()

    if kind[0] == "tp":
        idx = kind[1]
        if idx > sig.n or idx in hits:
            return
        hits.append(idx)
        await bot.send(reply_chat(row), format_tp_hit(sig, idx), reply_to=row["dst_msg_id"])
        db.add_event(row["id"], "tp", sig.tp_r(idx), ts.date().isoformat(), ts.isoformat())
        db.update_signal(row["id"], tps_hit=json.dumps(hits),
                         status="closed" if len(hits) >= sig.n else "open")
    elif kind[0] == "be":
        if row["be_moved"]:
            return
        await bot.send(reply_chat(row), format_be(sig), reply_to=row["dst_msg_id"])
        db.update_signal(row["id"], be_moved=1)
    elif kind[0] == "sl":
        r = stop_result(sig, len(hits), bool(row["be_moved"]))
        await bot.send(reply_chat(row), format_stop(sig, len(hits), bool(row["be_moved"])),
                       reply_to=row["dst_msg_id"])
        db.add_event(row["id"], "sl", r, ts.date().isoformat(), ts.isoformat())
        db.update_signal(row["id"], status="closed")
    log.info("ответ %s на сигнал #%s", kind, row["id"])


async def handle_edit(src, msg):
    """Источник поправил сигнал — правим и у себя (пока по нему не было тейков)."""
    row = db.by_src(src, msg.id)
    sig = parse_signal(msg.message or "")
    if not sig:
        return
    if row is None:
        if (datetime.now(msg.date.tzinfo) - msg.date) < timedelta(minutes=30):
            await handle_new(src, msg)
        return
    if row["status"] != "open" or row_tps(row):
        return
    await bot.edit(reply_chat(row), row["dst_msg_id"], format_signal(sig))
    db.update_signal(row["id"], data=json.dumps(sig.to_dict()))
    log.info("сигнал #%s отредактирован", row["id"])


# ------------------------------------------------------------- статистика

def report(day_from: date, day_to: date, title: str, label="итог") -> str:
    res = db.results(day_from.isoformat(), day_to.isoformat())
    text = format_stats(title, res, label)
    if day_from != day_to:
        lines, d = [], day_from
        while d <= day_to:
            day_res = db.results(d.isoformat(), d.isoformat())
            if day_res:
                total = sum(v["r"] for v in day_res.values())
                lines.append(f"{d:%d.%m} — {len(day_res)} поз., {fmt_r(total, trim=True)}")
            d += timedelta(days=1)
        if lines:
            text += "\n\n📅 по дням:\n" + "\n".join(lines)
    return text


def month_bounds(y: int, m: int):
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


async def daily_job(today: date):
    res = db.results(today.isoformat(), today.isoformat())
    if target_id() and (res or db.created_on(today.isoformat())):
        await bot.send(target_id(),
                       format_stats(f"торговая сессия {ru_date(today)}:", res, "итог дня"))

    # Сигналы без единого ответа (тейк/стоп) к концу дня считаются отменёнными
    due = (now() + timedelta(minutes=config.CANCEL_DELAY_MIN)).isoformat()
    for row in db.open_signals():
        if not row_tps(row):
            db.update_signal(row["id"], status="cancelled", cancel_due=due)

    admin_msgs = []
    if today.weekday() == 4:  # пятница
        monday = today - timedelta(days=4)
        admin_msgs.append(report(monday, today,
                                 f"📊 статистика за неделю {monday:%d.%m}–{today:%d.%m.%Y}:", "итог недели"))
    if (today + timedelta(days=1)).month != today.month:
        first, last = month_bounds(today.year, today.month)
        admin_msgs.append(report(first, last, f"📊 статистика за {today:%m.%Y}:", "итог месяца"))
    for text in admin_msgs:
        try:
            await bot.send(config.ADMIN_ID, text)
        except RuntimeError as e:
            log.error("не удалось отправить админу (нажмите /start в боте): %s", e)


async def scheduler():
    hh, mm = map(int, config.STATS_TIME.split(":"))
    while True:
        try:
            t = now()
            today = t.date().isoformat()
            if (t.hour, t.minute) >= (hh, mm) and db.get("last_daily") != today:
                db.set("last_daily", today)
                async with lock:
                    await daily_job(t.date())
            for row in db.due_cancels(t.isoformat()):
                await bot.send(reply_chat(row), CANCEL_TEXT, reply_to=row["dst_msg_id"])
                db.update_signal(row["id"], cancel_due=None)
        except Exception:
            log.exception("ошибка планировщика")
        await asyncio.sleep(20)


# ------------------------------------------------------------ команды админа

HELP = (
    "Команды:\n"
    "/settings — выбрать каналы (откуда и куда)\n"
    "/today — статистика за сегодня\n"
    "/week — с понедельника по сегодня\n"
    "/month — текущий месяц (или /month 09.2026)\n"
    "/period 01.10.2026 26.10.2026 — за выбранный период\n"
    "/open — открытые сигналы"
)

COMMANDS = [
    ("settings", "⚙️ каналы: откуда и куда"),
    ("today", "статистика за сегодня"),
    ("week", "статистика за неделю"),
    ("month", "статистика за месяц"),
    ("period", "за период: /period 01.10.2026 26.10.2026"),
    ("open", "открытые сигналы"),
]


def parse_day(s: str) -> date:
    return datetime.strptime(s, "%d.%m.%Y").date()


def admin_command(text: str) -> str:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    today = now().date()
    if cmd == "/today":
        return report(today, today, f"торговая сессия {ru_date(today)}:", "итог дня")
    if cmd == "/week":
        monday = today - timedelta(days=today.weekday())
        return report(monday, today, f"📊 неделя {monday:%d.%m}–{today:%d.%m.%Y}:", "итог недели")
    if cmd == "/month":
        y, m = today.year, today.month
        if len(parts) > 1:
            m, y = map(int, parts[1].split("."))
        first, last = month_bounds(y, m)
        return report(first, last, f"📊 статистика за {m:02d}.{y}:", "итог месяца")
    if cmd == "/period":
        if len(parts) < 3:
            return "Формат: /period 01.10.2026 26.10.2026"
        a, b = sorted([parse_day(parts[1]), parse_day(parts[2])])
        return report(a, b, f"📊 статистика {a:%d.%m.%Y}–{b:%d.%m.%Y}:", "итог периода")
    if cmd == "/open":
        rows = db.open_signals()
        if not rows:
            return "Открытых сигналов нет"
        out = []
        for r in rows:
            s = row_signal(r)
            out.append(f"#{r['id']} {s.direction} {s.symbol} вход {s.entry.normalize():f}, "
                       f"тейки {row_tps(r) or '—'}, БУ: {'да' if r['be_moved'] else 'нет'}")
        return "\n".join(out)
    return HELP


# ------------------------------------------------ панель настроек каналов

pending = {}       # chat_id админа -> "src"/"dst": ждём ID канала текстом


def keyboard(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


PANEL_KB = keyboard([
    [("📥 Откуда копировать", "pick:src")],
    [("📤 Куда отправлять", "pick:dst")],
    [("📊 Сегодня", "st:/today"), ("📈 Неделя", "st:/week"), ("🗓 Месяц", "st:/month")],
    [("🔄 Обновить", "panel")],
])


def panel_text(note: str = "") -> str:
    def line(icon, label, key):
        cid = db.get(key)
        if not cid:
            return f"{icon} {label}: не выбран"
        return f"{icon} {label}: {db.get(key + '_title') or 'канал'}\n      ID {cid}"

    lines = []
    if note:
        lines += [note, ""]
    lines += ["⚙️ Настройки копировщика", "",
              line("📥", "Откуда", "source"), line("📤", "Куда", "target"), ""]
    if user is None:
        lines.append("⚠️ Аккаунт-читатель не авторизован: на сервере запустите\n"
                     "cd /opt/signalbot && venv/bin/python -m signalbot.login")
    elif source_id() and target_id():
        lines.append(f"✅ Работает. Открытых сигналов: {len(db.open_signals())}")
    else:
        lines.append("Выберите оба канала кнопками ниже 👇")
    return "\n".join(lines)


async def pick_screen(kind: str):
    if kind == "src":
        text = "📥 Откуда копировать сигналы?\n\nВыберите канал из списка"
    else:
        text = ("📤 Куда публиковать сигналы?\n\nВыберите канал из списка "
                "(бот должен быть в нём администратором)")
    text += (" или пришлите сюда:\n• ID канала (-100…)\n• @username или ссылку t.me/…\n"
             "• или перешлите любое сообщение из канала")
    rows = []
    if user is not None:
        async for d in user.iter_dialogs(limit=300):
            if not d.is_channel:
                continue
            ent = d.entity
            # в «куда» показываем только каналы, где аккаунт — владелец или админ
            if kind == "dst" and not (getattr(ent, "creator", False) or getattr(ent, "admin_rights", None)):
                continue
            mark = "✅ " if d.id == (source_id() if kind == "src" else target_id()) else ""
            rows.append([(f"{mark}{d.name}"[:60], f"set:{kind}:{d.id}")])
            if len(rows) >= 30:
                break
    rows.append([("« Назад", "panel")])
    return text, keyboard(rows)


def parse_chat_ref(s: str):
    """ID / @username / ссылка -> int ID или '@username'."""
    s = s.strip()
    m = re.search(r"t\.me/c/(\d+)", s)
    if m:
        return int("-100" + m.group(1))
    m = re.search(r"t\.me/([A-Za-z]\w{3,})", s)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"-?\d+", s):
        v = int(s)
        return int("-100" + s) if v > 0 else v  # ID без -100 тоже принимаем
    if re.fullmatch(r"@?[A-Za-z]\w{3,}", s):
        return "@" + s.lstrip("@")
    return None


async def user_entity(ref):
    try:
        return await user.get_entity(ref)
    except ValueError:
        await user.get_dialogs()  # канала нет в кэше — обновляем список диалогов
        return await user.get_entity(ref)


async def set_channel(kind: str, ref) -> str:
    """Проверяет канал и сохраняет. Возвращает строку-результат для админа."""
    if ref is None:
        return "❌ Не понял. Пришлите ID канала (-100…), @username, ссылку или перешлите сообщение из канала."
    if kind == "src":
        if user is None:
            return "❌ Аккаунт-читатель не авторизован — сначала signalbot.login на сервере."
        try:
            ent = await user_entity(ref)
        except Exception:
            return "❌ Аккаунт-читатель не видит этот канал. Он должен быть подписан на него."
        cid, title = utils.get_peer_id(ent), getattr(ent, "title", str(ref))
        if cid == target_id():
            return "❌ Это канал, куда идут сигналы. Источник должен быть другим."
        db.set("source", cid)
        db.set("source_title", title)
        log.info("источник: %s (%s)", title, cid)
        return f"✅ Теперь копирую из «{title}»"

    try:
        chat = await bot.call("getChat", chat_id=ref)
    except RuntimeError:
        return ("❌ Бот не видит этот канал. Добавьте @" + (await bot.me())["username"] +
                " в канал администратором и выберите его снова.")
    cid, title = chat["id"], chat.get("title", str(ref))
    if cid == source_id():
        return "❌ Это канал-источник. Канал для публикации должен быть другим."
    member = await bot.call("getChatMember", chat_id=cid, user_id=(await bot.me())["id"])
    can_post = member["status"] == "creator" or (
        member["status"] == "administrator" and member.get("can_post_messages", chat["type"] != "channel"))
    if not can_post:
        return (f"❌ Бот не может публиковать в «{title}». Сделайте @{(await bot.me())['username']} "
                f"администратором с правом публикации сообщений и выберите канал снова.")
    db.set("target", cid)
    db.set("target_title", title)
    log.info("канал публикации: %s (%s)", title, cid)
    return f"✅ Теперь публикую в «{title}»"


async def show_panel(chat_id, note=""):
    await bot.send(chat_id, panel_text(note), markup=PANEL_KB)


async def on_callback(cq):
    if cq.get("from", {}).get("id") != config.ADMIN_ID or "message" not in cq:
        return await bot.answer(cq["id"])
    data, chat, mid = cq.get("data", ""), cq["message"]["chat"]["id"], cq["message"]["message_id"]
    if data == "panel":
        pending.pop(chat, None)
        await bot.edit(chat, mid, panel_text(), PANEL_KB)
    elif data.startswith("pick:"):
        kind = data[5:]
        pending[chat] = kind
        text, kb = await pick_screen(kind)
        await bot.edit(chat, mid, text, kb)
    elif data.startswith("set:"):
        _, kind, cid = data.split(":", 2)
        async with lock:
            result = await set_channel(kind, int(cid))
        if result.startswith("✅"):
            pending.pop(chat, None)
            await bot.edit(chat, mid, panel_text(result), PANEL_KB)
        else:
            await bot.send(chat, result)
    elif data.startswith("st:"):
        await bot.send(chat, admin_command(data[3:]))
    await bot.answer(cq["id"])


async def on_admin_message(msg):
    chat, text = msg["chat"]["id"], msg.get("text", "")
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        pending.pop(chat, None)
        if cmd in ("/start", "/settings", "/menu"):
            if cmd == "/start":
                await set_commands()
            return await show_panel(chat)
        try:
            answer = admin_command(text)
        except (ValueError, IndexError):
            answer = "Не понял дату. " + HELP
        return await bot.send(chat, answer)

    kind = pending.get(chat)
    if not kind:
        return await show_panel(chat)
    fwd = (msg.get("forward_origin") or {}).get("chat") or msg.get("forward_from_chat")
    ref = fwd["id"] if fwd else parse_chat_ref(text)
    async with lock:
        result = await set_channel(kind, ref)
    if result.startswith("✅"):
        pending.pop(chat, None)
        await show_panel(chat, result)
    else:
        await bot.send(chat, result)


async def set_commands():
    try:
        await bot.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS],
                       scope={"type": "chat", "chat_id": config.ADMIN_ID})
    except RuntimeError as e:
        log.warning("меню команд не установлено (админ ещё не нажал /start?): %s", e)


async def admin_loop():
    await set_commands()
    offset = int(db.get("upd_offset", 0))
    while True:
        try:
            for upd in await bot.updates(offset):
                offset = upd["update_id"] + 1
                db.set("upd_offset", offset)
                try:
                    if "callback_query" in upd:
                        await on_callback(upd["callback_query"])
                        continue
                    msg = upd.get("message") or {}
                    if msg.get("from", {}).get("id") == config.ADMIN_ID and msg["chat"]["type"] == "private":
                        await on_admin_message(msg)
                except Exception:
                    log.exception("ошибка обработки команды")
        except Exception:
            log.exception("ошибка получения обновлений")
            await asyncio.sleep(5)


# ------------------------------------------------------------------ запуск

async def import_env_channels():
    """Каналы из .env используются как начальные значения, дальше меняются в боте."""
    if not db.get("source") and config.SOURCE_CHANNEL and user is not None:
        try:
            ent = await user_entity(config.SOURCE_CHANNEL)
            db.set("source", utils.get_peer_id(ent))
            db.set("source_title", getattr(ent, "title", ""))
        except Exception as e:
            log.warning("SOURCE_CHANNEL из .env не найден: %s", e)
    if not db.get("target") and config.TARGET_CHANNEL:
        try:
            chat = await bot.call("getChat", chat_id=config.TARGET_CHANNEL)
            db.set("target", chat["id"])
            db.set("target_title", chat.get("title", ""))
        except RuntimeError as e:
            log.warning("TARGET_CHANNEL из .env недоступен боту: %s", e)


async def main():
    global user
    client = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        user = client
        await user.get_dialogs()  # прогреваем кэш, чтобы видеть закрытые каналы по ID
    else:
        log.error("аккаунт не авторизован: запустите  python -m signalbot.login  (бот-панель работает)")
    await import_env_channels()
    log.info("источник: %s, публикация: %s", db.get("source_title") or "—", db.get("target_title") or "—")

    if user is not None:
        # без фильтра chats=, чтобы источник можно было сменить в боте без перезапуска
        @user.on(events.NewMessage(func=lambda e: e.chat_id == source_id()))
        async def on_new(event):
            async with lock:
                try:
                    await handle_new(event.chat_id, event.message)
                except Exception:
                    log.exception("ошибка обработки сообщения %s", event.message.id)

        @user.on(events.MessageEdited(func=lambda e: e.chat_id == source_id()))
        async def on_edit(event):
            async with lock:
                try:
                    await handle_edit(event.chat_id, event.message)
                except Exception:
                    log.exception("ошибка обработки правки %s", event.message.id)

    asyncio.create_task(scheduler())
    if user is None:
        await admin_loop()
    else:
        asyncio.create_task(admin_loop())
        await user.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
