"""Копировщик сигналов: читает закрытые каналы с аккаунта и публикует в свои каналы через бота."""
import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, utils

from . import config
from .botapi import BotAPI
from .db import DB, row_signal, row_tps
from .logic import (
    CANCEL_TEXT, classify_reply, fmt_r, format_be, format_signal, format_stats,
    format_stop, format_tp_change, format_tp_hit, parse_signal, ru_date, stop_result, summarize,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signalbot")

TZ = ZoneInfo(config.TZ)
db = DB(config.DB_PATH)
bot = BotAPI(config.BOT_TOKEN)
lock = asyncio.Lock()
user = None        # TelegramClient аккаунта-читателя (None, если не авторизован)
sources = set()    # ID каналов-источников активных перенаправлений


def now() -> datetime:
    return datetime.now(TZ)


def refresh_sources():
    sources.clear()
    sources.update(r["source"] for r in db.routes())


def route_name(route) -> str:
    return f"{route['source_title'] or route['source']} → {route['target_title'] or route['target']}"


# ------------------------------------------------------ сообщения источника

def find_signal(route, msg, kind):
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        row = db.by_src(route["id"], msg.reply_to.reply_to_msg_id)
        if row:
            return row
    # Нет реплая — ищем открытый сигнал, у которого совпадает цена тейка
    rows = db.open_signals(route["id"])
    if kind[0] == "tp":
        for row in reversed(rows):
            sig = row_signal(row)
            if kind[1] <= sig.n and sig.tps[kind[1] - 1] == kind[2]:
                return row
    return rows[-1] if rows else None


async def handle_new(src, msg):
    for route in db.routes():
        if route["source"] == src:
            try:
                await process(route, msg)
            except Exception:
                log.exception("ошибка обработки сообщения %s (%s)", msg.id, route_name(route))


async def process(route, msg):
    text = msg.message or ""
    sig = parse_signal(text)
    if sig:
        if db.by_src(route["id"], msg.id):
            return
        dst_msg = await bot.send(route["target"], format_signal(sig))
        db.add_signal(route["id"], msg.id, route["target"], dst_msg, sig, now().isoformat())
        log.info("сигнал %s %s -> %s (%s)", sig.direction, sig.symbol, dst_msg, route_name(route))
        return

    kind = classify_reply(text)
    if not kind:
        return  # всё остальное (голосовые, фото, видео, болтовня) не копируем
    row = find_signal(route, msg, kind)
    if not row or row["status"] != "open":
        log.info("ответ %s без открытого сигнала, пропуск (%s)", kind[0], route_name(route))
        return
    sig, hits, ts = row_signal(row), row_tps(row), now()
    chat, reply_to = row["dst_chat"], row["dst_msg_id"]

    if kind[0] == "tp":
        idx = kind[1]
        if idx > sig.n or idx in hits:
            return
        hits.append(idx)
        await bot.send(chat, format_tp_hit(sig, idx), reply_to=reply_to)
        db.add_event(row["id"], "tp", sig.tp_r(idx), ts.date().isoformat(), ts.isoformat())
        db.update_signal(row["id"], tps_hit=json.dumps(hits),
                         status="closed" if len(hits) >= sig.n else "open")
    elif kind[0] == "chg":
        idx, price = kind[1], kind[2]
        if idx > sig.n or idx in hits or sig.tps[idx - 1] == price:
            return
        sig.tps[idx - 1] = price  # R этого тейка дальше считается от новой цены
        await bot.send(chat, format_tp_change(idx, price), reply_to=reply_to)
        db.update_signal(row["id"], data=json.dumps(sig.to_dict()))
    elif kind[0] == "be":
        if row["be_moved"]:
            return
        await bot.send(chat, format_be(sig), reply_to=reply_to)
        db.update_signal(row["id"], be_moved=1)
    elif kind[0] == "sl":
        r = stop_result(sig, len(hits), bool(row["be_moved"]))
        await bot.send(chat, format_stop(sig, len(hits), bool(row["be_moved"])), reply_to=reply_to)
        db.add_event(row["id"], "sl", r, ts.date().isoformat(), ts.isoformat())
        db.update_signal(row["id"], status="closed")
    log.info("ответ %s на сигнал #%s", kind, row["id"])


async def handle_edit(src, msg):
    """Источник поправил сигнал — правим и у себя (пока по нему не было тейков)."""
    sig = parse_signal(msg.message or "")
    if not sig:
        return
    for route in db.routes():
        if route["source"] != src:
            continue
        row = db.by_src(route["id"], msg.id)
        if row is None:
            if (datetime.now(msg.date.tzinfo) - msg.date) < timedelta(minutes=30):
                await process(route, msg)
            continue
        if row["status"] != "open" or row_tps(row):
            continue
        await bot.edit(row["dst_chat"], row["dst_msg_id"], format_signal(sig))
        db.update_signal(row["id"], data=json.dumps(sig.to_dict()))
        log.info("сигнал #%s отредактирован", row["id"])


# ------------------------------------------------------------- статистика

def report(route, day_from: date, day_to: date, title: str, label="итог") -> str:
    res = db.results(day_from.isoformat(), day_to.isoformat(), route["id"])
    text = format_stats(title, res, label)
    if day_from != day_to:
        lines, d = [], day_from
        while d <= day_to:
            day_res = db.results(d.isoformat(), d.isoformat(), route["id"])
            if day_res:
                total = sum(v["r"] for v in day_res.values())
                lines.append(f"{d:%d.%m} — {len(day_res)} поз., {fmt_r(total, trim=True)}")
            d += timedelta(days=1)
        if lines:
            text += "\n\n📅 по дням:\n" + "\n".join(lines)
    return text


def admin_report(day_from: date, day_to: date, title: str, label: str) -> str:
    """Отчёт админу: отдельный блок на каждое перенаправление."""
    routes = db.routes()
    if not routes:
        return "Перенаправлений пока нет — добавьте в /settings"
    blocks = [f"🔀 {route_name(r)}\n" + report(r, day_from, day_to, title, label) for r in routes]
    return "\n\n━━━━━━━━━━\n\n".join(blocks)


def month_bounds(y: int, m: int):
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


async def daily_job(today: date):
    for route in db.routes():
        res = db.results(today.isoformat(), today.isoformat(), route["id"])
        if res or db.created_on(today.isoformat(), route["id"]):
            try:
                await bot.send(route["target"],
                               format_stats(f"торговая сессия {ru_date(today)}:", res, "итог дня"))
            except RuntimeError as e:
                log.error("итоги дня не отправлены (%s): %s", route_name(route), e)

    # Сигнал без единого ответа (тейк или безубыток) к концу дня считается отменённым
    due = (now() + timedelta(minutes=config.CANCEL_DELAY_MIN)).isoformat()
    for row in db.open_signals():
        if not row_tps(row) and not row["be_moved"]:
            db.update_signal(row["id"], status="cancelled", cancel_due=due)

    admin_msgs = []
    if today.weekday() == 4:  # пятница
        monday = today - timedelta(days=4)
        admin_msgs.append(admin_report(monday, today,
                                       f"📊 статистика за неделю {monday:%d.%m}–{today:%d.%m.%Y}:", "итог недели"))
    if (today + timedelta(days=1)).month != today.month:
        first, last = month_bounds(today.year, today.month)
        admin_msgs.append(admin_report(first, last, f"📊 статистика за {today:%m.%Y}:", "итог месяца"))
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
                db.update_signal(row["id"], cancel_due=None)
                await bot.send(row["dst_chat"], CANCEL_TEXT, reply_to=row["dst_msg_id"])
        except Exception:
            log.exception("ошибка планировщика")
        await asyncio.sleep(20)


# ------------------------------------------------------------ команды админа

HELP = (
    "Команды:\n"
    "/settings — перенаправления (откуда и куда)\n"
    "/today — статистика за сегодня\n"
    "/week — с понедельника по сегодня\n"
    "/month — текущий месяц (или /month 09.2026)\n"
    "/period 01.10.2026 26.10.2026 — за выбранный период\n"
    "/open — открытые сигналы"
)

COMMANDS = [
    ("settings", "⚙️ перенаправления: откуда и куда"),
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
        return admin_report(today, today, f"торговая сессия {ru_date(today)}:", "итог дня")
    if cmd == "/week":
        monday = today - timedelta(days=today.weekday())
        return admin_report(monday, today, f"📊 неделя {monday:%d.%m}–{today:%d.%m.%Y}:", "итог недели")
    if cmd == "/month":
        y, m = today.year, today.month
        if len(parts) > 1:
            m, y = map(int, parts[1].split("."))
        first, last = month_bounds(y, m)
        return admin_report(first, last, f"📊 статистика за {m:02d}.{y}:", "итог месяца")
    if cmd == "/period":
        if len(parts) < 3:
            return "Формат: /period 01.10.2026 26.10.2026"
        a, b = sorted([parse_day(parts[1]), parse_day(parts[2])])
        return admin_report(a, b, f"📊 статистика {a:%d.%m.%Y}–{b:%d.%m.%Y}:", "итог периода")
    if cmd == "/open":
        out = []
        for route in db.routes():
            for r in db.open_signals(route["id"]):
                s = row_signal(r)
                out.append(f"{route_name(route)}: {s.direction} {s.symbol} вход {s.entry.normalize():f}, "
                           f"тейки {row_tps(r) or '—'}, БУ: {'да' if r['be_moved'] else 'нет'}")
        return "\n".join(out) or "Открытых сигналов нет"
    return HELP


# ------------------------------------------------ панель перенаправлений

pending = {}       # chat_id админа -> (kind, rid): ждём канал текстом; rid "new" — новое перенаправление
drafts = {}        # chat_id -> (source, title): источник нового перенаправления, ждём канал публикации


class Refuse(Exception):
    """Канал не подходит — текст исключения показываем админу."""


def keyboard(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


STATS_ROW = [("📊 Сегодня", "st:/today"), ("📈 Неделя", "st:/week"), ("🗓 Месяц", "st:/month")]


def panel(note: str = ""):
    routes = db.routes()
    lines = [note, ""] if note else []
    lines += ["⚙️ Перенаправления сигналов", ""]
    today = now().date().isoformat()
    for i, r in enumerate(routes, 1):
        res = db.results(today, today, r["id"])
        total = sum((v["r"] for v in res.values()), Decimal(0))
        lines.append(f"{i}. {route_name(r)}\n    сегодня: {len(res)} поз., {fmt_r(total, trim=True)}"
                     f" · открыто {len(db.open_signals(r['id']))}")
    if not routes:
        lines.append("Пока нет ни одного — нажмите «➕ Добавить».")
    if user is None:
        lines += ["", "⚠️ Аккаунт-читатель не авторизован: на сервере запустите\n"
                      "cd /opt/signalbot && venv/bin/python -m signalbot.login"]
    rows = [[(f"{i}. {route_name(r)}"[:60], f"r:{r['id']}")] for i, r in enumerate(routes, 1)]
    rows += [[("➕ Добавить перенаправление", "pick:src:new")], STATS_ROW, [("🔄 Обновить", "panel")]]
    return "\n".join(lines), keyboard(rows)


def route_screen(rid: int, note: str = ""):
    r = db.route(rid)
    if r is None:
        return panel("Это перенаправление уже удалено")
    start = datetime.fromisoformat(r["created_at"]).astimezone(TZ).date()
    profit, stops, be, total = summarize(db.results(start.isoformat(), now().date().isoformat(), rid))
    lines = [note, ""] if note else []
    lines += [
        f"🔀 {route_name(r)}", "",
        f"📥 Откуда: {r['source_title']}\n      ID {r['source']}",
        f"📤 Куда: {r['target_title']}\n      ID {r['target']}", "",
        f"📊 С {start:%d.%m.%Y}: ✅ {profit} · 🚨 {stops} · итог {fmt_r(total, trim=True)}",
        f"Открытых сигналов: {len(db.open_signals(rid))}",
    ]
    kb = keyboard([
        [("📥 Сменить источник", f"pick:src:{rid}")],
        [("📤 Сменить канал публикации", f"pick:dst:{rid}")],
        [("🗑 Удалить", f"del:{rid}")],
        [("« Назад", "panel")],
    ])
    return "\n".join(lines), kb


async def pick_screen(chat, kind: str, rid: str):
    pending[chat] = (kind, rid)
    if kind == "src":
        text = "📥 Откуда копировать сигналы?\n\nВыберите канал из списка"
        if rid != "new":
            text = "⚠️ После смены источника статистика этого перенаправления обнулится.\n\n" + text
    else:
        text = ("📤 Куда публиковать сигналы?\n\nВыберите канал из списка "
                "(бот должен быть в нём администратором)")
        if rid == "new" and chat in drafts:
            text = f"📥 Откуда: {drafts[chat][1]}\n\n" + text
    text += (" или пришлите сюда:\n• ID канала (-100…)\n• @username или ссылку t.me/…\n"
             "• или перешлите любое сообщение из канала")
    current = None
    if rid != "new" and db.route(int(rid)):
        current = db.route(int(rid))["source" if kind == "src" else "target"]
    rows = []
    if user is not None:
        async for d in user.iter_dialogs(limit=300):
            if not d.is_channel:
                continue
            ent = d.entity
            # в «куда» показываем только каналы, где аккаунт — владелец или админ
            if kind == "dst" and not (getattr(ent, "creator", False) or getattr(ent, "admin_rights", None)):
                continue
            mark = "✅ " if d.id == current else ""
            rows.append([(f"{mark}{d.name}"[:60], f"set:{kind}:{rid}:{d.id}")])
            if len(rows) >= 30:
                break
    rows.append([("« Назад", "panel" if rid == "new" else f"r:{rid}")])
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


async def resolve_source(ref):
    if user is None:
        raise Refuse("❌ Аккаунт-читатель не авторизован — сначала signalbot.login на сервере.")
    try:
        ent = await user_entity(ref)
    except Exception:
        raise Refuse("❌ Аккаунт-читатель не видит этот канал. Он должен быть подписан на него.")
    return utils.get_peer_id(ent), getattr(ent, "title", str(ref))


async def resolve_target(ref):
    username = (await bot.me())["username"]
    try:
        chat = await bot.call("getChat", chat_id=ref)
    except RuntimeError:
        raise Refuse(f"❌ Бот не видит этот канал. Добавьте @{username} в канал администратором "
                     f"и выберите его снова.")
    title = chat.get("title", str(ref))
    member = await bot.call("getChatMember", chat_id=chat["id"], user_id=(await bot.me())["id"])
    can_post = member["status"] == "creator" or (
        member["status"] == "administrator" and member.get("can_post_messages", chat["type"] != "channel"))
    if not can_post:
        raise Refuse(f"❌ Бот не может публиковать в «{title}». Сделайте @{username} администратором "
                     f"с правом публикации сообщений и выберите канал снова.")
    return chat["id"], title


def check_pair(source, target, skip_rid=None):
    if source == target:
        raise Refuse("❌ Источник и канал публикации должны быть разными.")
    for r in db.routes():
        if r["id"] != skip_rid and r["source"] == source and r["target"] == target:
            raise Refuse("❌ Такое перенаправление уже есть.")


async def apply_channel(chat, kind: str, rid: str, ref):
    """Применяет выбранный канал и возвращает следующий экран (text, keyboard)."""
    if ref is None:
        raise Refuse("❌ Не понял. Пришлите ID канала (-100…), @username, ссылку или перешлите сообщение из канала.")
    cid, title = await (resolve_source(ref) if kind == "src" else resolve_target(ref))

    if rid == "new":
        if kind == "src":
            drafts[chat] = (cid, title)
            return await pick_screen(chat, "dst", "new")
        if chat not in drafts:
            return panel("Начните заново: «➕ Добавить перенаправление»")
        src, src_title = drafts[chat]
        check_pair(src, cid)
        new = db.add_route(src, src_title, cid, title, now().isoformat())
        drafts.pop(chat)
        log.info("новое перенаправление #%s: %s → %s", new, src_title, title)
        return route_screen(new, "✅ Перенаправление добавлено")

    route = db.route(int(rid))
    if route is None:
        return panel("Это перенаправление уже удалено")
    if kind == "src":
        if cid == route["source"]:
            return route_screen(route["id"], "Это и так текущий источник")
        check_pair(cid, route["target"], skip_rid=route["id"])
        # новый источник = новое перенаправление: статистика с нуля, старые сигналы не отслеживаем
        db.close_route(route["id"])
        new = db.add_route(cid, title, route["target"], route["target_title"], now().isoformat())
        log.info("перенаправление #%s: источник %s → %s (#%s)", route["id"], route["source_title"], title, new)
        return route_screen(new, "✅ Источник заменён, статистика обнулена")
    check_pair(route["source"], cid, skip_rid=route["id"])
    db.update_route(route["id"], target=cid, target_title=title)
    log.info("перенаправление #%s: публикация → %s", route["id"], title)
    return route_screen(route["id"], "✅ Канал публикации заменён")


async def on_callback(cq):
    if cq.get("from", {}).get("id") != config.ADMIN_ID or "message" not in cq:
        return await bot.answer(cq["id"])
    data, chat, mid = cq.get("data", ""), cq["message"]["chat"]["id"], cq["message"]["message_id"]
    await bot.answer(cq["id"])
    screen = None
    if data == "panel":
        pending.pop(chat, None)
        drafts.pop(chat, None)
        screen = panel()
    elif data.startswith("r:"):
        pending.pop(chat, None)
        screen = route_screen(int(data[2:]))
    elif data.startswith("pick:"):
        _, kind, rid = data.split(":")
        screen = await pick_screen(chat, kind, rid)
    elif data.startswith("set:"):
        _, kind, rid, cid = data.split(":", 3)
        try:
            async with lock:
                screen = await apply_channel(chat, kind, rid, int(cid))
            refresh_sources()
        except Refuse as e:
            return await bot.send(chat, str(e))
    elif data.startswith("del:"):
        r = db.route(int(data[4:]))
        if r:
            screen = (f"Удалить перенаправление «{route_name(r)}»?\n\n"
                      f"Открытые сигналы перестанут отслеживаться, его статистика пропадёт из отчётов.",
                      keyboard([[("🗑 Да, удалить", f"delok:{r['id']}")], [("« Отмена", f"r:{r['id']}")]]))
        else:
            screen = panel()
    elif data.startswith("delok:"):
        db.close_route(int(data[6:]))
        refresh_sources()
        screen = panel("🗑 Перенаправление удалено")
    elif data.startswith("st:"):
        return await bot.send(chat, admin_command(data[3:]))
    if screen:
        await bot.edit(chat, mid, *screen)


async def on_admin_message(msg):
    chat, text = msg["chat"]["id"], msg.get("text", "")
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        pending.pop(chat, None)
        if cmd in ("/start", "/settings", "/menu"):
            if cmd == "/start":
                await set_commands()
            return await bot.send(chat, *panel())
        try:
            answer = admin_command(text)
        except (ValueError, IndexError):
            answer = "Не понял дату. " + HELP
        return await bot.send(chat, answer)

    if chat not in pending:
        return await bot.send(chat, *panel())
    kind, rid = pending[chat]
    fwd = (msg.get("forward_origin") or {}).get("chat") or msg.get("forward_from_chat")
    try:
        async with lock:
            screen = await apply_channel(chat, kind, rid, fwd["id"] if fwd else parse_chat_ref(text))
        refresh_sources()
    except Refuse as e:
        return await bot.send(chat, str(e))
    await bot.send(chat, *screen)


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
    """Пара каналов из .env — первое перенаправление, если в боте ещё ничего не настроено."""
    if db.routes() or not (config.SOURCE_CHANNEL and config.TARGET_CHANNEL) or user is None:
        return
    try:
        src, src_title = await resolve_source(config.SOURCE_CHANNEL)
        dst, dst_title = await resolve_target(config.TARGET_CHANNEL)
        db.add_route(src, src_title, dst, dst_title, now().isoformat())
    except Refuse as e:
        log.warning("каналы из .env не подошли: %s", e)


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
    refresh_sources()
    for r in db.routes():
        log.info("перенаправление #%s: %s", r["id"], route_name(r))

    if user is not None:
        # без фильтра chats=, чтобы перенаправления можно было менять в боте без перезапуска
        @user.on(events.NewMessage(func=lambda e: e.chat_id in sources))
        async def on_new(event):
            async with lock:
                await handle_new(event.chat_id, event.message)

        @user.on(events.MessageEdited(func=lambda e: e.chat_id in sources))
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
