"""Перевод реплаев на русский через Claude API."""
import logging

from .forexbulls import SKIP, TRANSLATE_PROMPT

log = logging.getLogger("translate")


class Translator:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = None
        if api_key:
            from anthropic import AsyncAnthropic  # импорт здесь, чтобы тесты шли без пакета
            self.client = AsyncAnthropic(api_key=api_key)

    async def translate(self, text: str):
        """Русский текст или None (не касается позиции / ключ не задан / ошибка)."""
        if self.client is None:
            log.warning("ANTHROPIC_API_KEY не задан — реплай не переведён")
            return None
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=1024,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low"},
                system=TRANSLATE_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
        except Exception:
            log.exception("ошибка Claude API")
            return None
        if resp.stop_reason == "refusal":
            log.warning("Claude отказался переводить: %r", text)
            return None
        out = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not out or out == SKIP:
            return None
        return out
