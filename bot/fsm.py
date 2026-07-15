"""
Машина состояний (FSM) для бота София.

Концепция v2 — поток пользователя:
START → ASK_NAME → ASK_BIRTH_DATE → ASK_BIRTH_TIME → ASK_BIRTH_PLACE
→ PROBING (1 вопрос прощупывания) → FREE_READING (Карта судьбы + крючок)
→ CONVERSATION → PAID_HOOK → TARO_ASK_NUMBERS / SINGLE_CARD
→ TARO_SMALL / TARO_FULL / HOROSCOPE → CONVERSATION

Round 4: зодиакальное определение, имена карт Таро, расширенные триггеры.
"""
from enum import Enum
from datetime import datetime, timezone, timedelta, date as date_type


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
    TARO_LOVE = "TARO_LOVE"                  # Round 5: расклад на любовь (3 карты)
    TARO_CAREER = "TARO_CAREER"              # Round 5: расклад на дело (5 карт)
    TARO_DECISION = "TARO_DECISION"          # Round 5: расклад на выбор (3 карты)
    HOROSCOPE = "HOROSCOPE"                  # Персональный гороскоп
    BLOCKED = "BLOCKED"                      # Блокировка за грубость
    AWAITING_DELETE_CONFIRM = "AWAITING_DELETE_CONFIRM"  # GDPR: ожидание подтверждения удаления
    BROADCAST = "BROADCAST"                  # Round 5: админ пишет текст рассылки


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
    "одну карту", "вытяни карту", "карту", "карта",
}

# Карты для выбора расклада (текстовые триггеры, НЕ кнопки)
TARO_SMALL_TRIGGERS = {"малый", "малый расклад", "5 карт", "пять карт", "1 кристалл"}
TARO_FULL_TRIGGERS = {"полный", "полный расклад", "20 карт", "двадцать карт", "3 кристалла"}
HOROSCOPE_TRIGGERS = {"гороскоп", "зодиак", "2 кристалла"}

# Round 5: новые тематические расклады
TARO_LOVE_TRIGGERS = {
    "расклад на любовь", "на любовь", "любовь", "отношения",
    "расклад на отношения", "сердце", "он она", "партнёр",
}
TARO_CAREER_TRIGGERS = {
    "расклад на дело", "на дело", "карьера", "работа", "дело",
    "расклад на работу", "бизнес", "профессия", "деньги",
}
TARO_DECISION_TRIGGERS = {
    "расклад на выбор", "на выбор", "выбор", "да нет",
    "что выбрать", "как решить", "решение", "или",
}

# Round 5: карта дня
CARD_OF_DAY_TRIGGERS = {
    "карта дня", "карта на день", "карта сегодняшнего дня",
    "сегодняшняя карта", "карту дня",
}

# Общий триггер «хочу расклад / погадай» — без указания конкретного типа.
# Срабатывает, когда пользователь явно просит расклад, но не назвал тип.
# ВАЖНО: должны идти ПОСЛЕ проверок detect_reading_type / wants_free_card /
# wants_card_of_day в роутинге, чтобы не перехватывать конкретные запросы.
READING_REQUEST_TRIGGERS = {
    "расклад", "погадай", "погадать", "гадание", "таро",
    "выложи карты", "разложи карты", "давай карты", "карты таро",
    "хочу расклад", "сделай расклад", "давай расклад", "нужен расклад",
    "мне нужен расклад", "можно расклад", "хочу погадать", "покажи карты",
    "вытяни карты", "давай погадаем", "хочу карты", "покажи расклад",
}

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
    if any(t in text_lower for t in TARO_FULL_TRIGGERS):
        return SofiaState.TARO_FULL
    if any(t in text_lower for t in TARO_SMALL_TRIGGERS):
        return SofiaState.TARO_SMALL
    if any(t in text_lower for t in HOROSCOPE_TRIGGERS):
        return SofiaState.HOROSCOPE
    if any(t in text_lower for t in TARO_LOVE_TRIGGERS):
        return SofiaState.TARO_LOVE
    if any(t in text_lower for t in TARO_CAREER_TRIGGERS):
        return SofiaState.TARO_CAREER
    if any(t in text_lower for t in TARO_DECISION_TRIGGERS):
        return SofiaState.TARO_DECISION
    return None


def wants_card_of_day(text: str) -> bool:
    """Проверяет, просит ли пользователь «карту дня» (Round 5)."""
    text_lower = text.lower().strip()
    return any(t in text_lower for t in CARD_OF_DAY_TRIGGERS)


def wants_deeper(text: str) -> bool:
    """Проверяет, хочет ли пользователь «узнать глубже» после хука."""
    text_lower = text.lower().strip()
    return any(t in text_lower for t in DEEPER_TRIGGERS)


def wants_free_card(text: str) -> bool:
    """Проверяет, просит ли пользователь бесплатную 1-карту."""
    text_lower = text.lower().strip()
    return any(t in text_lower for t in FREE_CARD_TRIGGERS)


def wants_reading(text: str) -> bool:
    """Проверяет, просит ли пользователь расклад Таро БЕЗ указания конкретного типа.

    ВАЖНО: caller должен сначала проверить detect_reading_type / wants_free_card /
    wants_card_of_day — иначе общий триггер перехватит конкретный запрос.
    """
    text_lower = text.lower().strip()
    return any(t in text_lower for t in READING_REQUEST_TRIGGERS)


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


# ─── Зодиакальное определение ───

def get_zodiac_sign(birth_date) -> tuple[str, str]:
    """Определяет знак зодиака по дате рождения.
    Возвращает (название, символ) или ('', '') при ошибке."""
    try:
        if not birth_date or not hasattr(birth_date, "month"):
            return ("", "")
        month = birth_date.month
        day = birth_date.day

        # Границы знаков: ((начало_м, начало_д), (конец_м, конец_д), название, символ)
        zodiac_dates = [
            ((3, 21), (4, 19), "Овен", "♈"),
            ((4, 20), (5, 20), "Телец", "♉"),
            ((5, 21), (6, 20), "Близнецы", "♊"),
            ((6, 21), (7, 22), "Рак", "♋"),
            ((7, 23), (8, 22), "Лев", "♌"),
            ((8, 23), (9, 22), "Дева", "♍"),
            ((9, 23), (10, 22), "Весы", "♎"),
            ((10, 23), (11, 21), "Скорпион", "♏"),
            ((11, 22), (12, 21), "Стрелец", "♐"),
            ((12, 22), (1, 19), "Козерог", "♑"),
            ((1, 20), (2, 18), "Водолей", "♒"),
            ((2, 19), (3, 20), "Рыбы", "♓"),
        ]

        for (s_m, s_d), (e_m, e_d), name, symbol in zodiac_dates:
            # Знаки, не пересекающие границу года
            if s_m <= e_m:
                if (month == s_m and day >= s_d) or (month == e_m and day <= e_d):
                    return (name, symbol)
            else:
                # Козерог: 22 декабря — 19 января (пересекает год)
                if (month == s_m and day >= s_d) or (month == e_m and day <= e_d):
                    return (name, symbol)

        return ("", "")
    except Exception:
        return ("", "")


# ─── Названия карт Таро по номеру (Райдер-Уэйт) ───

TAROT_MAJOR_ARCANA = [
    "Шут", "Маг", "Верховная Жрица", "Императрица", "Император",
    "Иерофант", "Влюблённые", "Колесница", "Сила", "Отшельник",
    "Колесо Фортуны", "Справедливость", "Повешенный", "Смерть", "Умеренность",
    "Дьявол", "Башня", "Звезда", "Луна", "Солнце",
    "Суд", "Мир",
]

TAROT_SUIT_NAMES = {
    "wands": ("Жезлов", ["Туз", "Двойка", "Тройка", "Четвёрка", "Пятёрка",
                        "Шестёрка", "Семёрка", "Восьмёрка", "Девятка", "Десятка",
                        "Паж", "Рыцарь", "Королева", "Король"]),
    "cups": ("Кубков", ["Туз", "Двойка", "Тройка", "Четвёрка", "Пятёрка",
                       "Шестёрка", "Семёрка", "Восьмёрка", "Девятка", "Десятка",
                       "Паж", "Рыцарь", "Королева", "Король"]),
    "swords": ("Мечей", ["Туз", "Двойка", "Тройка", "Четвёрка", "Пятёрка",
                        "Шестёрка", "Семёрка", "Восьмёрка", "Девятка", "Десятка",
                        "Паж", "Рыцарь", "Королева", "Король"]),
    "pentacles": ("Пентаклей", ["Туз", "Двойка", "Тройка", "Четвёрка", "Пятёрка",
                              "Шестёрка", "Семёрка", "Восьмёрка", "Девятка", "Десятка",
                              "Паж", "Рыцарь", "Королева", "Король"]),
}


def get_tarot_card_name(number: int) -> str:
    """Возвращает название карты Таро по номеру (1-78, Райдер-Уэйт).
    1-22: Старшие Арканы (1=Шут... 22=Мир)
    23-36: Жезлы
    37-50: Кубки
    51-64: Мечи
    65-78: Пентаклей
    """
    if number < 1 or number > 78:
        return f"Карта #{number}"

    if number <= 22:
        return TAROT_MAJOR_ARCANA[number - 1]

    # Младшие Арканы: 14 карт в каждой масти
    suit_order = ["wands", "cups", "swords", "pentacles"]
    suit_idx = (number - 23) // 14
    card_idx = (number - 23) % 14

    if suit_idx < len(suit_order):
        suit_name, rank_names = TAROT_SUIT_NAMES[suit_order[suit_idx]]
        if card_idx < len(rank_names):
            return f"{rank_names[card_idx]} {suit_name}"

    return f"Карта #{number}"
