"""Точка входа ORBot."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import config
from .db import db
from .handlers import router
from .runtime import models_cache


async def _prefetch_models() -> None:
    try:
        models = await models_cache.get(force=True)
        logging.info("Загружено бесплатных моделей: %d", len(models))
    except Exception as exc:  # noqa: BLE001
        logging.warning("Не удалось предзагрузить модели: %s", exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Политика no-logs: содержимое переписок нигде не пишется в логи.
    # Глушим библиотеки, которые могли бы залогировать тела запросов/сообщений.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    await db.connect()
    await _prefetch_models()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
