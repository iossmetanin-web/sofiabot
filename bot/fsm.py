"""
Машина состояний (FSM) для бота София.

Состояния диалога:
START → ASK_NAME → ASK_BIRTH_DATE → ASK_BIRTH_TIME → ASK_BIRTH_PLACE
→ FREE_READING → CONVERSATION → PAID_HOOK → TARO_SMALL / TARO_FULL / HOROSCOPE
"""
from enum import Enum


class SofiaState(str, Enum):
    """Все возможные состояния FSM бота."""

    START = "START"                          # Первый вход /start
    ASK_NAME = "ASK_NAME"                    # Спрашивает имя
    ASK_BIRTH_DATE = "ASK_BIRTH_DATE"        # Спрашивает дату рождения
    ASK_BIRTH_TIME = "ASK_BIRTH_TIME"        # Спрашивает время (можно пропустить)
    ASK_BIRTH_PLACE = "ASK_BIRTH_PLACE"      # Спрашивает место (можно пропустить)
    FREE_READING = "FREE_READING"            # Генерация Карты судьбы (бесплатно)
    CONVERSATION = "CONVERSATION"            # Свободный диалог
    PAID_HOOK = "PAID_HOOK"                  # София предлагает глубокий расклад
    TARO_ASK_NUMBERS = "TARO_ASK_NUMBERS"    # Бот просит числа для расклада
    TARO_SMALL = "TARO_SMALL"               # Малый расклад (5 карт)
    TARO_FULL = "TARO_FULL"                  # Полный расклад (20 карт)
    HOROSCOPE = "HOROSCOPE"                  # Персональный гороскоп
    BLOCKED = "BLOCKED"                      # Блокировка за грубость


# Текстовые триггеры для особых команд (без кнопок!)
MENU_TRIGGERS = {"меню", "menu", "помощь", "help"}
BALANCE_TRIGGERS = {"баланс", "balance", "кристаллы", "кристалл"}
PROFILE_TRIGGERS = {"профиль", "profile"}
HISTORY_TRIGGERS = {"история", "history"}
SORRY_TRIGGERS = {"извини", "прости", "извините", "простите", "извеняй", "сорри", "sorry"}
SKIP_TRIGGERS = {"пропустить", "пропуск", "skip", "далее", "дальше", "не знаю", "не помню"}

# Карты для выбора расклада (текстовые триггеры, НЕ кнопки)
TARO_SMALL_TRIGGERS = {"малый", "малый расклад", "5 карт", "пять карт", "1 кристалл"}
TARO_FULL_TRIGGERS = {"полный", "полный расклад", "20 карт", "двадцать карт", "3 кристалла"}
HOROSCOPE_TRIGGERS = {"гороскоп", "зодиак", "2 кристалла"}

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


def get_next_state(current: SofiaState) -> SofiaState:
    """Возвращает следующее состояние по порядку онбординга."""
    transitions = {
        SofiaState.START: SofiaState.ASK_NAME,
        SofiaState.ASK_NAME: SofiaState.ASK_BIRTH_DATE,
        SofiaState.ASK_BIRTH_DATE: SofiaState.ASK_BIRTH_TIME,
        SofiaState.ASK_BIRTH_TIME: SofiaState.ASK_BIRTH_PLACE,
        SofiaState.ASK_BIRTH_PLACE: SofiaState.FREE_READING,
        SofiaState.FREE_READING: SofiaState.CONVERSATION,
    }
    return transitions.get(current, SofiaState.CONVERSATION)
