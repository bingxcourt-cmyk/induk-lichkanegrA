"""Разбор сообщений источника и формирование постов для своего канала.

Модуль не зависит от Telegram — его можно проверять тестами отдельно.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

NUM = r"(\d+(?:[.,]\d+)?)"

SYMBOL_ALIASES = {
    "XAUUSD": "GOLD",
    "GOLD": "GOLD",
    "XAGUSD": "SILVER",
}

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def D(s) -> Decimal:
    return Decimal(str(s).replace(",", "."))


def round2(x: Decimal) -> Decimal:
    """Математическое округление до сотых (0.205 -> 0.21)."""
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_price(x: Decimal) -> str:
    x = x.normalize()
    s = format(x, "f")
    return s


def fmt_r(x: Decimal, trim: bool = False) -> str:
    """+0.11R / -1R. trim=True убирает лишние нули (для итогов)."""
    x = round2(x)
    if trim:
        s = format(x.normalize(), "f")
        if x == 0:
            s = "0"
    else:
        s = f"{x:.2f}"
        if x == x.to_integral_value():
            s = format(x.to_integral_value(), "f")
    if x > 0:
        s = "+" + s
    return f"{s}R"


def orders_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ордер"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "ордера"
    return "ордеров"


def display_symbol(symbol: str) -> str:
    return SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())


# ---------------------------------------------------------------- сигнал

@dataclass
class Signal:
    direction: str          # BUY / SELL
    symbol: str             # XAUUSD
    entry_low: Decimal
    entry_high: Decimal
    sl: Decimal
    tps: list = field(default_factory=list)  # list[Decimal]

    @property
    def entry(self) -> Decimal:
        return (self.entry_low + self.entry_high) / 2

    @property
    def n(self) -> int:
        return len(self.tps)

    def full_r(self, tp: Decimal) -> Decimal:
        """R всей позиции на этом тейке (округлено до сотых)."""
        risk = abs(self.entry - self.sl)
        return round2(abs(tp - self.entry) / risk)

    def tp_r(self, idx: int) -> Decimal:
        """R за один ордер из n (idx с 1)."""
        return round2(self.full_r(self.tps[idx - 1]) / self.n)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction, "symbol": self.symbol,
            "entry_low": str(self.entry_low), "entry_high": str(self.entry_high),
            "sl": str(self.sl), "tps": [str(t) for t in self.tps],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Signal":
        return cls(d["direction"], d["symbol"], D(d["entry_low"]), D(d["entry_high"]),
                   D(d["sl"]), [D(t) for t in d["tps"]])


HEADER_RE = re.compile(
    r"\b(BUY|SELL)\b(?:\s+(?:LIMIT|STOP|NOW))?\s+#?([A-Z][A-Z0-9]{2,9})\b"
    r"|#?\b([A-Z][A-Z0-9]{2,9})\b\s+(BUY|SELL)\b",
    re.I,
)
TP_DEF_RE = re.compile(r"(?:take\s*profit|\bTP)\s*(\d+)\s*[:=\-–—]?\s*" + NUM, re.I)
SL_DEF_RE = re.compile(r"(?:stop\s*loss|\bSL\b)[^\d\n]*" + NUM, re.I)
ENTRY_KW_RE = re.compile(r"📍|entry|вход|\bat\b|@", re.I)


def parse_signal(text: str):
    if not text:
        return None
    m = HEADER_RE.search(text)
    if not m:
        return None
    if m.group(1):
        direction, symbol = m.group(1).upper(), m.group(2).upper()
    else:
        symbol, direction = m.group(3).upper(), m.group(4).upper()
    if symbol in ("TP", "SL"):
        return None

    tps = {}
    for tm in TP_DEF_RE.finditer(text):
        tps.setdefault(int(tm.group(1)), D(tm.group(2)))
    sl_m = SL_DEF_RE.search(text)
    if not tps or not sl_m:
        return None
    sl = D(sl_m.group(1))

    entry_nums = None
    lines = text.splitlines()
    for line in lines:
        if TP_DEF_RE.search(line) or SL_DEF_RE.search(line):
            continue
        if ENTRY_KW_RE.search(line):
            nums = re.findall(NUM, line)
            if nums:
                entry_nums = nums[:2]
                break
    if entry_nums is None:
        # Цена прямо в строке заголовка: "SELL XAUUSD 4018-4020"
        head_line = next(l for l in lines if HEADER_RE.search(l))
        rest = HEADER_RE.sub("", head_line)
        nums = re.findall(NUM, rest)
        if nums:
            entry_nums = nums[:2]
    if not entry_nums:
        return None

    vals = sorted(D(x) for x in entry_nums)
    sig = Signal(direction, symbol, vals[0], vals[-1], sl,
                 [tps[k] for k in sorted(tps)])
    if sig.entry == sig.sl:
        return None
    return sig


def format_signal(sig: Signal) -> str:
    side = "SHORT ⬇️" if sig.direction == "SELL" else "LONG ⬆️"
    n = sig.n
    if n == 1:
        orders = "открываем 1 ордер"
    else:
        orders = (f"открываем {n} равных {orders_word(n)}, дабы иметь возможность "
                  f"фиксировать по одному ордеру на каждом тейк-профите")
    lines = [
        f"#{display_symbol(sig.symbol)} {side}",
        "",
        orders,
        "",
        f"◻️Вход {fmt_price(sig.entry)}",
        "",
        f"👺SL {fmt_price(sig.sl)}",
        "",
    ]
    for i, tp in enumerate(sig.tps, 1):
        lines.append(f"✅TP{i}: {fmt_price(tp)} {fmt_r(sig.tp_r(i))}")
    return "\n".join(lines)


# ------------------------------------------------- ответы на сигнал

TP_CHANGE_RE = re.compile(r"(?:измени\w*|меняем|перенос\w*|move|change)\s+TP\s*(\d+)\s*(?:на|to)?\s*[:=\-–—]?\s*" + NUM, re.I)
TP_HIT_RE = re.compile(r"\bTP\s*(\d+)\s*[-–—:]+\s*" + NUM, re.I)
STOP_RE = re.compile(r"отстоп|стоп[\s-]*лосс\s+(?:сработал|выбило)|выбило\s+по\s+стоп|\bSL\s+hit\b|stopped\s+out", re.I)
BE_RE = re.compile(r"безубыт|\bБУ\b|breakeven|break\s*even", re.I)


def classify_reply(text: str):
    """Возвращает ('tp', idx, price) / ('chg', idx, price) / ('sl',) / ('be',) / None."""
    if not text:
        return None
    m = TP_CHANGE_RE.search(text)  # раньше тейка: "Измените TP3 - 4106" не должно стать "TP3 взят"
    if m:
        return ("chg", int(m.group(1)), D(m.group(2)))
    m = TP_HIT_RE.search(text)
    if m and not TP_DEF_RE.search(text.replace(m.group(0), "", 1)):
        return ("tp", int(m.group(1)), D(m.group(2)))
    if STOP_RE.search(text):
        return ("sl",)
    if BE_RE.search(text):
        return ("be",)
    return None


def format_tp_hit(sig: Signal, idx: int) -> str:
    return (f"🔥 TP{idx} взят - {fmt_price(sig.tps[idx - 1])} "
            f"{display_symbol(sig.symbol)}, {fmt_r(sig.tp_r(idx))}")


def format_tp_change(idx: int, price: Decimal) -> str:
    return f"🚨 меняем TP{idx} на {fmt_price(price)}"


def format_be(sig: Signal) -> str:
    return f"переставляем стоп-лосс в безубыток на точку входа {fmt_price(sig.entry)}"


def stop_result(sig: Signal, tps_hit: int, be_moved: bool) -> Decimal:
    """R, которые теряются на стопе по оставшимся ордерам."""
    if tps_hit == 0:
        return Decimal(-1)
    if be_moved:
        return Decimal(0)
    remaining = sig.n - tps_hit
    return round2(Decimal(-remaining) / sig.n)


def format_stop(sig: Signal, tps_hit: int, be_moved: bool) -> str:
    r = stop_result(sig, tps_hit, be_moved)
    if r == 0:
        return "🚨 сработал стоп-лосс в безубыток. 0R"
    return f"🚨 сработал стоп-лосс. {fmt_r(r)}"


CANCEL_TEXT = "отменяем ордера по этой позиции"


# ------------------------------------------------------- статистика

def ru_date(d) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def summarize(results: dict) -> tuple:
    """results: {signal_id: {'r': Decimal, 'sl': bool}} -> (profit, stops, be, total)."""
    profit = sum(1 for v in results.values() if v["r"] > 0)
    stops = sum(1 for v in results.values() if v["r"] < 0)
    be = sum(1 for v in results.values() if v["r"] == 0 and v["sl"])
    total = sum((v["r"] for v in results.values()), Decimal(0))
    return profit, stops, be, total


def format_stats(title: str, results: dict, total_label: str = "итог дня") -> str:
    profit, stops, be, total = summarize(results)
    lines = [title, "", f"✅профитных позиций - {profit}", "", f"🚨стоп-лоссов - {stops}", ""]
    if be:
        lines += [f"🤝в безубыток - {be}", ""]
    lines.append(f"🏁{total_label}: {fmt_r(total, trim=True)}")
    return "\n".join(lines)
