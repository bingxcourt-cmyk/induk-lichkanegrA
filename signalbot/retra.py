"""XAUUSD-only copy from the indicator feed. Own reply map; no historical posting."""
import re
import logging

log = logging.getLogger('retra')
NUM = r'\d+(?:[.,]\d+)?'
HEAD = re.compile(r'^\s*#XAUUSD\s+(LONG|SHORT)\b', re.I)
ENTRY = re.compile(r'Вход\s+сейчас\s+или\s+('+NUM+r')', re.I)
SL = re.compile(r'\bSL\s+('+NUM+r')', re.I)
TPS = re.compile(r'\bTP\s*([1-4])\s+('+NUM+r')', re.I)
HIT = re.compile(r'^\s*✅\s*#?XAUUSD\s+TP\s*([1-4])\s+ВЗЯТ', re.I)
FINAL = re.compile(r'^\s*🏁\s*#?XAUUSD\s+ФИНАЛЬНЫЙ\s+TP\s+ВЗЯТ', re.I)
STOP = re.compile(r'^\s*❌\s*#?XAUUSD\s+Сработал\s+стоп[ -]лосс', re.I)
PROFIT = re.compile(r'([+]'+NUM+r')%\s*\(\s*('+NUM+r')%\s*риск\)', re.I)
LOSS = re.compile(r'(-'+NUM+r')%\s*закрыто', re.I)
FOOTER = ('\n\n<a href="https://t.me/jrjrjrjrjrjrjrjrjrjrjrjrjrbot">пообщаться насчет сделки</a>'
          '\n\n<a href="https://t.me/c/3978977082/134">получать автоматический расчет позиции</a>')


def format_message(text):
    """Return (signal|tp|stop|bu, HTML). No other instruments, ad blocks or lot tables."""
    t = (text or '').replace('\xa0', ' ')
    m = HEAD.search(t)
    if m:
        entry, sl = ENTRY.search(t), SL.search(t)
        tps = dict(TPS.findall(t))
        if not entry or not sl or set(tps) != set('1234'):
            return None
        side = m[1].upper(); arrow = '📈' if side == 'LONG' else '📉'
        lines = [f'#GOLD {side} {arrow}', '', f'🔸 Вход сейчас или {entry[1]}',
                 f'🛑 SL {sl[1]}', ''] + [f'🎯 TP{i}  {tps[str(i)]}' for i in range(1,5)]
        return 'signal', '\n'.join(lines) + FOOTER
    m = HIT.search(t)
    final = FINAL.search(t)
    if m or final:
        profit = PROFIT.search(t)
        if not profit:
            return None
        arrow = '📉' if '📉' in t.splitlines()[0] else '📈'
        title = f'🏁 GOLD ФИНАЛЬНЫЙ TP ВЗЯТ {arrow}' if final else f'✅ GOLD TP {m[1]} ВЗЯТ {arrow}'
        body = f'{title}\n\n📈 Прибыль по сделке:\n{profit[1]}% ({profit[2]}% риск)'
        if re.search(r'Рекомендуем\s+перевести\s+позицию\s+в\s+безубыток', t, re.I):
            body += '\n\n⚡️ Рекомендуем перевести позицию в безубыток (БУ)'
        return 'tp', body + FOOTER
    if STOP.search(t):
        loss = LOSS.search(t)
        if not loss:
            return None
        return 'stop', f'❌ GOLD Сработал стоп-лосс\n\n📉 {loss[1]}% закрыто по этой сделке.\n\nРиск под контролем 🎯' + FOOTER
    if re.match(r'^\s*🛡', t) and re.search(r'\bXAUUSD\b', t, re.I) and re.search(r'остановлен\s+на\s+БУ', t, re.I):
        value = re.search(r'Зафиксировано:\s*([+-]?'+NUM+r')%', t, re.I)
        if value:
            return 'bu', f'🛡 GOLD ОСТАНОВЛЕН НА БУ\n\nЗафиксировано: {value[1]}%' + FOOTER
    return None


class Retra:
    def __init__(self, bot, db):
        self.bot, self.db = bot, db
        db.conn.execute('''CREATE TABLE IF NOT EXISTS retra_messages (
            route_id INTEGER NOT NULL, src_id INTEGER NOT NULL, dst_chat INTEGER NOT NULL,
            dst_id INTEGER NOT NULL, kind TEXT NOT NULL, photo INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(route_id,src_id))''')
        db.conn.commit()

    def mapped(self, route_id, src_id):
        return self.db.conn.execute('SELECT * FROM retra_messages WHERE route_id=? AND src_id=?',
                                    (route_id, src_id)).fetchone()

    def remember(self, route, src_id, dst_id, kind, photo=False):
        self.db.conn.execute('INSERT OR IGNORE INTO retra_messages VALUES (?,?,?,?,?,?)',
            (route['id'], src_id, route['target'], dst_id, kind, int(photo)))
        self.db.conn.commit()

    async def handle(self, route, msg, reader, edited=False):
        parsed = format_message(msg.message)
        if not parsed or getattr(msg, 'video', None) or getattr(msg, 'document', None):
            return
        kind, text = parsed
        old = self.mapped(route['id'], msg.id)
        if old:
            if edited:
                method = 'editMessageCaption' if old['photo'] else 'editMessageText'
                params = {'chat_id':old['dst_chat'], 'message_id':old['dst_id'], 'parse_mode':'HTML',
                          'caption' if old['photo'] else 'text':text}
                try:
                    await self.bot.call(method, **params)
                except RuntimeError as e:
                    if 'not modified' not in str(e):
                        raise
            return
        reply = None
        if kind != 'signal':
            parent = msg.reply_to.reply_to_msg_id if msg.reply_to else None
            old_parent = self.mapped(route['id'], parent) if parent else None
            if old_parent is None:
                log.warning('не найден исходный GOLD-сигнал для результата %s', msg.id)
                return
            reply = old_parent['dst_id']
        if getattr(msg, 'photo', None):
            data = await reader.download_media(msg, file=bytes)
            dst = await self.bot.send_photo(route['target'], data, text, reply_to=reply)
        else:
            dst = await self.bot.send(route['target'], text, reply_to=reply, html=True, strict_reply=True)
        self.remember(route, msg.id, dst, kind, bool(getattr(msg, 'photo', None)))
        log.info('%s %s -> %s', kind, msg.id, dst)
