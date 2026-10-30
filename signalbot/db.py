import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from .logic import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       INTEGER NOT NULL,
    source_title TEXT,
    target       INTEGER NOT NULL,
    target_title TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL          -- смена источника = новое перенаправление = статистика с нуля
);
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL,
    src_msg_id  INTEGER NOT NULL,
    dst_chat    INTEGER NOT NULL,       -- канал публикации можно сменить, ответы идут туда, где пост
    dst_msg_id  INTEGER,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,          -- ISO, МСК
    status      TEXT NOT NULL DEFAULT 'open',  -- open / closed / cancelled / archived
    tps_hit     TEXT NOT NULL DEFAULT '[]',
    be_moved    INTEGER NOT NULL DEFAULT 0,
    cancel_due  TEXT,                   -- когда отправить пост об отмене
    UNIQUE (route_id, src_msg_id)
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
CREATE TABLE IF NOT EXISTS s2_signals (     -- сигналы перенаправлений с правилами «Forex Bulls»
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL,
    src_msg_id  INTEGER NOT NULL,           -- сигнал или «BUY XAUUSD AGAIN NOW»
    dst_chat    INTEGER NOT NULL,
    dst_msg_id  INTEGER,
    direction   TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    entry       TEXT,                       -- цена на момент отправки, NULL — цены не было
    sl          TEXT NOT NULL,              -- текущий стоп (после переносов)
    tp          TEXT NOT NULL,              -- '' — тейк открытый
    status      TEXT NOT NULL DEFAULT 'open',  -- open / profit / stopped / closed / reentered / archived
    child_id    INTEGER,                    -- перезаход после стопа
    created_at  TEXT NOT NULL,
    UNIQUE (route_id, src_msg_id)
);
CREATE TABLE IF NOT EXISTS s2_links (       -- реплай на реплай: сообщение источника -> его сигнал
    route_id    INTEGER NOT NULL,
    src_msg_id  INTEGER NOT NULL,
    signal_id   INTEGER NOT NULL,
    PRIMARY KEY (route_id, src_msg_id)
);
"""

RULES = {
    "standard": "Стандарт (источник 1)",
    "forexbulls": "Forex Bulls (источник 2)",
    "stub": "Заглушка (источник 3)",
}


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate_single_channel()
        self.conn.executescript(SCHEMA)
        self._migrate_rules()

    def _migrate_rules(self):
        """У перенаправления появились свои правила и строка-заголовок."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(routes)")}
        if "rules" not in cols:
            self.conn.execute("ALTER TABLE routes ADD COLUMN rules TEXT NOT NULL DEFAULT 'standard'")
        if "header" not in cols:
            self.conn.execute("ALTER TABLE routes ADD COLUMN header TEXT NOT NULL DEFAULT ''")
        if "sl0" not in {r[1] for r in self.conn.execute("PRAGMA table_info(s2_signals)")}:
            self.conn.execute("ALTER TABLE s2_signals ADD COLUMN sl0 TEXT")  # стоп из сигнала, для расчёта 1R
        self.conn.commit()

    def _migrate_single_channel(self):
        """Старая схема (один источник и один канал в kv) -> перенаправление №1."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(signals)")}
        if not cols or "route_id" in cols:
            return
        kv = dict(self.conn.execute("SELECT k, v FROM kv").fetchall())
        self.conn.execute("ALTER TABLE signals RENAME TO signals_old")
        self.conn.executescript(SCHEMA)
        if kv.get("source") and kv.get("target"):
            first = self.conn.execute("SELECT MIN(created_at) FROM signals_old").fetchone()[0]
            self.conn.execute(
                "INSERT INTO routes (id, source, source_title, target, target_title, created_at)"
                " VALUES (1,?,?,?,?,?)",
                (int(kv["source"]), kv.get("source_title"), int(kv["target"]), kv.get("target_title"),
                 first or datetime.now().astimezone().isoformat()))
        self.conn.execute(
            "INSERT INTO signals (id, route_id, src_msg_id, dst_chat, dst_msg_id, data, created_at,"
            " status, tps_hit, be_moved, cancel_due) SELECT id, 1, src_msg_id, dst_chat, dst_msg_id,"
            " data, created_at, status, tps_hit, be_moved, cancel_due FROM signals_old")
        self.conn.execute("DROP TABLE signals_old")
        self.conn.execute("DELETE FROM kv WHERE k IN ('source','source_title','target','target_title')")
        self.conn.commit()

    # --- routes
    def routes(self):
        return self.conn.execute("SELECT * FROM routes WHERE active=1 ORDER BY id").fetchall()

    def route(self, rid):
        return self.conn.execute("SELECT * FROM routes WHERE id=? AND active=1", (rid,)).fetchone()

    def add_route(self, source, source_title, target, target_title, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO routes (source, source_title, target, target_title, created_at) VALUES (?,?,?,?,?)",
            (source, source_title, target, target_title, created_at))
        self.conn.commit()
        return cur.lastrowid

    def update_route(self, rid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE routes SET {cols} WHERE id=?", (*fields.values(), rid))
        self.conn.commit()

    def close_route(self, rid):
        """Перенаправление больше не ведём: открытые сигналы не отслеживаются, статистика уходит."""
        self.conn.execute("UPDATE routes SET active=0 WHERE id=?", (rid,))
        self.conn.execute("UPDATE signals SET status='archived' WHERE route_id=? AND status='open'", (rid,))
        self.conn.execute("UPDATE s2_signals SET status='archived' WHERE route_id=? AND status='open'", (rid,))
        self.conn.commit()

    # --- signals
    def add_signal(self, route_id, src_id, dst_chat, dst_id, sig: Signal, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (route_id, src_msg_id, dst_chat, dst_msg_id, data, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (route_id, src_id, dst_chat, dst_id, json.dumps(sig.to_dict()), created_at))
        self.conn.commit()
        return cur.lastrowid

    def by_src(self, route_id, src_id):
        return self.conn.execute("SELECT * FROM signals WHERE route_id=? AND src_msg_id=?",
                                 (route_id, src_id)).fetchone()

    def open_signals(self, route_id=None):
        if route_id is None:
            return self.conn.execute("SELECT * FROM signals WHERE status='open' ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM signals WHERE status='open' AND route_id=? ORDER BY id",
                                 (route_id,)).fetchall()

    def created_on(self, day: str, route_id) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE substr(created_at,1,10)=? AND route_id=?",
            (day, route_id)).fetchone()[0]

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

    def results(self, day_from: str, day_to: str, route_id) -> dict:
        """Итог по каждой позиции перенаправления за период (по дате события)."""
        rows = self.conn.execute(
            "SELECT e.signal_id, e.kind, e.r FROM events e JOIN signals s ON s.id = e.signal_id"
            " WHERE e.day BETWEEN ? AND ? AND s.route_id=?",
            (day_from, day_to, route_id)).fetchall()
        res = {}
        for row in rows:
            v = res.setdefault(row["signal_id"], {"r": Decimal(0), "sl": False})
            v["r"] += Decimal(row["r"])
            if row["kind"] == "sl":
                v["sl"] = True
        return res

    # --- сигналы «Forex Bulls»
    def s2_add(self, route_id, src_id, dst_chat, dst_id, direction, symbol, entry, sl, tp,
               created_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO s2_signals (route_id, src_msg_id, dst_chat, dst_msg_id, direction, symbol,"
            " entry, sl, sl0, tp, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (route_id, src_id, dst_chat, dst_id, direction, symbol,
             None if entry is None else str(entry), str(sl), str(sl), "" if tp is None else str(tp),
             created_at))
        self.conn.commit()
        return cur.lastrowid

    def s2_by_src(self, route_id, src_id):
        return self.conn.execute("SELECT * FROM s2_signals WHERE route_id=? AND src_msg_id=?",
                                 (route_id, src_id)).fetchone()

    def s2_latest(self, route_id, src_id):
        """Сигнал по id сообщения источника (сам сигнал или любой ответ в его ветке);
        после перезахода — его актуальная версия."""
        row = self.s2_by_src(route_id, src_id)
        if row is None:
            link = self.conn.execute("SELECT signal_id FROM s2_links WHERE route_id=? AND src_msg_id=?",
                                     (route_id, src_id)).fetchone()
            if link:
                row = self.conn.execute("SELECT * FROM s2_signals WHERE id=?", (link[0],)).fetchone()
        while row and row["child_id"]:
            row = self.conn.execute("SELECT * FROM s2_signals WHERE id=?", (row["child_id"],)).fetchone()
        return row

    def s2_link(self, route_id, src_id, signal_id):
        self.conn.execute("INSERT OR IGNORE INTO s2_links (route_id, src_msg_id, signal_id) VALUES (?,?,?)",
                          (route_id, src_id, signal_id))
        self.conn.commit()

    def s2_open(self, route_id):
        return self.conn.execute("SELECT * FROM s2_signals WHERE route_id=? AND status='open' ORDER BY id",
                                 (route_id,)).fetchall()

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
