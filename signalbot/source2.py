"""Правила «Forex Bulls» (источник 2, Forex Bulls Vip Trades) для перенаправления."""
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from .forexbulls import (classify_reply, digits, fmt_level, format_close, format_signal, format_stop,
                         format_tp, parse_signal, profit_pct, result_pct, symbol_of)
from .logic import D, round2
from .prices import PriceUnavailable

log = logging.getLogger("forexbulls")

# что можно сделать сообщением без реплая — с последним открытым сигналом
STANDALONE = ("close", "closeoff", "tp", "sl", "move", "again", "reentry")
STANDALONE_MAX_AGE = timedelta(days=2)  # старее — не угадываем, к какому сигналу это относится


class Source2:
    def __init__(self, bot, db, prices, translator, now, admin_id=None):
        self.bot, self.db = bot, db
        self.prices, self.translator, self.now = prices, translator, now
        self.admin_id = admin_id

    async def _price(self, symbol, direction):
        try:
            return await self.prices.get(symbol, direction)
        except PriceUnavailable as e:
            log.warning("цена недоступна: %s", e)
            if self.admin_id:
                try:
                    await self.bot.send(self.admin_id, f"⚠️ Forex Bulls: нет цены ({e}). "
                                                       "Пост отправлен без цены.")
                except RuntimeError:
                    pass
            return None

    async def _post_signal(self, route, src_id, symbol, direction, sl: Decimal, tp, reply_to=None):
        entry = await self._price(symbol, direction)
        text = format_signal(direction, symbol, entry, sl, tp, route["header"])
        dst = await self.bot.send(route["target"], text, reply_to=reply_to, html=True)
        sid = self.db.s2_add(route["id"], src_id, route["target"], dst, direction, symbol, entry, sl, tp,
                             self.now().isoformat())
        log.info("сигнал %s %s вход %s SL %s TP %s -> %s", direction, symbol, entry, sl, tp, dst)
        return sid

    def _close(self, row, status, result=None):
        """Сделка закрыта: статус и итог в % депозита (None — посчитать не из чего) для статистики."""
        if result is not None:
            result = round2(result) + 0  # + 0 убирает «-0.00»
        self.db.s2_update(row["id"], status=status, closed_day=self.now().date().isoformat(),
                          result=None if result is None else str(result))

    @staticmethod
    def _stop_result(row, entry, sl0, sl):
        if entry is None or sl == sl0 or entry == sl0:
            return Decimal(-1)
        return result_pct(row["direction"], entry, sl0, sl)

    def _thread(self, route, msg):
        """Сигнал, к ветке которого относится сообщение (реплай на сигнал или на реплай к нему)."""
        reply_id = msg.reply_to.reply_to_msg_id if msg.reply_to else None
        return self.db.s2_latest(route["id"], reply_id) if reply_id else None

    async def handle(self, route, msg):
        text = msg.message or ""
        row = self._thread(route, msg)
        if row is not None:
            self.db.s2_link(route["id"], msg.id, row["id"])  # чтобы ответы на этот реплай тоже нашли сигнал
        if getattr(msg, "media", None) and not (row is None and parse_signal(text)):
            return  # медиа не копируем (сигнал с картинкой — копируем текстом)

        if row is None:
            sig = parse_signal(text)
            if sig:
                if not self.db.s2_by_src(route["id"], msg.id):
                    await self._post_signal(route, msg.id, sig.symbol, sig.direction, sig.sl, sig.tp)
                return
            kind = classify_reply(text)
            if not kind or kind[0] not in STANDALONE:
                return  # всё остальное (ссылки, реклама, приветствия, ожидания) не копируем
            opened = self.db.s2_open(route["id"])
            sym = symbol_of(text)
            if sym:  # «Close off profits on GBPJPY» — к сигналу по этой паре
                opened = [r for r in opened if r["symbol"] == sym]
            row = opened[-1] if opened else None
            if row is None or self.now() - datetime.fromisoformat(row["created_at"]) > STANDALONE_MAX_AGE:
                return
            self.db.s2_link(route["id"], msg.id, row["id"])
        else:
            kind = classify_reply(text)
            if kind is None:
                return
            sig = parse_signal(text)
            if kind[0] == "other" and sig and sig.symbol == row["symbol"]:
                kind = ("reentry", sig.direction, sig.sl, sig.tp)  # новый сигнал в ветке старого
        await self._apply(route, msg, row, kind, text)

    async def _apply(self, route, msg, row, kind, text):
        entry = D(row["entry"]) if row["entry"] else None
        sl, tp = D(row["sl"]), (D(row["tp"]) if row["tp"] else None)
        sl0 = D(row["sl0"]) if row["sl0"] else sl
        chat, reply_to, is_open = row["dst_chat"], row["dst_msg_id"], row["status"] == "open"
        act = kind[0]

        if act == "close":
            if not is_open:
                return
            price = await self._price(row["symbol"], row["direction"]) if entry is not None else None
            await self.bot.send(chat, format_close(entry, sl0, price), reply_to=reply_to)
            ok = entry is not None and price is not None and entry != sl0
            self._close(row, "profit", profit_pct(entry, sl0, price) if ok else None)
        elif act == "tp":
            if not is_open:
                return
            price = tp
            if price is None and entry is not None:
                price = await self._price(row["symbol"], row["direction"])
            await self.bot.send(chat, format_tp(entry, sl0, price), reply_to=reply_to)
            ok = entry is not None and price is not None and entry != sl0
            self._close(row, "profit", profit_pct(entry, sl0, price) if ok else None)
        elif act == "sl":
            if not is_open:
                return
            await self.bot.send(chat, format_stop(row["direction"], entry, sl0, sl), reply_to=reply_to)
            self._close(row, "stopped", self._stop_result(row, entry, sl0, sl))
        elif act == "closeoff":
            if not is_open:
                return
            ru = await self.translator.translate(text)
            await self.bot.send(chat, ru or "закрываем сделку по текущей цене.", reply_to=reply_to)
            price = await self._price(row["symbol"], row["direction"]) if entry is not None else None
            ok = entry is not None and price is not None and entry != sl0
            self._close(row, "closed", result_pct(row["direction"], entry, sl0, price) if ok else None)
        elif act == "move":
            if not is_open:
                return
            new_sl = kind[1] if kind[1] is not None else entry
            fields = {}
            if new_sl is not None:
                fields["sl"] = str(new_sl)
            if kind[2] is not None:
                fields["tp"] = str(kind[2])
            if fields:
                self.db.s2_update(row["id"], **fields)
            ru = await self.translator.translate(text)
            if not ru and new_sl is not None:
                ru = f"переносим стоп-лосс на {fmt_level(new_sl, digits(row['symbol'], sl0))}"
            if ru:
                await self.bot.send(chat, ru, reply_to=reply_to)
        elif act in ("again", "reentry"):
            if self.db.s2_by_src(route["id"], msg.id):
                return
            direction = kind[1] or row["direction"]
            new_sl = kind[2] if kind[2] is not None else sl0
            new_tp = kind[3] if kind[3] is not None else tp
            if act == "again":
                # «BUY XAUUSD AGAIN NOW»: прошлый сигнал закрыт по стопу, новый — отдельным постом
                if is_open:
                    await self.bot.send(chat, format_stop(row["direction"], entry, sl0, sl), reply_to=reply_to)
                new_id = await self._post_signal(route, msg.id, row["symbol"], direction, new_sl, new_tp)
                status = "stopped" if is_open else row["status"]
            else:
                new_id = await self._post_signal(route, msg.id, row["symbol"], direction, new_sl, new_tp,
                                                 reply_to=reply_to)
                status = "reentered" if is_open else row["status"]
            self.db.s2_update(row["id"], child_id=new_id)
            if status == "stopped":
                self._close(row, "stopped", self._stop_result(row, entry, sl0, sl))
            elif status != row["status"]:
                self.db.s2_update(row["id"], status=status)
        else:
            ru = await self.translator.translate(text)
            if ru:
                await self.bot.send(chat, ru, reply_to=reply_to)
        log.info("%s по сигналу #%s", act, row["id"])
