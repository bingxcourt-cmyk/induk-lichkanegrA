import json
import sqlite3
from decimal import Decimal

from .logic import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_msg_id  INTEGER UNIQUE,
    dst_msg_id  INTEGER,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,          -- ISO, МСК
    status      TEXT NOT NULL DEFAULT 'open',  -- open / closed / cancelled
    tps_hit     TEXT NOT NULL DEFAULT '[]',
    be_moved    INTEGER NOT NULL DEFAULT 0,
    cancel_due  TEXT                    -- когда отправить пост об отмене
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- tp / sl
    r           TEXT NOT NULL,
    day         TEXT NOT NULL,          -- YYYY-MM-DD, МСК
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS s2_signals (     -- источник 2 (Forex Bulls)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_msg_id  INTEGER UNIQUE,             -- сигнал или «BUY XAUUSD AGAIN NOW»
    dst_msg_id  INTEGER,
    direction   TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    entry       TEXT,                       -- цена FxPro на момент отправки, NULL — не было цены
    sl          TEXT NOT NULL,
    tp          TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / profit / stopped
    child_id    INTEGER,                    -- перезаход после стопа
    created_at  TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- signals
    def add_signal(self, src_id, dst_id, sig: Signal, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (src_msg_id, dst_msg_id, data, created_at) VALUES (?,?,?,?)",
            (src_id, dst_id, json.dumps(sig.to_dict()), created_at))
        self.conn.commit()
        return cur.lastrowid

    def by_src(self, src_id):
        return self.conn.execute("SELECT * FROM signals WHERE src_msg_id=?", (src_id,)).fetchone()

    def latest_open(self):
        return self.conn.execute(
            "SELECT * FROM signals WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()

    def open_signals(self):
        return self.conn.execute("SELECT * FROM signals WHERE status='open' ORDER BY id").fetchall()

    def created_on(self, day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE substr(created_at,1,10)=?", (day,)).fetchone()[0]

    def update_signal(self, sid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE signals SET {cols} WHERE id=?", (*fields.values(), sid))
        self.conn.commit()

    def due_cancels(self, now_iso):
        return self.conn.execute(
            "SELECT * FROM signals WHERE status='cancelled' AND cancel_due IS NOT NULL AND cancel_due<=?",
            (now_iso,)).fetchall()

    # --- events
    def add_event(self, sid, kind, r: Decimal, day: str, created_at: str):
        self.conn.execute(
            "INSERT INTO events (signal_id, kind, r, day, created_at) VALUES (?,?,?,?,?)",
            (sid, kind, str(r), day, created_at))
        self.conn.commit()

    def results(self, day_from: str, day_to: str) -> dict:
        """Итог по каждой позиции за период (по дате события)."""
        rows = self.conn.execute(
            "SELECT signal_id, kind, r FROM events WHERE day BETWEEN ? AND ?",
            (day_from, day_to)).fetchall()
        res = {}
        for row in rows:
            v = res.setdefault(row["signal_id"], {"r": Decimal(0), "sl": False})
            v["r"] += Decimal(row["r"])
            if row["kind"] == "sl":
                v["sl"] = True
        return res

    # --- источник 2
    def s2_add(self, src_id, dst_id, direction, symbol, entry, sl, tp, created_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO s2_signals (src_msg_id, dst_msg_id, direction, symbol, entry, sl, tp, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (src_id, dst_id, direction, symbol, None if entry is None else str(entry),
             str(sl), str(tp), created_at))
        self.conn.commit()
        return cur.lastrowid

    def s2_by_src(self, src_id):
        return self.conn.execute("SELECT * FROM s2_signals WHERE src_msg_id=?", (src_id,)).fetchone()

    def s2_latest(self, src_id):
        """Сигнал по id исходного сообщения; после перезахода — его актуальная версия."""
        row = self.s2_by_src(src_id)
        while row and row["child_id"]:
            row = self.conn.execute("SELECT * FROM s2_signals WHERE id=?", (row["child_id"],)).fetchone()
        return row

    def s2_update(self, sid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE s2_signals SET {cols} WHERE id=?", (*fields.values(), sid))
        self.conn.commit()

    # --- kv
    def get(self, k, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (k, str(v)))
        self.conn.commit()


def row_signal(row) -> Signal:
    return Signal.from_dict(json.loads(row["data"]))


def row_tps(row) -> list:
    return json.loads(row["tps_hit"])
