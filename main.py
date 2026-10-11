"""Запасная точка входа для хостингов, которые запускают main.py. Делает то же, что bot.py."""
import asyncio

from bot import main

if __name__ == "__main__":
    asyncio.run(main())
