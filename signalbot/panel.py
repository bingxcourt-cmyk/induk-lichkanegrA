"""Панель управления в личке бота: /panel.

Для каждого источника админ выбирает канал-источник (читается аккаунтом) и
канал-приёмник (куда пишет бот). Для источника 2 — ещё текст строки-заголовка.
"""
import html

from .settings import SOURCES

PAGE = 8
KIND_RU = {"source": "канал-источник", "target": "канал-приёмник"}


def _btn(text, data):
    return {"text": text, "callback_data": data}


class Panel:
    def __init__(self, bot, db, settings, list_chats):
        self.bot, self.db, self.settings = bot, db, settings
        self.list_chats = list_chats  # async () -> [(chat_id, name)] из диалогов аккаунта

    # ---------------------------------------------------------------- экраны
    def main_screen(self):
        rows = []
        for n, title in SOURCES.items():
            rows.append([_btn(f"{title}: {self.settings.name(n, 'source')} → "
                              f"{self.settings.name(n, 'target')}", f"src:{n}")])
        rows.append([_btn("Заголовок сигнала (источник 2)", "hdr")])
        return "⚙️ Панель управления. Выберите источник:", {"inline_keyboard": rows}

    def source_screen(self, n: int):
        text = (f"<b>{SOURCES[n]}</b>\n\n"
                f"Источник: {html.escape(self.settings.name(n, 'source'))}\n"
                f"Приёмник: {html.escape(self.settings.name(n, 'target'))}")
        kb = [[_btn("Выбрать канал-источник", f"pick:{n}:source:0")],
              [_btn("Выбрать канал-приёмник", f"pick:{n}:target:0")],
              [_btn("« Назад", "main")]]
        return text, {"inline_keyboard": kb}

    async def pick_screen(self, n: int, kind: str, page: int):
        chats = await self.list_chats()
        chunk = chats[page * PAGE:(page + 1) * PAGE]
        kb = [[_btn(name[:60], f"set:{n}:{kind}:{cid}")] for cid, name in chunk]
        nav = []
        if page > 0:
            nav.append(_btn("‹", f"pick:{n}:{kind}:{page - 1}"))
        if (page + 1) * PAGE < len(chats):
            nav.append(_btn("›", f"pick:{n}:{kind}:{page + 1}"))
        if nav:
            kb.append(nav)
        kb.append([_btn("« Назад", f"src:{n}")])
        note = ("\n\nБот должен быть админом этого канала." if kind == "target" else "")
        return f"{SOURCES[n]}: выберите {KIND_RU[kind]}.{note}", {"inline_keyboard": kb}

    def header_prompt(self):
        cur = self.settings.header()
        text = ("Пришлите текст строки-заголовка для сигналов источника 2 одним сообщением.\n"
                f"Сейчас: {html.escape(cur) if cur else '— (без заголовка)'}\n\n"
                "/clear — убрать заголовок, /cancel — отмена")
        return text, {"inline_keyboard": [[_btn("« Назад", "main")]]}

    # ------------------------------------------------------------ обработка
    async def open(self, chat_id):
        text, kb = self.main_screen()
        await self.bot.send(chat_id, text, html=True, markup=kb)

    async def on_callback(self, cb):
        data = cb.get("data", "")
        msg = cb["message"]
        chat_id, mid = msg["chat"]["id"], msg["message_id"]
        parts = data.split(":")
        note = None
        if parts[0] == "main":
            screen = self.main_screen()
        elif parts[0] == "src":
            screen = self.source_screen(int(parts[1]))
        elif parts[0] == "pick":
            screen = await self.pick_screen(int(parts[1]), parts[2], int(parts[3]))
        elif parts[0] == "set":
            n, kind, cid = int(parts[1]), parts[2], int(parts[3])
            name = dict(await self.list_chats()).get(cid, str(cid))
            self.settings.set_chat(n, kind, cid, name)
            note = f"Сохранено: {name}"
            screen = self.source_screen(n)
        elif parts[0] == "hdr":
            self.db.set("await_header", "1")
            screen = self.header_prompt()
        else:
            screen = self.main_screen()
        await self.bot.answer_callback(cb["id"], note)
        await self.bot.edit(chat_id, mid, screen[0], html=True, markup=screen[1])

    async def on_text(self, chat_id, text: str) -> bool:
        """Текст от админа. True — сообщение обработано панелью."""
        if self.db.get("await_header") != "1":
            return False
        self.db.set("await_header", "0")
        cmd = text.strip().split("@")[0].lower()
        if cmd == "/cancel":
            await self.bot.send(chat_id, "Заголовок не изменён")
        elif cmd == "/clear":
            self.settings.set_header("")
            await self.bot.send(chat_id, "Заголовок убран")
        else:
            self.settings.set_header(text.strip())
            await self.bot.send(chat_id, f"Заголовок сохранён: {text.strip()}")
        await self.open(chat_id)
        return True
