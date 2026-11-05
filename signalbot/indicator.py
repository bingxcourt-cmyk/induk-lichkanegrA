"""Independent indicator #1 ledger. Captions count; advertising never enters reports.

Each TPn means n% for a full-size position. Weighted strategies use the fraction
of that position; any unfilled target loses its fraction at SL. No BE moves.
"""
import asyncio
import logging
import re
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

MSK = ZoneInfo('Europe/Moscow')
log = logging.getLogger('indicator')
HEAD = re.compile(r'^\s*#([A-Z][A-Z0-9_]*)\s+(LONG|SHORT)\b', re.I)
ENTRY = re.compile(r'Вход\s+сейчас\s+или\s+[\d.,]+', re.I)
TPS = re.compile(r'\bTP\s*([1-4])\s*[:\s]+[\d.,]+', re.I)
TP = re.compile(r'^\s*✅\s*#?([A-Z][A-Z0-9_]*)\s+TP\s*([1-4])\s+ВЗЯТ', re.I)
FINAL = re.compile(r'^\s*🏁\s*#?([A-Z][A-Z0-9_]*)\s+ФИНАЛЬНЫЙ\s+TP\s+ВЗЯТ', re.I)
STOP = re.compile(r'^\s*❌\s*#?([A-Z][A-Z0-9_]*)\s+Сработал\s+стоп[ -]лосс', re.I)
ALIASES = {'XAUUSD': 'GOLD', 'XAU': 'GOLD', 'XAGUSD': 'SILVER',
           'XAG': 'SILVER', 'DE40': 'GER40', 'GERMANY40': 'GER40'}


def canonical(symbol):
    symbol = symbol.upper()
    return ALIASES.get(symbol, symbol)


STRATEGIES = [
 ('На первом тейке', (1, 0, 0, 0)),
 ('На втором тейке', (0, 1, 0, 0)),
 ('На третьем тейке', (0, 0, 1, 0)),
 ('На четвёртом тейке', (0, 0, 0, 1)),
 ('Частями 25/25/25/25', ('.25', '.25', '.25', '.25')),
 ('Частями 40/20/20/20', ('.4', '.2', '.2', '.2')),
 ('Частями 50/50 на TP1/TP2', ('.5', '.5', 0, 0)),
]


def parse(text):
    text = (text or '').replace('\xa0', ' ')
    m = HEAD.search(text)
    if m and ENTRY.search(text) and re.search(r'\bSL\s+[\d.,]+', text, re.I) and {int(n) for n in TPS.findall(text)} == {1, 2, 3, 4}:
        return ('signal', canonical(m[1]), m[2].upper(), 0)
    m = TP.search(text)
    if m:
        return ('tp', canonical(m[1]), '', int(m[2]))
    m = FINAL.search(text)
    if m:
        return ('tp', canonical(m[1]), '', 4)
    m = STOP.search(text)
    if m:
        return ('stop', canonical(m[1]), '', 0)
    return None


def payoff(max_tp, weights):
    return sum((Decimal(str(w)) * (Decimal(i) if max_tp >= i else Decimal(-1))
                for i, w in enumerate(weights, 1)), Decimal(0))


def percent(v):
    v = v.quantize(Decimal('.01'))
    if not v:
        return '0%'
    return ('+' if v > 0 else '') + format(v, 'f').rstrip('0').rstrip('.').replace('.', ',') + '%'


class Indicator:
    def __init__(self, db, channel):
        self.db, self.channel = db, int(channel)
        self.loading = True
        self.error = None
        self.ready = asyncio.Event()
        db.conn.execute('''CREATE TABLE IF NOT EXISTS indicator_messages (
            channel INTEGER NOT NULL, msg_id INTEGER NOT NULL, stamp TEXT NOT NULL,
            reply_id INTEGER, kind TEXT, symbol TEXT, side TEXT, tp INTEGER,
            PRIMARY KEY(channel, msg_id))''')
        db.conn.commit()

    def ingest(self, msg):
        parsed = parse(msg.message)
        reply = msg.reply_to.reply_to_msg_id if msg.reply_to else None
        # Keep neutral message links too, for replies to replies and edited/deleted formats.
        kind, symbol, side, tp = parsed or ('', '', '', 0)
        self.db.conn.execute('''INSERT OR REPLACE INTO indicator_messages
            VALUES (?,?,?,?,?,?,?,?)''', (self.channel, msg.id, msg.date.astimezone(MSK).isoformat(),
                                         reply, kind, symbol, side, tp))
        self.db.conn.commit()

    async def sync(self, user):
        try:
            # Full rescan on startup also picks up edited messages; live upserts are idempotent.
            async for msg in user.iter_messages(self.channel):
                self.ingest(msg)
            self.error = None
            log.info('индикатор #1: история загружена')
        except Exception:
            self.error = 'Не удалось полностью загрузить историю. Отчёт может быть неполным.'
            log.exception('индикатор #1: загрузка истории')
        finally:
            self.loading = False
            self.ready.set()

    def trades(self, at):
        """Use explicit reply chains; never guess between concurrent GOLD signals."""
        rows = [dict(r) for r in self.db.conn.execute(
            'SELECT * FROM indicator_messages WHERE channel=? ORDER BY stamp, msg_id', (self.channel,))]
        by_id = {r['msg_id']: r for r in rows}
        signals = {}
        for r in rows:
            if r['kind'] != 'signal':
                continue
            dt = datetime.fromisoformat(r['stamp'])
            if dt > at:
                continue
            cutoff = datetime.combine(dt.date(), time(22), MSK)
            signals[r['msg_id']] = dict(r, day=dt.date(), cutoff=cutoff, max_tp=0,
                                        stop=False, finished=False, last_id=0)
        unmatched = []
        for r in rows:
            if r['kind'] not in ('tp', 'stop'):
                continue
            dt = datetime.fromisoformat(r['stamp'])
            if dt > at:
                continue
            p = r['reply_id']
            seen = set()
            while p and p not in signals and p not in seen:
                seen.add(p)
                p = by_id[p]['reply_id'] if p in by_id else None
            s = signals.get(p)
            if s is None or s['symbol'] != r['symbol']:
                unmatched.append(r)
                continue
            # A signal belongs to its publication date. No late results after 22:00.
            if dt < datetime.fromisoformat(s['stamp']) or dt >= s['cutoff'] or s['finished']:
                continue
            if r['kind'] == 'tp':
                s['max_tp'] = max(s['max_tp'], r['tp'])
                s['finished'] = s['max_tp'] == 4
            else:
                s['stop'] = s['finished'] = True
        for s in signals.values():
            s['finished'] = s['finished'] or at >= s['cutoff']
        return list(signals.values()), unmatched

    def report(self, start, end, at=None, symbol=None):
        at = (at or datetime.now(MSK)).astimezone(MSK)
        trades, unmatched = self.trades(at)
        chosen = [s for s in trades if start <= s['day'] <= end and (symbol is None or s['symbol'] == symbol)]
        closed = [s for s in chosen if s['finished']]
        waiting = len(chosen) - len(closed)
        title = 'твердая стата по индюку #1 за сегодня:' if start == end == at.date() else (
            f'твердая стата по индюку #1 за {start:%d.%m.%Y}–{end:%d.%m.%Y}:')
        scope = {'GOLD': 'Золото (GOLD)', 'GER40': 'Германия (GER40)'}.get(symbol, symbol or 'Все инструменты')
        lines = [title, scope, '', f'Сигналов: {len(chosen)} · завершено: {len(closed)} · ожидают: {waiting}']
        lines += ['Максимальный TP: ' + ' · '.join(f'TP{i}: {sum(s["max_tp"] == i for s in closed)}' for i in range(1, 5)),
                  f'Без тейков: {sum(s["max_tp"] == 0 for s in closed)}', '']
        for label, weights in STRATEGIES:
            result = sum((payoff(s['max_tp'], weights) for s in closed), Decimal(0))
            lines += [f'Если фиксировать {label[0].lower() + label[1:]} = {percent(result)} к депозиту', '']
        lines += ['Риск 1% на сигнал. Все стратегии без переноса стопа в БУ.',
                  'Остаток позиции и сигналы без тейков в 22:00 МСК считаются закрытыми по стопу.']
        if waiting:
            lines += ['Ожидающие сделки в суммы пока не включены. Итоги дня — после 22:00 МСК.']
        if self.loading:
            lines += ['⏳ История ещё загружается — результат предварительный.']
        if self.error:
            lines += [f'⚠️ {self.error}']
        missing = sum(start <= datetime.fromisoformat(r['stamp']).date() <= end
                      and (symbol is None or r['symbol'] == symbol) for r in unmatched)
        if missing:
            lines += [f'Отдельно: {missing} сообщений о результате за период без привязки к сигналу; в расчёт не включены.']
        return '\n'.join(lines)
