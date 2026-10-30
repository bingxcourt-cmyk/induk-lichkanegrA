"""Источник 2 (Forex Bulls Vip Trades): разбор сообщений и тексты постов.

Модуль не зависит от Telegram — его можно проверять тестами отдельно.
"""
import html
import re
from dataclasses import dataclass
from decimal import Decimal

from .logic import D, NUM, display_symbol, round2

BOT_LINK = "https://t.me/delaydengizdravo_bot"

SIGNAL_RE = re.compile(r"\b(BUY|SELL)\s+#?([A-Z]{3,10})\b(?!\s+AGAIN)", re.I)
SL_RE = re.compile(r"\bSL\b\s*[:=\-]?\s*" + NUM, re.I)
TP_RE = re.compile(r"\bTP\d?\b\s*[:=\-]?\s*" + NUM, re.I)
AGAIN_RE = re.compile(r"\b(BUY|SELL)\s+#?([A-Z]{3,10})\s+AGAIN\b", re.I)
CLOSE_RE = re.compile(r"\bclos(?:e|ing)\b[^\n]*\bprofits?\b", re.I)
LINK_RE = re.compile(r"https?://|www\.|t\.me/|@\w{4,}", re.I)
# реклама VIP и хвастовство результатами: «GOT PAID TODAY IN THE VIP», «join VIP», «DM me»
PROMO_RE = re.compile(r"\bvip\b|\bgot\s+paid\b|\bjoin\b|\bsubscri|\bdm\b|\binbox\b|\bmessage\s+me\b"
                      r"|\bpromo\b|\bdiscount\b|\bslots?\b|\bmembers?\b|\bpremium\b|\bmentorship\b"
                      r"|\btestimonial|\breview|\bwhatsapp\b|\bcontact\b", re.I)
SKIP = "SKIP"


@dataclass
class Signal2:
    direction: str      # BUY / SELL
    symbol: str         # XAUUSD
    sl: Decimal
    tp: Decimal


def parse_signal(text: str):
    """«BUY XAUUSD / SL 4019.957 / TP 4068.642» -> Signal2, иначе None."""
    if not text:
        return None
    m = SIGNAL_RE.search(text)
    sl, tp = SL_RE.search(text), TP_RE.search(text)
    if not (m and sl and tp):
        return None
    return Signal2(m.group(1).upper(), m.group(2).upper(), D(sl.group(1)), D(tp.group(1)))


def classify_reply(text: str):
    """Реплай на сигнал: ('again', BUY|SELL) / ('close',) / ('other',) / None (отбросить)."""
    text = (text or "").strip()
    if not text or LINK_RE.search(text) or PROMO_RE.search(text):
        return None
    m = AGAIN_RE.search(text)
    if m:
        return ("again", m.group(1).upper())
    if CLOSE_RE.search(text):
        return ("close",)
    if not re.search(r"[A-Za-zА-Яа-я]", text):
        return None
    return ("other",)


def price2(x: Decimal) -> str:
    return f"{round2(x):.2f}"


def pct(x: Decimal) -> str:
    """+0.53% / +2% — сотые, без лишних нулей."""
    s = format(round2(x).normalize(), "f")
    return f"+{s}%" if x > 0 else f"{s}%"


def format_signal(direction: str, symbol: str, entry, sl: Decimal, tp: Decimal,
                  header: str = "") -> str:
    """HTML-текст поста. entry=None — цену получить не удалось, пишем «по рынку»."""
    side = "SHORT" if direction == "SELL" else "LONG"
    lines = []
    if header.strip():
        lines.append(html.escape(header.strip()))
    lines += [
        f"#{display_symbol(symbol)} {side}",
        f"Вход {price2(entry)}" if entry is not None else "Вход по рынку",
        f"SL: {price2(sl)}",
        f"Takeprofit: {price2(tp)}",
        f'в <a href="{BOT_LINK}">этом</a> боте отображено точное количество лотов, на которое '
        "нужно открыть позицию, дабы при стопе ты потерял ровно 1% от своего депозита",
    ]
    return "\n\n".join(lines)  # каждая строка — отдельным абзацем


def profit_pct(entry: Decimal, sl: Decimal, price: Decimal, risk: Decimal = Decimal(1)) -> Decimal:
    """Прибыль в % депозита при риске risk% на сделку."""
    return risk * abs(price - entry) / abs(entry - sl)


def format_close(entry, sl: Decimal, price) -> str:
    text = "закрываем сделку в профит по текущей цене."
    if entry is None or price is None or entry == sl:
        return text
    return f"{text} {pct(profit_pct(entry, sl, price))} к депозиту"


STOP_TEXT = "🚨 сработал стоп-лосс. -1% к депозиту"

TRANSLATE_PROMPT = (
    "Ты переводишь сообщения трейдера из канала с сигналами по золоту (XAUUSD). "
    "Сообщение — ответ на открытую позицию. Пропускай только то, что говорит подписчику, "
    "что делать с этой сделкой или что с ней происходит: держать, закрыть часть, перенести стоп, "
    "добавиться, тейк или стоп близко, цена идёт к цели и т.п. Такое переведи на русский "
    "разговорным языком трейдера, коротко, без пояснений и кавычек; тикеры и числа оставь как есть. "
    "Всё остальное — реклама VIP-канала или платной подписки, хвастовство прибылью и отзывы "
    "(«got paid today», «another win for VIP»), приветствия, мотивация, болтовня — "
    f"ответь ровно {SKIP}. Если сомневаешься, тоже {SKIP}."
)
