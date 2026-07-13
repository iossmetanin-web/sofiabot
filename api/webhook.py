"""
Vercel Serverless Function — webhook endpoint для Telegram бота.

Принимает POST-запросы от Telegram, обрабатывает update и возвращает 200 OK.

URL: https://your-app.vercel.app/api/webhook

Используем asyncio.run() для каждого запроса — безопасно, т.к.
мы НЕ храним persistent соединения (БД открывается/закрывается внутри запроса).
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Глобальное приложение — создаётся при "cold start" Vercel
_application = None


def get_application():
    """Инициализирует Telegram Application при первом вызове (cold start)."""
    global _application
    if _application is None:
        from bot.handlers import setup_handlers
        from bot.database import init_db

        _application = Application.builder().token(TOKEN).build()
        setup_handlers(_application)
        # Создаём таблицы при первом запуске
        asyncio.run(init_db())
        logger.info("Telegram Application initialized (cold start)")
    return _application


@app.route("/api/webhook", methods=["POST"])
def webhook():
    """Webhook endpoint для Telegram."""
    try:
        application = get_application()
        update = Update.de_json(request.get_json(force=True), application.bot)

        # process_update — async, Flask — sync. asyncio.run() создаёт новый loop,
        # но мы НЕ храним persistent соединения (БД открывается/закрывается внутри),
        # поэтому это безопасно.
        asyncio.run(application.process_update(update))

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
