"""Минимальный клиент Telegram Bot API: отправка постов и команды админа."""
import asyncio
import logging

import aiohttp

log = logging.getLogger("botapi")


class BotAPI:
    def __init__(self, token: str):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.session = None

    async def _call(self, method: str, **params):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=70))
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(5):
            try:
                async with self.session.post(self.url + method, json=params) as r:
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("%s: сеть (%s), повтор", method, e)
                await asyncio.sleep(2 ** attempt)
                continue
            if data.get("ok"):
                return data["result"]
            retry = data.get("parameters", {}).get("retry_after")
            if retry:
                await asyncio.sleep(retry + 1)
                continue
            raise RuntimeError(f"{method}: {data.get('description')}")
        raise RuntimeError(f"{method}: не удалось после повторов")

    async def call(self, method: str, **params):
        return await self._call(method, **params)

    async def send(self, chat_id, text: str, reply_to: int = None, markup: dict = None,
                   html: bool = False) -> int:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True,
                  "reply_markup": markup, "parse_mode": "HTML" if html else None}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        msg = await self._call("sendMessage", **params)
        return msg["message_id"]

    async def edit(self, chat_id, message_id: int, text: str, markup: dict = None):
        try:
            await self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                             reply_markup=markup, disable_web_page_preview=True)
        except RuntimeError as e:
            if "not modified" not in str(e):
                raise

    async def updates(self, offset: int):
        return await self._call("getUpdates", offset=offset, timeout=50,
                                allowed_updates=["message", "callback_query"])

    async def answer(self, callback_id: str, text: str = None):
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except RuntimeError:
            pass  # кнопка устарела — не страшно

    async def me(self) -> dict:
        if not hasattr(self, "_me"):
            self._me = await self._call("getMe")
        return self._me

    async def close(self):
        if self.session:
            await self.session.close()
