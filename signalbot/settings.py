"""Настройки из панели бота: канал-источник и канал-приёмник для каждого источника."""

SOURCES = {
    1: "Источник 1 (основной)",
    2: "Источник 2 (Forex Bulls)",
    3: "Источник 3 (пока заглушка)",
}


def _chat(v):
    if v is None or v == "":
        return None
    return int(v) if str(v).lstrip("-").isdigit() else v


class Settings:
    def __init__(self, db):
        self.db = db

    def source(self, n: int):
        return _chat(self.db.get(f"src{n}.source"))

    def target(self, n: int):
        return _chat(self.db.get(f"src{n}.target"))

    def name(self, n: int, kind: str) -> str:
        """kind: source / target."""
        return self.db.get(f"src{n}.{kind}_name") or str(self.db.get(f"src{n}.{kind}") or "—")

    def set_chat(self, n: int, kind: str, chat_id, name: str):
        self.db.set(f"src{n}.{kind}", chat_id)
        self.db.set(f"src{n}.{kind}_name", name)

    def header(self) -> str:
        return self.db.get("src2.header", "")

    def set_header(self, text: str):
        self.db.set("src2.header", text)

    def source_of(self, chat_id):
        """Номер источника по ID чата, из которого пришло сообщение."""
        for n in SOURCES:
            if self.source(n) == chat_id:
                return n
        return None
