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

    # Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # Admin
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

    # Webhook
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "sofia_secret_2024")

    # Bot settings
    FREE_CRYSTALS_ON_START: int = 3
    TARO_SMALL_COST: int = 1      # Малый расклад — 1 кристалл
    TARO_FULL_COST: int = 3       # Полный расклад — 3 кристалла
    HOROSCOPE_COST: int = 2       # Гороскоп — 2 кристалла
    CONTEXT_MESSAGES_LIMIT: int = 20  # Последние N сообщений для контекста
    MEMORY_IMPORTANCE_THRESHOLD: int = 3  # Минимальная важность фактов
    MEMORY_EXTRACT_INTERVAL: int = 5  # Извлекать факты каждые N сообщений
    RATE_LIMIT_SECONDS: float = 2.0  # Минимальный интервал между сообщениями
    MAX_RUDENESS_BEFORE_BLOCK: int = 5  # Блокировка после N грубостей

    @classmethod
    def validate(cls) -> list[str]:
        """Проверяет, что все обязательные переменные заданы."""
        missing = []
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        if not cls.DATABASE_URL:
            missing.append("DATABASE_URL")
        return missing


# Глобальный экземпляр конфигурации
config = Config()
