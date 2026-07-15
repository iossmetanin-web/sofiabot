"""
Конфигурация проекта София — загрузка переменных окружения.
"""
import os
from pathlib import Path

# Загрузка .env файла для локальной разработки
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=env_path)
except ImportError:
    pass  # На Vercel переменные задаются через dashboard


class Config:
    """Централизованный доступ к переменным окружения."""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # LLM Provider: "gemini" (default), "groq", "openrouter"
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")

    # Gemini (legacy, still works)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # Admin
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

    # Webhook
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "sofia_secret_2024")

    # ─── Bot settings ───
    FREE_CRYSTALS_ON_START: int = 3
    TARO_SMALL_COST: int = 1
    TARO_FULL_COST: int = 3
    HOROSCOPE_COST: int = 2
    CONTEXT_MESSAGES_LIMIT: int = 8
    MEMORY_IMPORTANCE_THRESHOLD: int = 3
    MEMORY_EXTRACT_INTERVAL: int = 5
    RATE_LIMIT_SECONDS: float = 2.0
    MAX_RUDENESS_BEFORE_BLOCK: int = 5

    # ─── Концепция v2: поток и вовлечение ───
    PROBING_ROUNDS: int = 1              # сколько вопросов прощупывания до Карты судьбы
    RETURN_ABSENCE_HOURS: int = 20       # через сколько часов absence — София «вспоминала о тебе»
    FREE_CARD_COOLDOWN_HOURS: int = 24   # как часто доступна бесплатная 1-карта Таро
    PAID_HOOK_MIN_MESSAGES: int = 6      # с какого номера сообщения возможен платный хук
    PAID_HOOK_EVERY: int = 7             # как часто повторять платный хук (каждые N сообщений после минимума)
    MAX_TARO_NUMBER: int = 78
    MOOD_CHECKIN_INTERVAL_HOURS: int = 72  # через сколько часов можно отправить /mood check-in
    CRON_DAILY_BATCH: int = 5            # сколько пользователей обрабатывает cron за запуск

    # ─── Round 5: новые расклады, карта дня, рассылка, дни рождения ───
    TARO_LOVE_COST: int = 2              # расклад на любовь (3 карты)
    TARO_CAREER_COST: int = 2            # расклад на дело (5 карт)
    TARO_DECISION_COST: int = 2          # расклад на выбор (3 карты)
    CARD_OF_DAY_COOLDOWN_HOURS: int = 20 # как часто доступна «карта дня»
    CRON_CHECKIN_BATCH: int = 3          # батч cron checkin
    CRON_BIRTHDAY_BATCH: int = 10        # батч cron birthday (именинников обычно мало)
    BROADCAST_BATCH: int = 25            # сколько пользователей за один запрос в /broadcast
    BROADCAST_RATE_MS: int = 35          # задержка между сообщениями в рассылке (Telegram ~30 msg/sec)

    # ─── Round 6: коммерческая модель ───
    DAILY_FREE_MESSAGES: int = 10        # сколько бесплатных сообщений диалога в день
    DAILY_COST_CRYSTALS: int = 1         # цена за дополнительный пакет сообщений после лимита (5 шт)
    DAILY_PACKAGE_SIZE: int = 5          # сколько сообщений в платном пакете
    TODAY_COST: int = 1                  # /today — ежедневное послание
    MOOD_COST: int = 1                   # /mood — проверка настроения
    ADMIN_CONTACT: str = os.getenv("ADMIN_CONTACT", "@admin_username")  # контакт для пополнения
    PAYMENT_INSTRUCTIONS: str = os.getenv(
        "PAYMENT_INSTRUCTIONS",
        "Для пополнения кристаллов напиши администратору {admin}.\n\n"
        "Тарифы:\n"
        "• 10 💎 — 199 ₽\n"
        "• 25 💎 — 449 ₽\n"
        "• 50 💎 — 799 ₽ (выгоднее)\n"
        "• 100 💎 — 1490 ₽ (самый выгодный)\n\n"
        "Оплата: перевод на карту / ЮMoney / крипта. "
        "После оплаты кристаллы начисляются в течение 10 минут."
    )

    @classmethod
    def validate(cls) -> list[str]:
        """Проверяет, что все обязательные переменные заданы."""
        missing = []
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.LLM_API_KEY and not cls.GEMINI_API_KEY:
            missing.append("LLM_API_KEY (или GEMINI_API_KEY)")
        if not cls.DATABASE_URL:
            missing.append("DATABASE_URL")
        return missing


# Глобальный экземпляр конфигурации
config = Config()
