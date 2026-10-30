"""Источник 2 (Forex Bulls Vip Trades): разбор сообщений и тексты постов.

Форматы взяты из истории канала за полгода. Пересылаем золото и все пары канала.
Модуль не зависит от Telegram — его можно проверять тестами отдельно.
"""
import html
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .logic import D, NUM, round2

BOT_LINK = "https://t.me/delaydengizdravo_bot"

# инструменты канала: XAUUSD/GOLD, XAGUSD/SILVER, валютные пары и их сокращения («GJ» — GBPJPY)
SYMBOL_RE = re.compile(r"\b(XAUUSD|XAU|GOLD|XAGUSD|XAG|SILVER|(?:USD|EUR|GBP|AUD|NZD|CAD|CHF)"
                       r"(?:USD|JPY|GBP|EUR|CHF|CAD|AUD|NZD)|GJ|GU|EU|UJ|US30|NAS100|BTCUSD)\b", re.I)
ALIASES = {"XAU": "XAUUSD", "GOLD": "XAUUSD", "XAG": "XAGUSD", "SILVER": "XAGUSD",
           "GJ": "GBPJPY", "GU": "GBPUSD", "EU": "EURUSD", "UJ": "USDJPY"}
DISPLAY = {"XAUUSD": "GOLD", "XAGUSD": "SILVER"}
DIR_RE = re.compile(r"\b(buys?|sells?)\b", re.I)
SIDE_RE = re.compile(r"\b(long|short)\b(?!\s+term)", re.I)  # «short term buys» — это покупки
SL_RE = re.compile(r"\b(?:SL|stop\s*-?\s*loss)\b[^\d\n]{0,12}?" + NUM, re.I)
TP_RE = re.compile(r"\b(?:TP|take\s*-?\s*profit)\b[^\d\n]{0,12}?" + NUM, re.I)
TP_OPEN_RE = re.compile(r"\b(?:TP|take\s*-?\s*profit)\b\s*[:;=\-~]?\s*open\b", re.I)

AGAIN_RE = re.compile(r"\b(BUY|SELL)\s+#?[A-Z0-9]{2,7}\s+AGAIN\b", re.I)
REENTRY_RE = re.compile(r"\bre-?\s?entry\b|\bre-?\s?enter\b|\brecovery\s+(?:entry|setup)\b"
                        r"|\b(?:buy|sell)\w*\s+[a-z0-9]{2,7}\s+again\b", re.I)
LATER_RE = re.compile(r"\b(?:coming|soon|shortly|incoming|later|hour|tomorrow|next\s+week|if\s+you\s+hadn"
                      r"|sending\s+in|send\s+in|will)\b", re.I)
TP_HIT_RE = re.compile(r"\b(?:tp|take\s*-?\s*profit)\b[^\n]{0,15}?\b(?:hit|smashed|reached|done|secured)\b", re.I)
SL_HIT_RE = re.compile(r"\b(?:sl|stop\s*-?\s*loss)\b[^\n]{0,15}?\b(?:hit|triggered)\b|\btaken?\s+out\b"
                       r"|\btakes?\s+out\s+with\s+a\s+loss\b|\bstopped\s+out\b", re.I)
SL_MOVE_RE = re.compile(r"\b(?:adjust|modify|move|extend|set|trail|shift)\w*\b[^\n]{0,25}?"
                        r"\b(?:sl|stop\s*-?\s*loss)\b[^\d\n]{0,12}?" + NUM, re.I)
BE_RE = re.compile(r"\b(?:sl|stop\s*-?\s*loss)\b[^\n]{0,20}\b(?:entry|break\s*-?even|be)\b", re.I)
CLOSE_PROFIT_RE = re.compile(r"\bclos(?:e|ing)\b[^\n]*\bprofits?\b", re.I)
PARTIAL_RE = re.compile(r"\bpartial|\bhalf\b|\bpart\s+of\b", re.I)
CLOSE_RE = re.compile(r"\bclos(?:e|ing)\s+(?:off|the\s+(?:trade|buys|sells|position)|of\b|it\b)"
                      r"|\binvalidated\b", re.I)  # сетап отменён — сделка тоже закрыта

LINK_RE = re.compile(r"https?://|www\.|t\.me/|@(?!current\b)\w{4,}", re.I)  # «@current price» — не ссылка
# реклама VIP и хвастовство результатами: «GOT PAID TODAY IN THE VIP», «join VIP», «DM me»
PROMO_RE = re.compile(r"\bvip\b|\bgot\s+paid\b|\bgetting\s+paid\b|\bjoin\b|\bsubscri|\bdm\b|\binbox\b"
                      r"|\bmessage\s+me\b|\bpromo\b|\bdiscount\b|\bslots?\b|\bmembers?\b|\bpremium\b"
                      r"|\bmentorship\b|\btestimonial|\breview|\bwhatsapp\b|\bcontact\b", re.I)
SKIP = "SKIP"


@dataclass
class Signal2:
    direction: str          # BUY / SELL
    symbol: str             # XAUUSD, GBPJPY…
    sl: Decimal
    tp: Optional[Decimal]   # None — тейк открытый


def symbol_of(text: str):
    """Первый инструмент в тексте (XAUUSD, GBPJPY…) или None."""
    m = SYMBOL_RE.search(text or "")
    if not m:
        return None
    sym = m.group(1).upper()
    return ALIASES.get(sym, sym)


def display_symbol(symbol: str) -> str:
    return DISPLAY.get(symbol, symbol)


def direction_of(text: str):
    m = DIR_RE.search(text or "") or SIDE_RE.search(text or "")
    if not m:
        return None
    return "BUY" if m.group(1).lower() in ("buy", "buys", "long") else "SELL"


def levels(text: str):
    """(SL, TP) из текста; None, если уровня нет или тейк открытый."""
    sl, tp = SL_RE.search(text), TP_RE.search(text)
    return (D(sl.group(1)) if sl else None,
            D(tp.group(1)) if tp and not TP_OPEN_RE.search(text) else None)


def parse_signal(text: str):
    """Новый сигнал: направление и SL обязательны, TP может быть открытым; без инструмента — золото.

    «BUY XAUUSD / SL 4019.957 / TP 4068.642», «SELLS ON XAUUSD. SL … TP …», «XAUUSD BUYS SL …»,
    «Let’s buy XAUUSD Stop loss … Take profit; …», «Sell XAUUSD SL … TP open».
    """
    if not text or AGAIN_RE.search(text):
        return None
    direction = direction_of(text)
    sl, tp = levels(text)
    if not direction or sl is None:
        return None
    return Signal2(direction, symbol_of(text) or "XAUUSD", sl, tp)


def classify_reply(text: str):
    """Сообщение о сделке. Возвращает кортеж или None (отбросить):

    ('again', BUY|SELL, sl|None, tp|None) — «BUY XAUUSD AGAIN NOW»: стоп + новый сигнал;
    ('reentry', BUY|SELL|None, sl|None, tp|None) — перезаход: новый сигнал;
    ('tp',) / ('sl',) — тейк или стоп сработал;
    ('close',) — закрыть в профит; ('closeoff',) — закрыть без профита (переводим);
    ('move', price|None, tp|None) — перенос стопа (None — в безубыток) и, если есть, тейка; тоже переводим;
    ('other',) — прочее, решает перевод через Claude.
    """
    text = (text or "").strip()
    if not text or LINK_RE.search(text) or PROMO_RE.search(text):
        return None
    m = AGAIN_RE.search(text)
    if m:
        return ("again", m.group(1).upper(), *levels(text))
    if SL_HIT_RE.search(text):
        return ("sl",)
    if TP_HIT_RE.search(text):
        return ("tp",)
    if REENTRY_RE.search(text) and not LATER_RE.search(text):
        return ("reentry", direction_of(text), *levels(text))
    if CLOSE_PROFIT_RE.search(text) and not PARTIAL_RE.search(text):
        return ("close",)
    if CLOSE_RE.search(text) and not PARTIAL_RE.search(text):
        return ("closeoff",)
    m = SL_MOVE_RE.search(text)
    if m:
        return ("move", D(m.group(1)), levels(text)[1])
    if BE_RE.search(text):
        return ("move", None, None)
    if not re.search(r"[A-Za-zА-Яа-я]", text):
        return None
    return ("other",)


def price2(x: Decimal) -> str:
    return f"{round2(x):.2f}"


def digits(symbol: str, sl: Decimal) -> int:
    """Знаков после запятой: золото — сотые, остальное — как в уровнях сигнала (216.622, 1.35466)."""
    if symbol == "XAUUSD":
        return 2
    return max(-sl.as_tuple().exponent, 2)


def fmt_level(x: Decimal, n: int) -> str:
    return f"{x.quantize(Decimal(1).scaleb(-n), rounding='ROUND_HALF_UP'):.{n}f}"


def pct(x: Decimal) -> str:
    """+0.53% / +2% / -1% — сотые, без лишних нулей."""
    s = format(round2(x).normalize(), "f")
    if s in ("0", "-0"):
        return "0%"
    return f"+{s}%" if x > 0 else f"{s}%"


def format_signal(direction: str, symbol: str, entry, sl: Decimal, tp, header: str = "") -> str:
    """HTML-текст поста. entry=None — цену получить не удалось, пишем «по рынку»."""
    side = "SHORT" if direction == "SELL" else "LONG"
    n = digits(symbol, sl)
    lines = []
    if header.strip():
        lines.append(html.escape(header.strip()))
    lines += [
        f"#{display_symbol(symbol)} {side}",
        f"Вход {fmt_level(entry, n)}" if entry is not None else "Вход по рынку",
        f"SL: {fmt_level(sl, n)}",
        f"Takeprofit: {fmt_level(tp, n)}" if tp is not None else "Takeprofit: открытый",
        f'в <a href="{BOT_LINK}">этом</a> боте отображено точное количество лотов, на которое '
        "нужно открыть позицию, дабы при стопе ты потерял ровно 1% от своего депозита",
    ]
    return "\n\n".join(lines)  # каждая строка — отдельным абзацем


def profit_pct(entry: Decimal, sl: Decimal, price: Decimal, risk: Decimal = Decimal(1)) -> Decimal:
    """Прибыль в % депозита при риске risk% на сделку."""
    return risk * abs(price - entry) / abs(entry - sl)


def result_pct(direction: str, entry: Decimal, sl0: Decimal, price: Decimal) -> Decimal:
    """Результат со знаком: (цена − вход) в риске исходного стопа, 1R = 1% депозита."""
    sign = 1 if direction == "BUY" else -1
    return sign * (price - entry) / abs(entry - sl0)


def format_close(entry, sl: Decimal, price) -> str:
    text = "закрываем сделку в профит по текущей цене."
    if entry is None or price is None or entry == sl:
        return text
    return f"{text} {pct(profit_pct(entry, sl, price))} к депозиту"


def format_tp(entry, sl0: Decimal, price) -> str:
    text = "🔥 тейк-профит взят."
    if entry is None or price is None or entry == sl0:
        return text
    return f"{text} {pct(profit_pct(entry, sl0, price))} к депозиту"


def format_stop(direction: str, entry, sl0: Decimal, sl: Decimal) -> str:
    """Стоп сработал. Если стоп переносили, результат считаем по новому стопу."""
    if entry is None or sl == sl0 or entry == sl0:
        return STOP_TEXT
    r = result_pct(direction, entry, sl0, sl)
    if round2(r) == 0:
        return "сработал стоп-лосс в безубыток. 0% к депозиту"
    return f"🚨 сработал стоп-лосс. {pct(r)} к депозиту"


STOP_TEXT = "🚨 сработал стоп-лосс. -1% к депозиту"

TRANSLATE_PROMPT = (
    "Ты переводишь сообщения трейдера из канала с сигналами по золоту (XAUUSD) и валютным парам. "
    "Сообщение — ответ на открытую позицию. Пропускай только то, что говорит подписчику, "
    "что делать с этой сделкой или что с ней происходит: держать, закрыть часть, перенести стоп, "
    "добавиться, тейк или стоп близко, цена идёт к цели и т.п. Такое переведи на русский "
    "разговорным языком трейдера, коротко и точно по смыслу, ничего не добавляя, без пояснений и кавычек; "
    "тикеры и числа оставь как есть. «Close off» значит закрыть сделку целиком, не часть. "
    "Всё остальное — реклама VIP-канала или платной подписки, хвастовство прибылью и отзывы "
    "(«got paid today», «another win for VIP»), приветствия, мотивация, болтовня — "
    f"ответь ровно {SKIP}. Если сомневаешься, тоже {SKIP}."
)
