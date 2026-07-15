"""
Машина состояний (FSM) для бота София.

Концепция v2 — поток пользователя:
START → ASK_NAME → ASK_BIRTH_DATE → ASK_BIRTH_TIME → ASK_BIRTH_PLACE
→ PROBING (1 вопрос прощупывания) → FREE_READING (Карта судьбы + крючок)
→ CONVERSATION → PAID_HOOK → TARO_ASK_NUMBERS / SINGLE_CARD
→ TARO_SMALL / TARO_FULL / HOROSCOPE → CONVERSATION
"""
from enum import Enum
from datetime import datetime, timezone, timedelta


class SofiaState(str, Enum):
    """Все возможные состояния FSM бота."""

    START = "START"                          # Первый вход /start
    ASK_NAME = "ASK_NAME"                    # Спрашивает имя
    ASK_BIRTH_DATE = "ASK_BIRTH_DATE"        # Спрашивает дату рождения
    ASK_BIRTH_TIME = "ASK_BIRTH_TIME"        # Спрашивает время (можно пропустить)
    ASK_BIRTH_PLACE = "ASK_BIRTH_PLACE"      # Спрашивает место (можно пропустить)
    PROBING = "PROBING"                      # Прощупывание: 1 вопрос до Карты судьбы
    FREE_READING = "FREE_READING"            # Генерация Карты судьбы (бесплатно)
    CONVERSATION = "CONVERSATION"            # Свободный диалог
    PAID_HOOK = "PAID_HOOK"                  # София предлагает глубокий расклад
    TARO_ASK_NUMBERS = "TARO_ASK_NUMBERS"    # Бот просит числа для расклада
    TARO_SMALL = "TARO_SMALL"               # Малый расклад (5 карт)
    TARO_FULL = "TARO_FULL"                  # Полный расклад (20 карт)
    HOROSCOPE = "HOROSCOPE"                  # Персональный гороскоп
    BLOCKED = "BLOCKED"                      # Блокировка за грубость
    AWAITING_DELETE_CONFIRM = "AWAITING_DELETE_CONFIRM"  # GDPR: ожидание подтверждения удаления


# Текстовые триггеры для особых команд (без кнопок!)
MENU_TRIGGERS = {"меню", "menu", "помощь", "help"}
BALANCE_TRIGGERS = {"баланс", "balance", "кристаллы", "кристалл"}
PROFILE_TRIGGERS = {"профиль", "profile"}
HISTORY_TRIGGERS = {"история", "history"}
SORRY_TRIGGERS = {"извини", "прости", "извините", "простите", "извеняй", "сорри", "sorry"}
SKIP_TRIGGERS = {"пропустить", "пропуск", "skip", "далее", "дальше", "не знаю", "не помню"}

# Триггеры бесплатной 1-карты Таро (новое — по концепции «дать вкус»)
FREE_CARD_TRIGGERS = {
    "1 карта", "одна карта", "бесплатная карта", "бесплатно карта",
    "что важно", "что сейчас важно", "карта дня", "карта на сегодня",
    "одну карту", "вытяни карту", "карту",
}

# Карты для выбора расклада (текстовые триггеры, НЕ кнопки)
TARO_SMALL_TRIGGERS = {"малый", "малый расклад", "5 карт", "пять карт", "1 кристалл"}
TARO_FULL_TRIGGERS = {"полный", "полный расклад", "20 карт", "двадцать карт", "3 кристалла"}
HOROSCOPE_TRIGGERS = {"гороскоп", "зодиак", "2 кристалла"}

# Триггеры «узнать полностью / открыть глубже» (после хука)
DEEPER_TRIGGERS = {
    "узнать полностью", "открыть глубже", "хочу узнать", "давай",
    "расскажи", "покажи", "открой", "глубже", "полностью", "да хочу",
    "хочу", "продолжай", "продолжить",
}

# Маппинг триггеров грубости (для определения)
RUDENESS_PATTERNS = [
    "дурак", "идиот", "дебил", "тупой", "тупая", "дура", "дурной",
    "хрен", "блин", "чёрт", "черт", "бесит", "заткнись", "отстань",
    "надоел", "достал", "пошёл", "пошла", "нахрен", "к чёрту",
    "сука", "блять", "бля", "нахуй", "хуй", "пиздец", "пизда",
    "гавно", "говно", "жопа", "дерьмо", "соси", "отсоси"
]


def is_rude(text: str) -> bool:
    """Определяет, содержит ли текст грубость."""
    text_lower = text.lower().strip()
    for pattern in RUDENESS_PATTERNS:
        if pattern in text_lower:
            return True
    return False


def detect_reading_type(text: str) -> SofiaState | None:
    """Определяет тип расклада по текстовому триггеру."""
    text_lower = text.lower().strip()
    if any(t in text_lower for t in TARO_SMALL_TRIGGERS):
        return SofiaState.TARO_SMALL
    if any(t in text_lower for t in TARO_FULL_TRIGGERS):
        return SofiaState.TARO_FULL
    if any(t in text_lower for t in HOROSCOPE_TRIGGERS):
        return SofiaState.HOROSCOPE
    return None


def wants_deeper(text: str) -> bool:
    """Проверяет, хочет ли пользователь «узнать глубже» после хука."""
    text_lower = text.lower().strip()
    return any(t in text_lower for t in DEEPER_TRIGGERS)


def wants_free_card(text: str) -> bool:
    """Проверяет, просит ли пользователь бесплатную 1-карту."""
    text_lower = text.lower().strip()
    return any(t in text_lower for t in FREE_CARD_TRIGGERS)


def get_next_state(current: SofiaState) -> SofiaState:
    """Возвращает следующее состояние по порядку онбординга."""
    transitions = {
        SofiaState.START: SofiaState.ASK_NAME,
        SofiaState.ASK_NAME: SofiaState.ASK_BIRTH_DATE,
        SofiaState.ASK_BIRTH_DATE: SofiaState.ASK_BIRTH_TIME,
        SofiaState.ASK_BIRTH_TIME: SofiaState.ASK_BIRTH_PLACE,
        SofiaState.ASK_BIRTH_PLACE: SofiaState.PROBING,
        SofiaState.PROBING: SofiaState.FREE_READING,
        SofiaState.FREE_READING: SofiaState.CONVERSATION,
    }
    return transitions.get(current, SofiaState.CONVERSATION)


def is_long_absence(last_seen_at, absence_hours: int = 20) -> bool:
    """Определяет, был ли пользователь долго (больше absence_hours часов)."""
    if not last_seen_at:
        return False
    try:
        # Если last_seen_at naive — считаем UTC
        now = datetime.now(timezone.utc)
        if hasattr(last_seen_at, "tzinfo") and last_seen_at.tzinfo is None:
            last = last_seen_at.replace(tzinfo=timezone.utc)
        else:
            last = last_seen_at
        diff = (now - last).total_seconds()
        return diff >= absence_hours * 3600
    except Exception:
        return False


def infer_gender_from_name(name: str) -> str:
    """Грубая эвристика пола по русскому имени (окончание)."""
    if not name:
        return "unknown"
    n = name.strip().lower()
    # Типичные женские окончания
    if n.endswith(("а", "я", "ь")) and not n.endswith("ель"):
        # «Любовь» оканчивается на «ь» — женское; но «Игорь», «Шамиль» тоже. Учитываем исключения.
        male_soft = {"игорь", "шамиль", "кирилл", "павел", "ольгерд"}
        if n in male_soft:
            return "male"
        return "female"
    # Мужские
    male_names = {"саша", "женя", "вова", "слава"}
    if n in male_names:
        return "unknown"  # неоднозначно
    return "male"


def age_group_from_birth_date(birth_date) -> str:
    """Возвращает возрастную группу по дате рождения."""
    try:
        if not birth_date or not hasattr(birth_date, "year"):
            return "unknown"
        now = datetime.now(timezone.utc)
        age = now.year - birth_date.year
        # корректировка по дню года
        if (birth_date.month, birth_date.day) > (now.month, now.day):
            age -= 1
        if age < 18:
            return "young"
        if age < 30:
            return "young_adult"
        if age < 45:
            return "adult"
        if age < 60:
            return "mature"
        return "senior"
    except Exception:
        return "unknown"
