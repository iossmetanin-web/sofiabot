"""
Обработчики Telegram для бота София.

Все обработчики сообщений и команд:
- /start — приветствие, переход в ASK_NAME
- /profile — профиль и баланс
- /balance — баланс кристаллов
- /admin — панель администратора
- Обработка текста по состояниям FSM
- Обработка грубости
- Специальные текстовые команды (меню, баланс, извини)

Адаптировано под connection-per-request (database.py) и httpx (gemini.py).
"""
import logging
import re
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import config
from bot.fsm import (
    SofiaState,
    is_rude,
    detect_reading_type,
    get_next_state,
    MENU_TRIGGERS,
    BALANCE_TRIGGERS,
    PROFILE_TRIGGERS,
    HISTORY_TRIGGERS,
    SORRY_TRIGGERS,
    SKIP_TRIGGERS,
)
from bot import database as db
from bot import memory
from bot.gemini import (
    generate_response,
    generate_fate_card,
    generate_taro_reading,
    generate_horoscope,
    detect_topic,
)

logger = logging.getLogger(__name__)

# ─────────────────── Rate Limiting (через БД) ───────────────────


async def _check_rate_limit(user_id: int) -> bool:
    """Проверяет rate limit через БД. Возвращает True, если можно отправить сообщение."""
    allowed = await db.check_rate_limit(user_id, config.RATE_LIMIT_SECONDS)
    if allowed:
        await db.update_rate_limit(user_id)
    return allowed


# ─────────────────── Хелперы ───────────────────

async def _send_long_message(update: Update, text: str, max_length: int = 4096) -> None:
    """Отправляет длинное сообщение, разбивая на части если нужно."""
    if len(text) <= max_length:
        await update.message.reply_text(text)
        return

    parts = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > max_length:
            if current:
                parts.append(current)
            current = paragraph
        else:
            current = current + "\n\n" + paragraph if current else paragraph
    if current:
        parts.append(current)

    for part in parts:
        await update.message.reply_text(part)


# ─────────────────── Грубость ───────────────────

RUDENESS_RESPONSES = [
    "Слова бывают тяжелее камней. Попробуй сказать то же самое без злобы.",
    "Я понимаю, что тяжело. Но грубость редко помогает услышать друг друга.",
    "Мне неприятно продолжать в таком тоне.",
    "Похоже, сегодня разговор не складывается.",
    "Я не хочу продолжать, пока ты говоришь так. Если захочешь поговорить спокойно — я буду здесь.",
]


# ─────────────────── Команда /start ───────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name

    user = await db.get_or_create_user(user_id, username, first_name)

    # Если пользователь уже был — перезапускаем
    if user.get("message_count", 0) > 0:
        await update.message.reply_text(
            f"Здравствуй снова, {user.get('name') or first_name or 'милый человек'}. "
            f"Соскучилась по тебе. Что привело тебя ко мне сегодня?"
        )
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await db.save_message(user_id, "sofia", "Здравствуй снова. Соскучилась.", "greeting")
        return

    # Первый вход
    greeting = (
        "Здравствуй, милый человек. Меня зовут София. "
        "Не знаю, что именно привело тебя сюда сегодня, "
        "но случайных встреч бывает меньше, чем нам кажется.\n\n"
        "Как тебя зовут?"
    )
    await update.message.reply_text(greeting)
    await db.update_user_state(user_id, SofiaState.ASK_NAME)
    await db.save_message(user_id, "sofia", greeting, "greeting")
    logger.info(f"User {user_id} started the bot")


# ─────────────────── Команда /profile ───────────────────

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает профиль и баланс кристаллов."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.message.reply_text("Напиши /start, чтобы начать.")
        return

    name = user.get("name") or user.get("first_name") or "Не указано"
    birth_date = user.get("birth_date", "Не указана")
    birth_time = user.get("birth_time", "Не указано")
    birth_place = user.get("birth_place", "Не указано")
    crystals = user.get("crystals", 0)
    msg_count = user.get("message_count", 0)
    created = user.get("created_at", "Неизвестно")

    if birth_date and hasattr(birth_date, "strftime"):
        birth_date = birth_date.strftime("%d.%m.%Y")
    if birth_time and hasattr(birth_time, "strftime"):
        birth_time = birth_time.strftime("%H:%M")

    profile_text = (
        f"📜 Твоя карточка\n\n"
        f"👤 Имя: {name}\n"
        f"📅 Дата рождения: {birth_date}\n"
        f"🕐 Время рождения: {birth_time}\n"
        f"📍 Место рождения: {birth_place}\n"
        f"💎 Кристаллы: {crystals}\n"
        f"💬 Сообщений: {msg_count}\n"
        f"📅 С нами с: {created}\n\n"
        f"─── Расклады ───\n"
        f"🔮 Малый расклад (5 карт) — 1 💎\n"
        f"🃏 Полный расклад (20 карт) — 3 💎\n"
        f"⭐ Гороскоп — 2 💎"
    )
    await update.message.reply_text(profile_text)


# ─────────────────── Команда /balance ───────────────────

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает баланс кристаллов."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    crystals = await db.get_user_crystals(user_id)

    if crystals == 0:
        text = "💎 У тебя сейчас нет кристаллов. Обратись к администратору для пополнения."
    elif crystals == 1:
        text = "💎 У тебя 1 кристалл. Хватит на малый расклад."
    else:
        suffix = "кристалла" if 2 <= crystals <= 4 else "кристаллов"
        text = f"💎 У тебя сейчас {crystals} {suffix}. Достаточно для любого расклада."

    await update.message.reply_text(text)


# ─────────────────── Команда /admin ───────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Панель администратора (только для ADMIN_ID)."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    if user_id != config.ADMIN_ID:
        await update.message.reply_text("Эта команда тебе недоступна, милый человек.")
        return

    text = update.message.text or ""
    parts = text.split()

    if len(parts) >= 4 and parts[1] == "add":
        target_username = parts[2].lstrip("@")
        amount = 0
        try:
            amount = int(parts[3])
        except ValueError:
            await update.message.reply_text("Укажи количество кристаллов числом.")
            return

        stats = await db.get_user_stats()
        target = None
        for s in stats:
            if s.get("username") == target_username:
                target = s
                break

        if not target:
            await update.message.reply_text(f"Пользователь @{target_username} не найден.")
            return

        await db.add_crystals(
            target["user_id"], amount,
            f"Admin gift from {user_id}", txn_type="admin_gift",
        )
        new_balance = await db.get_user_crystals(target["user_id"])
        await update.message.reply_text(
            f"✅ Начислено {amount} 💎 пользователю @{target_username}.\n"
            f"Новый баланс: {new_balance} 💎"
        )
        return

    # Статистика
    stats = await db.get_user_stats()
    if not stats:
        await update.message.reply_text("Пока нет пользователей.")
        return

    lines = ["📊 Статистика бота\n"]
    for s in stats[:20]:
        name = s.get("name") or s.get("first_name") or s.get("username", "—")
        lines.append(
            f"• {name} (@{s.get('username', '—')}): "
            f"{s.get('crystals', 0)} 💎, "
            f"{s.get('message_count', 0)} сообщ., "
            f"сост: {s.get('state', '—')}"
        )

    await update.message.reply_text("\n".join(lines))


# ─────────────────── Основной обработчик текста ───────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик всех текстовых сообщений."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # ─── Rate Limiting ───
    if not await _check_rate_limit(user_id):
        return

    # ─── Получаем пользователя ───
    user = await db.get_or_create_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    state = user.get("state", SofiaState.START)
    is_blocked = user.get("is_blocked", False)

    # ─── Сохраняем сообщение пользователя ───
    await db.save_message(user_id, "user", text_stripped)

    # ─── Проверка на «извини» (снимает блокировку) ───
    if any(trigger in text_lower for trigger in SORRY_TRIGGERS):
        if is_blocked or user.get("rudeness_count", 0) > 0:
            await db.reset_rudeness(user_id)
            await db.update_user_state(user_id, SofiaState.CONVERSATION)
            response = "Ладно, милый человек. Всё забыто. Давай начнём сначала. О чём хочешь поговорить?"
            await update.message.reply_text(response)
            await db.save_message(user_id, "sofia", response, "forgiveness")
            return

    # ─── Блокировка за грубость ───
    if is_blocked:
        await update.message.reply_text(
            "Я пока не готова продолжать разговор. Если хочешь поговорить спокойно — скажи «извини»."
        )
        return

    # ─── Специальные текстовые команды ───
    if any(trigger in text_lower for trigger in BALANCE_TRIGGERS):
        await cmd_balance(update, context)
        return

    if any(trigger in text_lower for trigger in PROFILE_TRIGGERS):
        await cmd_profile(update, context)
        return

    if any(trigger in text_lower for trigger in HISTORY_TRIGGERS):
        await _show_history(update, user_id)
        return

    if any(trigger in text_lower for trigger in MENU_TRIGGERS):
        await _show_menu(update, user)
        return

    # ─── Проверка на грубость ───
    if is_rude(text_stripped):
        new_count = await db.increment_rudeness(user_id)
        idx = min(new_count - 1, len(RUDENESS_RESPONSES) - 1)
        response = RUDENESS_RESPONSES[idx]

        if new_count >= config.MAX_RUDENESS_BEFORE_BLOCK:
            await db.update_user_profile(user_id, is_blocked=True)
            await db.update_user_state(user_id, SofiaState.BLOCKED)

        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "rudeness")
        return

    # ─── Маршрутизация по состоянию FSM ───
    try:
        if state == SofiaState.ASK_NAME:
            await _handle_ask_name(update, user_id, text_stripped, user)
        elif state == SofiaState.ASK_BIRTH_DATE:
            await _handle_ask_birth_date(update, user_id, text_stripped, user)
        elif state == SofiaState.ASK_BIRTH_TIME:
            await _handle_ask_birth_time(update, user_id, text_stripped, user)
        elif state == SofiaState.ASK_BIRTH_PLACE:
            await _handle_ask_birth_place(update, user_id, text_stripped, user)
        elif state == SofiaState.FREE_READING:
            await _handle_free_reading(update, user_id, user)
        elif state == SofiaState.CONVERSATION:
            await _handle_conversation(update, user_id, text_stripped, user)
        elif state == SofiaState.TARO_ASK_NUMBERS:
            await _handle_taro_numbers(update, user_id, text_stripped, user)
        elif state in (SofiaState.TARO_SMALL, SofiaState.TARO_FULL):
            await _handle_paid_reading(update, user_id, text_stripped, user, state)
        elif state == SofiaState.HOROSCOPE:
            await _handle_horoscope_state(update, user_id, text_stripped, user)
        elif state == SofiaState.BLOCKED:
            await update.message.reply_text(
                "Я пока не готова продолжать разговор. Если хочешь поговорить спокойно — скажи «извини»."
            )
        else:
            await db.update_user_state(user_id, SofiaState.CONVERSATION)
            await _handle_conversation(update, user_id, text_stripped, user)
    except Exception as e:
        logger.error(f"Error handling message from {user_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "Что-то туман сегодня густой... Попробуй ещё раз, милый человек."
        )


# ─────────────────── Обработчики состояний FSM ───────────────────

async def _handle_ask_name(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка состояния ASK_NAME."""
    name = text.strip()[:100]
    await db.update_user_profile(user_id, name=name)

    response = (
        f"Красивое имя, {name}. "
        f"Ты пришёл просто из любопытства? Или внутри есть вопрос, "
        f"который давно не даёт тебе покоя?"
    )
    await update.message.reply_text(response)
    await db.update_user_state(user_id, SofiaState.ASK_BIRTH_DATE)
    await db.save_message(user_id, "sofia", response, "name_saved")


async def _handle_ask_birth_date(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка состояния ASK_BIRTH_DATE."""
    text_lower = text.lower()
    curiosity_phrases = ["просто интересно", "любопытство", "любопытно", "просто так", "интересно"]
    if any(p in text_lower for p in curiosity_phrases):
        response = "Любопытство — тоже иногда ведёт человека туда, куда ему нужно попасть. Тогда начнём с малого. Скажи дату рождения."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "curiosity")
        return

    parsed_date = _parse_date(text.strip())
    if parsed_date:
        await db.update_user_profile(user_id, birth_date=parsed_date)
        response = (
            f"{parsed_date.strftime('%d %B %Y')}... Запомнила.\n\n"
            f"А время рождения помнишь? Это поможет точнее увидеть. "
            f"Если не помнишь — скажи «пропустить»."
        )
        await update.message.reply_text(response)
        await db.update_user_state(user_id, SofiaState.ASK_BIRTH_TIME)
        await db.save_message(user_id, "sofia", response, "birth_date_saved")
    else:
        response = "Я не разобрала дату. Напиши в формате день.месяц.год (например, 15.03.1990)"
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_ask_birth_time(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка состояния ASK_BIRTH_TIME."""
    text_lower = text.lower()

    if any(trigger in text_lower for trigger in SKIP_TRIGGERS):
        response = "Ничего, обойдёмся без времени. А место рождения помнишь? Если нет — скажи «пропустить»."
        await update.message.reply_text(response)
        await db.update_user_state(user_id, SofiaState.ASK_BIRTH_PLACE)
        await db.save_message(user_id, "sofia", response, "birth_time_skipped")
        return

    parsed_time = _parse_time(text.strip())
    if parsed_time:
        await db.update_user_profile(user_id, birth_time=parsed_time)
        response = "Запомнила. А место рождения помнишь? Если нет — скажи «пропустить»."
    else:
        response = "Не разобрала время, но ничего страшного. А место рождения помнишь? Если нет — скажи «пропустить»."

    await update.message.reply_text(response)
    await db.update_user_state(user_id, SofiaState.ASK_BIRTH_PLACE)
    await db.save_message(user_id, "sofia", response, "birth_time_saved")


async def _handle_ask_birth_place(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка состояния ASK_BIRTH_PLACE."""
    text_lower = text.lower()

    if any(trigger in text_lower for trigger in SKIP_TRIGGERS):
        response = "Ничего. Я уже вижу достаточно, чтобы заглянуть в твою карту.\n\nДай мне минутку..."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "preparing_reading")
        await db.update_user_state(user_id, SofiaState.FREE_READING)
        await _handle_free_reading(update, user_id, user)
        return

    place = text.strip()[:200]
    await db.update_user_profile(user_id, birth_place=place)

    response = f"{place}... Я чувствую этот край.\n\nДай мне минутку, сейчас загляну в твою карту..."
    await update.message.reply_text(response)
    await db.save_message(user_id, "sofia", response, "preparing_reading")
    await db.update_user_state(user_id, SofiaState.FREE_READING)
    await _handle_free_reading(update, user_id, user)


async def _handle_free_reading(update: Update, user_id: int, user: dict) -> None:
    """Генерация бесплатной Карты судьбы."""
    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    birth_time = user.get("birth_time")
    birth_place = user.get("birth_place")

    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date)
    time_str = birth_time.strftime("%H:%M") if birth_time and hasattr(birth_time, "strftime") else None
    place_str = str(birth_place) if birth_place else None

    fate_card = await generate_fate_card(
        name=name,
        birth_date=date_str,
        birth_time=time_str,
        birth_place=place_str,
    )

    await db.save_message(user_id, "sofia", fate_card, "fate_card")
    await _send_long_message(update, f"📜 Твоя Карта судьбы\n\n{fate_card}")

    await db.update_user_state(user_id, SofiaState.CONVERSATION)

    follow_up = "Ну вот, милый человек. Это твоя карта. Она не приговор — она зеркало.\n\nХочешь поговорить о чём-то, что ты увидел? Или есть вопрос, который тебя тревожит?"
    await update.message.reply_text(follow_up)
    await db.save_message(user_id, "sofia", follow_up, "reading_complete")


async def _handle_conversation(update: Update, user_id: int, text: str, user: dict) -> None:
    """Свободный диалог — основное состояние бота."""
    msg_count = await db.increment_message_count(user_id)

    # Собираем контекст
    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date) if birth_date else ""

    facts = await db.get_memory_facts(user_id, min_importance=3)
    history = await db.get_recent_messages(user_id, limit=8)

    # Генерируем ответ через Gemini (httpx, 8 сек таймаут)
    response = await generate_response(
        user_name=name,
        birth_date=date_str,
        facts=facts,
        history=history,
        user_message=text,
    )

    await db.save_message(user_id, "sofia", response)
    await _send_long_message(update, response)

    # Извлекаем факты каждые 5 сообщений
    if msg_count > 0 and msg_count % 5 == 0:
        await memory.extract_and_save_facts(user_id)

    # ─── Платный хук ───
    if msg_count >= 8 and msg_count % 6 == 0:
        topic = await detect_topic(text)
        if topic not in ("general",):
            topic_names = {
                "relationship": "отношениях и близких людях",
                "work": "деле и достатке",
                "health": "здоровье и самочувствии",
                "purpose": "предназначении и пути",
                "fear": "страхах и тревогах",
            }
            topic_label = topic_names.get(topic, topic)
            hook = (
                f"Знаешь, в твоей карте есть ещё одна интересная сторона. "
                f"Она касается {topic_label}. "
                f"Но карта показывает только часть пути. "
                f"Если хочешь, можно открыть глубже — посмотреть ситуацию через карты."
            )
            await update.message.reply_text(hook)
            await db.save_message(user_id, "sofia", hook, "paid_hook")
            await db.update_user_state(user_id, SofiaState.TARO_ASK_NUMBERS)


async def _handle_taro_numbers(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка выбора расклада и чисел для Таро."""
    text_lower = text.lower()

    reading_type_state = detect_reading_type(text)

    # Объяснение стоимости
    if any(w in text_lower for w in ["как", "давай", "хочу", "открыть", "продолжить", "расклад", "карты"]):
        response = (
            "Чтобы заглянуть глубже, мне нужны карты. Вот что я могу предложить:\n\n"
            "🔮 Малый расклад (5 карт) — 1 💎 кристалл\n"
            "   Откроет текущую ситуацию, скрытое, помехи, помощь и направление.\n\n"
            "🃏 Полный расклад судьбы (20 карт) — 3 💎 кристалла\n"
            "   Глубокий взгляд на все стороны жизни.\n\n"
            "⭐ Персональный гороскоп — 2 💎 кристалла\n"
            "   Энергетика текущего периода.\n\n"
            f"У тебя сейчас {user.get('crystals', 0)} 💎.\n\n"
            "Напиши, какой расклад хочешь: «малый», «полный» или «гороскоп»."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "reading_options")
        return

    # Выбрал тип расклада
    if reading_type_state:
        if reading_type_state == SofiaState.TARO_SMALL:
            cost = config.TARO_SMALL_COST
            card_count = 5
            type_name = "Малый расклад"
            reading_type_str = "small"
        elif reading_type_state == SofiaState.TARO_FULL:
            cost = config.TARO_FULL_COST
            card_count = 20
            type_name = "Полный расклад"
            reading_type_str = "full"
        else:  # HOROSCOPE
            await _execute_horoscope(update, user_id, user)
            return

        # Проверяем кристаллы
        crystals = user.get("crystals", 0)
        if crystals < cost:
            response = (
                f"Мне нужно немного сил. У тебя {crystals} 💎, а для {type_name.lower()} нужно {cost} 💎. "
                f"Обратись к администратору для пополнения."
            )
            await update.message.reply_text(response)
            await db.save_message(user_id, "sofia", response, "insufficient_crystals")
            await db.update_user_state(user_id, SofiaState.CONVERSATION)
            return

        # Сохраняем тип в БД
        await db.update_user_profile(user_id, reading_type=reading_type_str)

        response = (
            f"Хорошо, {type_name.lower()} так {type_name.lower()}.\n\n"
            f"Выбери {card_count} чисел от 1 до 78. Каждое число откроет карту. "
            f"Напиши через пробел или запятую.\n\nНапример: 7 15 23 42 61"
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "ask_numbers")
        await db.update_user_state(user_id, reading_type_state)
        return

    # Пытаемся парсить числа
    numbers = _parse_numbers(text)
    reading_type = user.get("reading_type") or "small"
    cost = config.TARO_SMALL_COST if reading_type == "small" else config.TARO_FULL_COST
    needed = 5 if reading_type == "small" else 20

    if len(numbers) >= needed:
        await _execute_taro_reading(update, user_id, user, numbers[:needed], reading_type == "full", cost)
    else:
        response = f"Мне нужно {needed} чисел от 1 до 78. Ты указал(а) {len(numbers)}. Напиши все числа через пробел или запятую."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_paid_reading(
    update: Update, user_id: int, text: str, user: dict, state: str
) -> None:
    """Обработка состояний TARO_SMALL / TARO_FULL — ожидание чисел."""
    numbers = _parse_numbers(text)

    if state == SofiaState.TARO_SMALL:
        needed = 5
        full = False
        cost = config.TARO_SMALL_COST
    else:
        needed = 20
        full = True
        cost = config.TARO_FULL_COST

    if len(numbers) >= needed:
        await _execute_taro_reading(update, user_id, user, numbers[:needed], full, cost)
    else:
        response = f"Мне нужно {needed} чисел от 1 до 78. Ты указал(а) {len(numbers)}. Напиши все числа."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_horoscope_state(update: Update, user_id: int, text: str, user: dict) -> None:
    """Гороскоп (состояние HOROSCOPE — подтверждение)."""
    await _execute_horoscope(update, user_id, user)


async def _execute_taro_reading(
    update: Update,
    user_id: int,
    user: dict,
    numbers: list[int],
    full: bool,
    cost: int,
) -> None:
    """Исполняет расклад Таро."""
    type_name = "Полный расклад" if full else "Малый расклад"

    success = await db.spend_crystals(user_id, cost, type_name)
    if not success:
        crystals = await db.get_user_crystals(user_id)
        response = f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. Обратись к администратору."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    # Тема из последних сообщений
    recent = await db.get_recent_messages(user_id, limit=4)
    topic_parts = [m["content"][:80] for m in recent if m["role"] == "user"]
    topic = " | ".join(topic_parts[-3:]) if topic_parts else "общий вопрос"

    name = user.get("name") or user.get("first_name") or "милый человек"

    await update.message.reply_text("🗺️ Раскладываю карты... Дай мне минутку.")

    reading = await generate_taro_reading(
        name=name,
        question=topic,
        numbers=numbers,
        full=full,
    )

    type_label = "🃏 Полный расклад судьбы" if full else "🔮 Малый расклад"
    await db.save_message(user_id, "sofia", reading, f"taro_{'full' if full else 'small'}")
    await _send_long_message(update, f"{type_label}\n\n{reading}")
    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await db.update_user_profile(user_id, reading_type=None)


async def _execute_horoscope(update: Update, user_id: int, user: dict) -> None:
    """Исполняет гороскоп."""
    cost = config.HOROSCOPE_COST
    crystals = user.get("crystals", 0)

    if crystals < cost:
        response = f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. Обратись к администратору."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    success = await db.spend_crystals(user_id, cost, "Гороскоп")
    if not success:
        await update.message.reply_text("Не удалось списать кристаллы. Попробуй позже.")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date) if birth_date else ""

    concerns = ""
    recent = await db.get_recent_messages(user_id, limit=4)
    if recent:
        concerns = " | ".join(m["content"][:50] for m in recent if m["role"] == "user")

    horoscope = await generate_horoscope(
        name=name,
        birth_date=date_str,
        birth_time=str(user.get("birth_time")) if user.get("birth_time") else None,
        birth_place=user.get("birth_place"),
        concerns=concerns,
    )

    await db.save_message(user_id, "sofia", horoscope, "horoscope")
    await _send_long_message(update, f"⭐ Твой персональный гороскоп\n\n{horoscope}")
    await db.update_user_state(user_id, SofiaState.CONVERSATION)


# ─────────────────── Вспомогательные функции ───────────────────

async def _show_history(update: Update, user_id: int) -> None:
    """Показывает последние 5 сообщений."""
    messages = await db.get_recent_messages(user_id, limit=5)
    if not messages:
        await update.message.reply_text("У нас пока нет истории диалога.")
        return

    lines = []
    for msg in messages:
        role = "👤 Ты" if msg["role"] == "user" else "👵 София"
        content = msg["content"][:150] + ("..." if len(msg["content"]) > 150 else "")
        lines.append(f"{role}: {content}")

    await update.message.reply_text("📜 Последние сообщения:\n\n" + "\n\n".join(lines))


async def _show_menu(update: Update, user: dict) -> None:
    """Показывает текстовое меню."""
    name = user.get("name") or user.get("first_name") or "милый человек"
    crystals = user.get("crystals", 0)

    menu = (
        f"📋 Вот что можно сделать, {name}:\n\n"
        f"💬 Просто пиши — и мы поговорим\n"
        f"📊 «профиль» — твоя карточка\n"
        f"💎 «баланс» — сколько кристаллов\n"
        f"📜 «история» — последние сообщения\n\n"
        f"─── Расклады ───\n"
        f"🔮 «малый расклад» — 5 карт (1 💎)\n"
        f"🃏 «полный расклад» — 20 карт (3 💎)\n"
        f"⭐ «гороскоп» — персональный (2 💎)\n\n"
        f"У тебя {crystals} 💎"
    )
    await update.message.reply_text(menu)


def _parse_date(text: str):
    """Парсит дату из текста. Возвращает date или None."""
    from datetime import date as date_type

    text = text.strip().replace(",", ".").replace("-", ".").replace("/", ".")
    formats = ["%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d"]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt).date()
            if 1900 <= parsed.year <= 2015:
                return parsed
        except ValueError:
            continue

    # Пробуем найти дату в тексте
    match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
    if match:
        try:
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if year < 100:
                year += 1900 if year >= 40 else 2000
            if 1900 <= year <= 2015 and 1 <= month <= 12 and 1 <= day <= 31:
                return date_type(year, month, day)
        except (ValueError, TypeError):
            pass

    return None


def _parse_time(text: str):
    """Парсит время из текста."""
    from datetime import time as time_type

    text = text.strip()
    for fmt in ["%H:%M", "%H:%M:%S", "%H"]:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.time().replace(second=0, microsecond=0)
        except ValueError:
            continue

    match = re.search(r"(\d{1,2})[:ч]\s*(\d{2})?", text)
    if match:
        try:
            hour = int(match.group(1))
            minute = int(match.group(2)) if match.group(2) else 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time_type(hour, minute)
        except (ValueError, TypeError):
            pass

    return None


def _parse_numbers(text: str) -> list[int]:
    """Извлекает числа из текста (от 1 до 78)."""
    parts = re.split(r"[,\s]+", text.strip())
    numbers = []
    for part in parts:
        try:
            n = int(part.strip())
            if 1 <= n <= 78:
                numbers.append(n)
        except ValueError:
            continue
    return numbers


# ─────────────────── Настройка обработчиков ───────────────────

def setup_handlers(application: Application) -> None:
    """Регистрирует все обработчики. Вызывается из webhook.py."""
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
