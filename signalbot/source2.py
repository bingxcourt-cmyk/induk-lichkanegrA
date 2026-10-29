"""Источник 2 (Forex Bulls Vip Trades): обработка сообщений канала."""
import logging
from decimal import Decimal

from .forexbulls import STOP_TEXT, classify_reply, format_close, format_signal, parse_signal
from .logic import D
from .prices import PriceUnavailable

log = logging.getLogger("source2")


class Source2:
    def __init__(self, bot, db, settings, prices, translator, now, admin_id=None):
        self.bot, self.db, self.settings = bot, db, settings
        self.prices, self.translator, self.now = prices, translator, now
        self.admin_id = admin_id

    @property
    def target(self):
        return self.settings.target(2)

    async def _price(self, symbol, direction):
        try:
            return await self.prices.get(symbol, direction)
        except PriceUnavailable as e:
            log.warning("цена FxPro недоступна: %s", e)
            if self.admin_id:
                try:
                    await self.bot.send(self.admin_id, f"⚠️ Источник 2: нет цены FxPro ({e}). "
                                                       "Пост отправлен без цены.")
                except RuntimeError:
                    pass
            return None

    async def _post_signal(self, src_id, direction, symbol, sl: Decimal, tp: Decimal, reply_to=None):
        entry = await self._price(symbol, direction)
        text = format_signal(direction, symbol, entry, sl, tp, self.settings.header())
        dst = await self.bot.send(self.target, text, reply_to=reply_to, html=True)
        sid = self.db.s2_add(src_id, dst, direction, symbol, entry, sl, tp, self.now().isoformat())
        log.info("сигнал %s %s вход %s -> %s", direction, symbol, entry, dst)
        return sid

    async def handle(self, msg):
        if not self.target:
            log.warning("источник 2: не выбран канал-приёмник (/panel)")
            return
        text = msg.message or ""
        reply_id = msg.reply_to.reply_to_msg_id if msg.reply_to else None
        row = self.db.s2_latest(reply_id) if reply_id else None

        if row is None:
            sig = parse_signal(text)
            if sig and not self.db.s2_by_src(msg.id):
                await self._post_signal(msg.id, sig.direction, sig.symbol, sig.sl, sig.tp)
            return  # всё остальное (медиа, ссылки, приветствия) не копируем

        if getattr(msg, "media", None):
            return  # медиа в реплаях не копируем
        kind = classify_reply(text)
        if kind is None:
            return
        entry = D(row["entry"]) if row["entry"] else None
        sl, tp = D(row["sl"]), D(row["tp"])

        if kind[0] == "close":
            if row["status"] != "open":
                return
            price = await self._price(row["symbol"], row["direction"]) if entry is not None else None
            await self.bot.send(self.target, format_close(entry, sl, price), reply_to=row["dst_msg_id"])
            self.db.s2_update(row["id"], status="profit")
        elif kind[0] == "again":
            if self.db.s2_by_src(msg.id):
                return
            if row["status"] == "open":
                await self.bot.send(self.target, STOP_TEXT, reply_to=row["dst_msg_id"])
            new_id = await self._post_signal(msg.id, kind[1], row["symbol"], sl, tp)
            self.db.s2_update(row["id"], status="stopped" if row["status"] == "open" else row["status"],
                              child_id=new_id)
        else:
            ru = await self.translator.translate(text)
            if ru:
                await self.bot.send(self.target, ru, reply_to=row["dst_msg_id"])
        log.info("реплай %s на сигнал #%s", kind[0], row["id"])
