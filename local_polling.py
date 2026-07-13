#!/usr/bin/env python3
"""
Локальный запуск бота София в режиме polling.

Использование:
    python local_polling.py

Для локальной разработки не нужен Vercel — бот сам опрашивает Telegram.
"""
import asyncio
import logging
import sys

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Запуск бота в режиме polling."""
    from config import config

    # Проверяем конфигурацию
    missing = config.validate()
    if missing:
        logger.error(f"❌ Не заданы обязательные переменные: {', '.join(missing)}")
        logger.error("Создай файл .env на основе .env.example")
        sys.exit(1)

    logger.info("🚀 Запуск бота София (polling mode)...")

    # Инициализируем БД
    try:
        from bot.database import init_db
        await init_db()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        logger.error("Проверь DATABASE_URL в .env")
        sys.exit(1)

    # Создаём Application через setup_handlers (как в webhook.py)
    try:
        from telegram.ext import Application
        from bot.handlers import setup_handlers

        application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        setup_handlers(application)
        await application.initialize()
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook удалён, переход в polling")
    except Exception as e:
        logger.error(f"❌ Не удалось инициализировать бота: {e}")
        sys.exit(1)

    # Запускаем polling
    try:
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
        logger.info("👵 София слушает... Нажми Ctrl+C для остановки")

        # Бесконечный цикл
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("⏹ Остановка бота...")
    finally:
        try:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
        except Exception:
            pass

        logger.info("👋 Бот остановлен. До встречи!")


if __name__ == "__main__":
    asyncio.run(main())
