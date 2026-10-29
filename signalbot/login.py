"""Однократный вход в аккаунт-читатель и вывод ID ваших каналов."""
import asyncio

from telethon import TelegramClient

from . import config


async def main():
    client = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
    await client.start()  # спросит телефон, код из Telegram и пароль 2FA
    me = await client.get_me()
    print(f"\nВход выполнен: {me.first_name} (id {me.id})\n\nКаналы и группы:")
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            print(f"  {d.id:>16}  {d.name}")
    print("\nКаналы для источников выбираются в боте командой /panel")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
