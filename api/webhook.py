"""
Vercel Serverless Function — webhook endpoint для Telegram бота София.

Принимает POST-запросы от Telegram, обрабатывает update и возвращает 200 OK.

URL: https://sofiabot-git-main-iossmetanin-webs-projects.vercel.app/api/webhook

Безопасная работа с asyncio на Vercel Serverless:
- _bot_app и _db_initialized — глобальные, переживают warm starts
- asyncio.run() — безопасен, т.к. Flask sync и нового loop не существует
- БД открывает/закрывает соединение на каждый запрос (connection-per-request)
"""
import os
import asyncio
import json
import logging
import traceback

from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Глобальные объекты — инициализируются один раз при cold start, переживают warm starts
_bot_app: Application | None = None
_db_initialized: bool = False


async def _init():
    """Инициализация Telegram Application и БД. Вызывается один раз."""
    global _bot_app, _db_initialized

    if _bot_app is None:
        from bot.handlers import setup_handlers

        _bot_app = Application.builder().token(TOKEN).build()
        setup_handlers(_bot_app)
        logger.info("Telegram Application initialized (cold start)")

    if not _db_initialized:
        from bot.database import init_db
        try:
            await init_db()
            _db_initialized = True
            logger.info("Database tables initialized")
        except Exception as e:
            logger.error(f"Database init error: {e}")
            # Не падаем — таблицы могут уже существовать


@app.route("/api/webhook", methods=["POST"])
def webhook():
    """Webhook endpoint для Telegram."""
    try:
        # Инициализация при первом запросе
        asyncio.run(_init())

        # Обработка update
        update = Update.de_json(request.get_json(force=True), _bot_app.bot)
        asyncio.run(_bot_app.process_update(update))

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"[WEBHOOK ERROR] {e}\n{traceback.format_exc()}")
        # Возвращаем 200, чтобы Telegram не ретраил
        return jsonify({"status": "error"}), 200


@app.route("/api/webhook", methods=["GET"])
def webhook_info():
    """Информация о webhook (для проверки)."""
    return "👵 София ждёт ваших сообщений. Webhook активен.", 200


@app.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "Sofia"}), 200


# Локальный тест
if __name__ == "__main__":
    app.run(debug=True, port=3000)
