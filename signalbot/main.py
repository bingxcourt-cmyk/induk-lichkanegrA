"""Копировщик сигналов: читает закрытые каналы с аккаунта и публикует в свои каналы через бота.

Источник 1 — основная логика (этот файл), источник 2 — source2.py, источник 3 — заглушка.
Каналы для каждого источника выбираются в панели бота (/panel).
"""
import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events

from . import config, prices
from .botapi import BotAPI
from .db import DB, row_signal, row_tps
from .panel import Panel
from .settings import Settings
from .source2 import Source2
from .translate import Translator
from .logic import (
    CANCEL_TEXT, classify_reply, fmt_r, format_be, format_signal, format_stats,
    format_stop, format_tp_hit, parse_signal, ru_date, stop_result,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signalbot")

TZ = ZoneInfo(config.TZ)
db = DB(config.DB_PATH)
bot = BotAPI(config.BOT_TOKEN)
settings = Settings(db)
lock = asyncio.Lock()
panel = None    # создаются в main(): нужен аккаунт-читатель
source2 = None


def now() -> datetime:
    return datetime.now(TZ)


def target1():
    return settings.target(1) or config.TARGET_CHANNEL


# ------------------------------------------------------ сообщения источника

def find_signal(msg, kind):
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        row = db.by_src(msg.reply_to.reply_to_msg_id)
        if row:
            return row
    # Нет реплая — ищем открытый сигнал, у которого совпадает цена тейка
    if kind[0] == "tp":
        for row in reversed(db.open_signals()):
            sig = row_signal(row)
            if kind[1] <= sig.n and sig.tps[kind[1] - 1] == kind[2]:
                return row
    return db.latest_open()


async def handle_new(msg):
    text = msg.message or ""
    sig = parse_signal(text)
    if sig:
        if db.by_src(msg.id):
            return
        dst = await bot.send(target1(), format_signal(sig))
        db.add_signal(msg.id, dst, sig, now().isoformat())
        log.info("сигнал %s %s -> %s", sig.direction, sig.symbol, dst)
        return

    kind = classify_reply(text)
    if not kind:
        return  # всё остальное (голосовые, фото, видео, болтовня) не копируем
    row = find_signal(msg, kind)
    if not row or row["status"] != "open":
        log.info("ответ %s без открытого сигнала, пропуск", kind[0])
        return
    sig, hits, ts = row_signal(row), row_tps(row), now()

    if kind[0] == "tp":
        idx = kind[1]
        if idx > sig.n or idx in hits:
            return
        hits.append(idx)
        await bot.send(target1(), format_tp_hit(sig, idx), reply_to=row["dst_msg_id"])
        db.add_event(row["id"], "tp", sig.tp_r(idx), ts.date().isoformat(), ts.isoformat())
        db.update_signal(row["id"], tps_hit=json.dumps(hits),
                         status="closed" if len(hits) >= sig.n else "open")
    elif kind[0] == "be":
        if row["be_moved"]:
            return
        await bot.send(target1(), format_be(sig), reply_to=row["dst_msg_id"])
        db.update_signal(row["id"], be_moved=1)
    elif kind[0] == "sl":
        r = stop_result(sig, len(hits), bool(row["be_moved"]))
        await bot.send(target1(), format_stop(sig, len(hits), bool(row["be_moved"])),
                       reply_to=row["dst_msg_id"])
        db.add_event(row["id"], "sl", r, ts.date().isoformat(), ts.isoformat())
        db.update_signal(row["id"], status="closed")
    log.info("ответ %s на сигнал #%s", kind, row["id"])


async def handle_edit(msg):
    """Источник поправил сигнал — правим и у себя (пока по нему не было тейков)."""
    row = db.by_src(msg.id)
    sig = parse_signal(msg.message or "")
    if not sig:
        return
    if row is None:
        if (datetime.now(msg.date.tzinfo) - msg.date) < timedelta(minutes=30):
            await handle_new(msg)
        return
    if row["status"] != "open" or row_tps(row):
        return
    await bot.edit(target1(), row["dst_msg_id"], format_signal(sig))
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
    if res or db.created_on(today.isoformat()):
        await bot.send(target1(),
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
                await bot.send(target1(), CANCEL_TEXT, reply_to=row["dst_msg_id"])
                db.update_signal(row["id"], cancel_due=None)
        except Exception:
            log.exception("ошибка планировщика")
        await asyncio.sleep(20)


# ------------------------------------------------------------ команды админа

HELP = (
    "Команды:\n"
    "/today — статистика за сегодня\n"
    "/week — с понедельника по сегодня\n"
    "/month — текущий месяц (или /month 09.2026)\n"
    "/period 01.10.2026 26.10.2026 — за выбранный период\n"
    "/open — открытые сигналы\n"
    "/panel — панель: каналы источников и приёмников"
)


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


async def admin_loop():
    offset = int(db.get("upd_offset", 0))
    while True:
        try:
            for upd in await bot.updates(offset):
                offset = upd["update_id"] + 1
                db.set("upd_offset", offset)
                cb = upd.get("callback_query")
                if cb:
                    if cb.get("from", {}).get("id") == config.ADMIN_ID and panel:
                        await panel.on_callback(cb)
                    continue
                msg = upd.get("message") or {}
                if msg.get("from", {}).get("id") != config.ADMIN_ID or msg.get("chat", {}).get("type") != "private":
                    continue
                text = msg.get("text", "")
                if panel and await panel.on_text(msg["chat"]["id"], text):
                    continue
                if not text.startswith("/"):
                    continue
                if panel and text.split()[0].split("@")[0].lower() in ("/panel", "/start"):
                    await panel.open(msg["chat"]["id"])
                    continue
                try:
                    answer = admin_command(text)
                except (ValueError, IndexError):
                    answer = "Не понял дату. " + HELP
                await bot.send(msg["chat"]["id"], answer)
        except Exception:
            log.exception("ошибка обработки команд")
            await asyncio.sleep(5)


# ------------------------------------------------------------------ запуск

async def seed_source1(user):
    """Первый запуск после обновления: переносим каналы источника 1 из .env в панель."""
    from telethon import utils
    if settings.source(1) is None and config.SOURCE_CHANNEL:
        ent = await user.get_entity(config.SOURCE_CHANNEL)
        settings.set_chat(1, "source", utils.get_peer_id(ent), getattr(ent, "title", str(ent.id)))
    if settings.target(1) is None and config.TARGET_CHANNEL:
        settings.set_chat(1, "target", config.TARGET_CHANNEL, str(config.TARGET_CHANNEL))


async def main():
    global panel, source2
    user = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
    await user.connect()
    if not await user.is_user_authorized():
        raise SystemExit("Аккаунт не авторизован: запустите  python -m signalbot.login")
    await user.get_dialogs()  # прогреваем кэш, чтобы видеть закрытые каналы по ID
    await seed_source1(user)

    async def list_chats():
        return [(d.id, d.name) async for d in user.iter_dialogs() if d.is_channel or d.is_group]

    fx = prices.from_config(config)
    await fx.start()
    panel = Panel(bot, db, settings, list_chats)
    source2 = Source2(bot, db, settings, fx, Translator(config.ANTHROPIC_API_KEY, config.CLAUDE_MODEL),
                      now, config.ADMIN_ID)
    for n in (1, 2, 3):
        log.info("источник %s: %s -> %s", n, settings.name(n, "source"), settings.name(n, "target"))

    def from_source(n):
        return lambda e: settings.source(n) == e.chat_id

    @user.on(events.NewMessage(func=from_source(1)))
    async def on_new(event):
        async with lock:
            try:
                await handle_new(event.message)
            except Exception:
                log.exception("ошибка обработки сообщения %s", event.message.id)

    @user.on(events.NewMessage(func=from_source(2)))
    async def on_new2(event):
        async with lock:
            try:
                await source2.handle(event.message)
            except Exception:
                log.exception("источник 2: ошибка обработки сообщения %s", event.message.id)

    # Источник 3 — заглушка: канал выбирается в панели, правила появятся позже.

    @user.on(events.MessageEdited(func=from_source(1)))
    async def on_edit(event):
        async with lock:
            try:
                await handle_edit(event.message)
            except Exception:
                log.exception("ошибка обработки правки %s", event.message.id)

    asyncio.create_task(scheduler())
    asyncio.create_task(admin_loop())
    await user.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
