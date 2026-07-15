"""
Vercel Serverless Function — webhook endpoint для Telegram бота София.

Принимает POST-запросы от Telegram, обрабатывает update и возвращает 200 OK.

Ключевое: ВСЯ async работа выполняется в ОДНОМ asyncio.run() вызове,
чтобы избежать проблем с event loop на Vercel Serverless.
"""
import os
import sys
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


async def _process_request(update_data: dict):
    """Обрабатывает один запрос: инициализация + обработка update. Всё в одном event loop."""
    global _bot_app, _db_initialized
    import time as _time
    _t0 = _time.monotonic()

    # ─── Инициализация (один раз при cold start) ───
    if _bot_app is None:
        from bot.handlers import setup_handlers

        _bot_app = Application.builder().token(TOKEN).build()
        setup_handlers(_bot_app)
        await _bot_app.initialize()
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

    # ─── Обработка update ───
    update = Update.de_json(update_data, _bot_app.bot)
    if update is None:
        logger.warning("Received None update, skipping")
        return

    await _bot_app.process_update(update)
    _elapsed = _time.monotonic() - _t0
    logger.info(f"[TIMING] Total request processing: {_elapsed:.2f}s")


@app.route("/api/webhook", methods=["POST"])
def webhook():
    """Webhook endpoint для Telegram."""
    try:
        update_data = request.get_json(force=True)
        # ВСЯ async работа в одном asyncio.run() — один event loop
        asyncio.run(_process_request(update_data))
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
    """Health check endpoint с диагностикой."""
    llm_provider = os.getenv("LLM_PROVIDER", "gemini")
    llm_key_set = bool(os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY"))
    db_url_set = bool(os.getenv("DATABASE_URL"))
    token_set = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    llm_model = os.getenv("LLM_MODEL", "gemini-2.0-flash" if llm_provider == "gemini" else "unknown")
    return jsonify({
        "status": "ok",
        "service": "Sofia",
        "env_check": {
            "TELEGRAM_BOT_TOKEN": token_set,
            "LLM_PROVIDER": llm_provider,
            "LLM_API_KEY": llm_key_set,
            "LLM_MODEL": llm_model,
            "DATABASE_URL": db_url_set,
        },
        "bot_initialized": _bot_app is not None,
        "db_initialized": _db_initialized,
    }), 200


@app.route("/api/cron/daily", methods=["GET", "POST"])
def cron_daily():
    """Vercel Cron: отправляет ежедневные послания подписчикам.
    Ограничение: Vercel Serverless 10 сек. Поэтому обрабатываем батчами по 5 пользователей."""
    try:
        asyncio.run(_cron_daily_impl())
        return jsonify({"status": "ok", "sent": True}), 200
    except Exception as e:
        logger.error(f"[CRON DAILY ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200


async def _cron_daily_impl():
    """Отправляет ежедневные послания подписчикам (батч по 5)."""
    from bot.database import get_daily_horoscope_subscribers, save_message, mark_daily_horoscope_used
    from bot.gemini import generate_daily_horoscope

    # Инициализируем бота если нужно
    global _bot_app, _db_initialized
    if _bot_app is None:
        from bot.handlers import setup_handlers
        _bot_app = Application.builder().token(TOKEN).build()
        setup_handlers(_bot_app)
        await _bot_app.initialize()

    if not _db_initialized:
        from bot.database import init_db
        try:
            await init_db()
            _db_initialized = True
        except Exception as e:
            logger.error(f"Cron: DB init error: {e}")
            return

    subscribers = await get_daily_horoscope_subscribers()
    sent_count = 0

    for user in subscribers[:5]:  # Лимит 5 за один запуск (serverless timeout)
        user_id = user["user_id"]
        name = user.get("name") or user.get("first_name") or "милый человек"
        birth_date = user.get("birth_date")
        date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else (str(birth_date) if birth_date else "")

        try:
            from bot.database import get_emotional_memory
            emotional = await get_emotional_memory(user_id, min_importance=2)
            daily_msg = await generate_daily_horoscope(name=name, birth_date=date_str, emotional=emotional)

            # Отправляем сообщение через Telegram Bot API
            await _bot_app.bot.send_message(chat_id=user_id, text=daily_msg)
            await save_message(user_id, "sofia", daily_msg, "daily_horoscope_cron")
            await mark_daily_horoscope_used(user_id)
            sent_count += 1
            logger.info(f"Cron daily sent to {user_id}")
        except Exception as e:
            logger.error(f"Cron daily error for {user_id}: {e}")

    logger.info(f"Cron daily: sent {sent_count}/{len(subscribers[:5])} messages")


# Локальный тест
if __name__ == "__main__":
    app.run(debug=True, port=3000)
