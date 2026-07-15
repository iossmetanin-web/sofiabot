"""
Vercel Serverless Function — webhook endpoint для Telegram бота София.

Принимает POST-запросы от Telegram, обрабатывает update и возвращает 200 OK.

Ключевое: ВСЯ async работа выполняется в ОДНОМ asyncio.run() вызове,
чтобы избежать проблем с event loop на Vercel Serverless.

Round 4: добавлен /api/cron/checkin для отправки mood-проверок неактивным пользователям.
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


async def _ensure_initialized():
    """Инициализация бота и БД (вызывается из всех эндпоинтов)."""
    global _bot_app, _db_initialized

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


async def _send_early_typing(update_data: dict) -> None:
    """Отправляет chat_action=typing НЕМЕДЛЕННО, до полной инициализации бота.

    Решает проблему cold-start задержки: на Vercel Python serverless
    `_ensure_initialized()` + import modules может занимать 2-4 секунды,
    в течение которых пользователь не видит реакции бота.

    Используем прямой httpx-запрос к Telegram API (не зависит от _bot_app).
    Если что-то пошло не так — тихо игнорируем (это лишь UX-улучшение).
    """
    try:
        # Достаём chat_id из update
        msg = update_data.get("message") or update_data.get("edited_message")
        if not msg:
            cb = update_data.get("callback_query")
            if cb and cb.get("message"):
                msg = cb["message"]
        if not msg:
            return
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            return

        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return

        # Прямой запрос к Telegram API, без инициализации Application
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
    except Exception:
        # Это лишь UX-улучшение — не падаем, если не получилось
        pass


async def _process_request(update_data: dict):
    """Обрабатывает один запрос: инициализация + обработка update. Всё в одном event loop."""
    import time as _time
    _t0 = _time.monotonic()

    # ─── Ранний typing — ДО инициализации бота ───
    # Запускаем параллельно с _ensure_initialized, чтобы пользователь
    # увидел «печатает...» сразу (в пределах ~500мс), а не через 2-4 секунды cold start.
    typing_task = asyncio.create_task(_send_early_typing(update_data))

    await _ensure_initialized()

    # ─── Обработка update ───
    update = Update.de_json(update_data, _bot_app.bot)
    if update is None:
        logger.warning("Received None update, skipping")
        typing_task.cancel()
        return

    await _bot_app.process_update(update)

    # Ждём завершения typing-задачи (она уже должна была отправиться, но для надёжности)
    try:
        await asyncio.wait_for(typing_task, timeout=1.0)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass

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
    from bot.database import get_daily_horoscope_subscribers, save_message, mark_daily_horoscope_used, get_emotional_memory
    from bot.gemini import generate_daily_horoscope
    from bot.fsm import get_zodiac_sign

    await _ensure_initialized()

    subscribers = await get_daily_horoscope_subscribers()
    sent_count = 0

    for user in subscribers[:5]:
        user_id = user["user_id"]
        name = user.get("name") or user.get("first_name") or "милый человек"
        birth_date = user.get("birth_date")
        date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else (str(birth_date) if birth_date else "")

        # Зодиак
        zodiac_name, zodiac_symbol = get_zodiac_sign(birth_date) if birth_date and hasattr(birth_date, "month") else ("", "")
        zodiac = f"{zodiac_symbol} {zodiac_name}" if zodiac_name else ""

        try:
            emotional = await get_emotional_memory(user_id, min_importance=2)
            daily_msg = await generate_daily_horoscope(name=name, birth_date=date_str, emotional=emotional, zodiac=zodiac)

            await _bot_app.bot.send_message(chat_id=user_id, text=daily_msg)
            await save_message(user_id, "sofia", daily_msg, "daily_horoscope_cron")
            await mark_daily_horoscope_used(user_id)
            sent_count += 1
            logger.info(f"Cron daily sent to {user_id}")
        except Exception as e:
            logger.error(f"Cron daily error for {user_id}: {e}")

    logger.info(f"Cron daily: sent {sent_count}/{len(subscribers[:5])} messages")


@app.route("/api/cron/checkin", methods=["GET", "POST"])
def cron_checkin():
    """Vercel Cron: отправляет mood-проверки пользователям, которые давно не заходили.
    Запускается раз в 3 дня. Лимит — 3 пользователя за запуск (serverless timeout)."""
    try:
        asyncio.run(_cron_checkin_impl())
        return jsonify({"status": "ok", "sent": True}), 200
    except Exception as e:
        logger.error(f"[CRON CHECKIN ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200


async def _cron_checkin_impl():
    """Отправляет mood-проверки неактивным пользователям (батч 3)."""
    from bot.database import get_conn, save_message
    from bot.gemini import generate_mood_checkin
    from bot.fsm import get_zodiac_sign

    await _ensure_initialized()

    # Находим пользователей, которые были активны более 3 дней назад, но менее 14 дней
    # и завершили онбординг, не заблокированы
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            SELECT user_id, name, first_name, birth_date, last_topic_summary
            FROM users
            WHERE onboarding_completed = TRUE
              AND is_blocked = FALSE
              AND last_seen_at < NOW() - INTERVAL '3 days'
              AND last_seen_at > NOW() - INTERVAL '14 days'
            ORDER BY last_seen_at ASC
            LIMIT 3
        """)
    finally:
        await conn.close()

    sent_count = 0
    for row in rows:
        user_id = row["user_id"]
        name = row.get("name") or row.get("first_name") or "милый человек"
        last_topic = row.get("last_topic_summary") or ""
        birth_date = row.get("birth_date")

        zodiac_name, zodiac_symbol = get_zodiac_sign(birth_date) if birth_date and hasattr(birth_date, "month") else ("", "")
        zodiac = f"{zodiac_symbol} {zodiac_name}" if zodiac_name else ""

        try:
            from bot.database import get_emotional_memory
            emotional = await get_emotional_memory(user_id, min_importance=2)
            checkin_msg = await generate_mood_checkin(name, emotional, last_topic, zodiac)

            await _bot_app.bot.send_message(chat_id=user_id, text=checkin_msg)
            await save_message(user_id, "sofia", checkin_msg, "cron_mood_checkin")
            sent_count += 1
            logger.info(f"Cron checkin sent to {user_id}")
        except Exception as e:
            logger.error(f"Cron checkin error for {user_id}: {e}")

    logger.info(f"Cron checkin: sent {sent_count}/{len(rows)} messages")


@app.route("/api/cron/birthday", methods=["GET", "POST"])
def cron_birthday():
    """Vercel Cron: отправляет поздравления с днём рождения.
    Запускается ежедневно в 9:00. Именинников обычно мало, лимит 10."""
    try:
        asyncio.run(_cron_birthday_impl())
        return jsonify({"status": "ok", "sent": True}), 200
    except Exception as e:
        logger.error(f"[CRON BIRTHDAY ERROR] {e}\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200


async def _cron_birthday_impl():
    """Отправляет поздравления с днём рождения пользователям, у которых сегодня др."""
    from bot.database import get_birthday_users, save_message, get_emotional_memory
    from bot.gemini import generate_birthday_greeting
    from bot.fsm import get_zodiac_sign
    from datetime import date as date_cls

    await _ensure_initialized()

    birthday_users = await get_birthday_users(limit=10)
    if not birthday_users:
        logger.info("Cron birthday: no birthdays today")
        return

    sent_count = 0
    today = date_cls.today()
    for user in birthday_users:
        user_id = user["user_id"]
        name = user.get("name") or user.get("first_name") or "милый человек"
        birth_date = user.get("birth_date")
        birth_year = user.get("birth_year")

        # Возраст
        age = None
        if birth_date and hasattr(birth_date, "year"):
            age = today.year - birth_date.year

        # Зодиак
        zodiac_name, zodiac_symbol = get_zodiac_sign(birth_date) if birth_date and hasattr(birth_date, "month") else ("", "")
        zodiac = f"{zodiac_symbol} {zodiac_name}" if zodiac_name else ""

        try:
            emotional = await get_emotional_memory(user_id, min_importance=2)
            greeting = await generate_birthday_greeting(name, age=age, zodiac=zodiac, emotional=emotional)

            await _bot_app.bot.send_message(chat_id=user_id, text=greeting)
            await save_message(user_id, "sofia", greeting, "cron_birthday")
            sent_count += 1
            logger.info(f"Cron birthday sent to {user_id} (age={age})")
        except Exception as e:
            logger.error(f"Cron birthday error for {user_id}: {e}")

    logger.info(f"Cron birthday: sent {sent_count}/{len(birthday_users)} messages")


# Локальный тест
if __name__ == "__main__":
    app.run(debug=True, port=3000)
