"""
Обработчики Telegram для бота София (концепция v2).

Поток:
START → ASK_NAME → ASK_BIRTH_DATE → ASK_BIRTH_TIME → ASK_BIRTH_PLACE
→ PROBING (1 вопрос прощупывания) → FREE_READING (Карта судьбы + крючок)
→ CONVERSATION → (платный хук по концепции) → TARO / HOROSCOPE / SINGLE_CARD
→ CONVERSATION

Особенности v2:
- Прощупывание до Карты судьбы (1 LLM-вопрос)
- Логика долгого отсутствия: «Я вспоминала наш прошлый разговор...»
- Бесплатная 1-карта Таро (cooldown 24ч) — «дать вкус»
- Inline-кнопки для платных офферов
- Переформулированный платный хук по концепции
- Адаптация под пол/возраст (инференс по имени/дате рождения)
- Эмоциональная память в контексте
"""
import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import config
from bot.fsm import (
    SofiaState,
    is_rude,
    detect_reading_type,
    wants_deeper,
    wants_free_card,
    get_next_state,
    is_long_absence,
    infer_gender_from_name,
    age_group_from_birth_date,
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
    generate_single_card,
    generate_probing_question,
    generate_return_greeting,
    detect_topic,
)

logger = logging.getLogger(__name__)

# ─────────────────── Rate Limiting ───────────────────


async def _check_rate_limit(user_id: int) -> bool:
    allowed = await db.check_rate_limit(user_id, config.RATE_LIMIT_SECONDS)
    if allowed:
        await db.update_rate_limit(user_id)
    return allowed


# ─────────────────── Хелперы отправки ───────────────────


async def _send_long_message(update: Update, text: str, max_length: int = 4096,
                              reply_markup: InlineKeyboardMarkup = None) -> None:
    """Отправляет длинное сообщение, разбивая на части."""
    if len(text) <= max_length:
        await update.message.reply_text(text, reply_markup=reply_markup)
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

    # reply_markup только к последней части
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await update.message.reply_text(part, reply_markup=markup)


async def _safe_reply(update: Update, text: str, reply_markup: InlineKeyboardMarkup = None):
    """Безопасная отправка с проверкой на callback vs message."""
    try:
        if update.callback_query:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"_safe_reply error: {e}")


# ─────────────────── Inline-клавиатуры ───────────────────


def _paid_reading_keyboard(crystals: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора расклада."""
    buttons = []
    if crystals >= config.TARO_SMALL_COST:
        buttons.append([InlineKeyboardButton(
            f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST}💎", callback_data="reading:small"
        )])
    if crystals >= config.TARO_FULL_COST:
        buttons.append([InlineKeyboardButton(
            f"🃏 Полный расклад судьбы (20 карт) — {config.TARO_FULL_COST}💎", callback_data="reading:full"
        )])
    if crystals >= config.HOROSCOPE_COST:
        buttons.append([InlineKeyboardButton(
            f"⭐ Персональный гороскоп — {config.HOROSCOPE_COST}💎", callback_data="reading:horoscope"
        )])
    buttons.append([InlineKeyboardButton("🔮 Бесплатная карта", callback_data="reading:free_card")])
    buttons.append([InlineKeyboardButton("Открыть глубже", callback_data="reading:deeper")])
    return InlineKeyboardMarkup(buttons)


def _deeper_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура после хука — «Узнать полностью»."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Узнать полностью", callback_data="reading:deeper")],
        [InlineKeyboardButton("Не сейчас", callback_data="reading:decline")],
    ])


# ─────────────────── Грубость ───────────────────

RUDENESS_RESPONSES = [
    "Слова бывают тяжелее камней. Попробуй сказать то же самое без злобы.\n\nЯ никуда не исчезну.",
    "Я понимаю, что тяжело. Но грубость редко помогает услышать друг друга.",
    "Мне неприятно продолжать разговор в таком тоне.",
    "Похоже, сегодня разговор не складывается.",
    "Я не хочу продолжать разговор, пока ты говоришь со мной таким образом.\n\nЕсли однажды захочешь поговорить спокойно, я буду здесь.",
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
    await db.touch_last_seen(user_id)

    # Если пользователь уже был — перезапускаем с тёплым приветствием
    if user.get("message_count", 0) > 0:
        name = user.get("name") or first_name or "милый человек"
        greeting = (
            f"Здравствуй снова, {name}. "
            f"Соскучилась по тебе. Что привело тебя ко мне сегодня?"
        )
        await update.message.reply_text(greeting)
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await db.save_message(user_id, "sofia", greeting, "greeting")
        return

    # Первый вход — концепция v2: разрушение ожидания бота
    greeting = (
        "🌙\n\nЗдравствуй...\n\n"
        "Я София.\n\n"
        "Не знаю, что именно привело тебя сюда сегодня, но случайных встреч бывает меньше, чем нам кажется.\n\n"
        "Как мне к тебе обращаться?"
    )
    await update.message.reply_text(greeting)
    await db.update_user_state(user_id, SofiaState.ASK_NAME)
    await db.save_message(user_id, "sofia", greeting, "greeting")
    logger.info(f"User {user_id} started the bot")


# ─────────────────── Команда /profile ───────────────────

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST} 💎\n"
        f"🃏 Полный расклад (20 карт) — {config.TARO_FULL_COST} 💎\n"
        f"⭐ Гороскоп — {config.HOROSCOPE_COST} 💎\n"
        f"🗺️ Бесплатная карта — 0 💎 (раз в {config.FREE_CARD_COOLDOWN_HOURS}ч)"
    )
    await update.message.reply_text(profile_text)


# ─────────────────── Команда /balance ───────────────────

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text("Подожди чуток, милый человек... Я не успеваю.")
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

    # ─── Специальные текстовые команды (только в свободном диалоге/после онбординга) ───
    if state in (SofiaState.CONVERSATION, SofiaState.PAID_HOOK, SofiaState.TARO_ASK_NUMBERS):
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

    # ─── Логика долгого отсутствия (только в CONVERSATION) ───
    last_seen = user.get("last_seen_at")
    if state == SofiaState.CONVERSATION and is_long_absence(last_seen, config.RETURN_ABSENCE_HOURS):
        await _handle_return(update, user_id, user, text_stripped)
        return

    # ─── Бесплатная 1-карта (в диалоге) ───
    if state == SofiaState.CONVERSATION and wants_free_card(text_lower):
        await _handle_free_card(update, user_id, user)
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
        elif state == SofiaState.PROBING:
            await _handle_probing(update, user_id, text_stripped, user)
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
        elif state == SofiaState.PAID_HOOK:
            await _handle_paid_hook_response(update, user_id, text_stripped, user)
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
    name = text.strip()[:100]
    await db.update_user_profile(user_id, name=name)

    # Инференс пола
    gender = infer_gender_from_name(name)
    if gender != "unknown":
        await db.update_user_profile(user_id, gender=gender)

    response = (
        f"Красивое имя, {name}. "
        f"Ты пришёл просто из любопытства? Или внутри есть вопрос, "
        f"который давно не даёт тебе покоя?"
    )
    await update.message.reply_text(response)
    await db.update_user_state(user_id, SofiaState.ASK_BIRTH_DATE)
    await db.save_message(user_id, "sofia", response, "name_saved")


async def _handle_ask_birth_date(update: Update, user_id: int, text: str, user: dict) -> None:
    text_lower = text.lower()
    curiosity_phrases = ["просто интересно", "любопытство", "любопытно", "просто так", "интересно"]
    if any(p in text_lower for p in curiosity_phrases):
        response = (
            "Любопытство — тоже иногда ведёт человека туда, куда ему нужно попасть. "
            "Тогда начнём с малого. Скажи дату рождения."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "curiosity")
        return

    parsed_date = _parse_date(text.strip())
    if parsed_date:
        await db.update_user_profile(user_id, birth_date=parsed_date)
        age_group = age_group_from_birth_date(parsed_date)
        if age_group != "unknown":
            await db.update_user_profile(user_id, age_group=age_group)
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
    text_lower = text.lower()

    if any(trigger in text_lower for trigger in SKIP_TRIGGERS):
        response = (
            "Ничего, обойдёмся без времени. А место рождения помнишь? "
            "Если нет — скажи «пропустить»."
        )
        await update.message.reply_text(response)
        await db.update_user_state(user_id, SofiaState.ASK_BIRTH_PLACE)
        await db.save_message(user_id, "sofia", response, "birth_time_skipped")
        return

    parsed_time = _parse_time(text.strip())
    if parsed_time:
        await db.update_user_profile(user_id, birth_time=parsed_time)
        response = "Запомнила. А место рождения помнишь? Если нет — скажи «пропустить»."
    else:
        response = (
            "Не разобрала время, но ничего страшного. "
            "А место рождения помнишь? Если нет — скажи «пропустить»."
        )

    await update.message.reply_text(response)
    await db.update_user_state(user_id, SofiaState.ASK_BIRTH_PLACE)
    await db.save_message(user_id, "sofia", response, "birth_time_saved")


async def _handle_ask_birth_place(update: Update, user_id: int, text: str, user: dict) -> None:
    """После места рождения — переход в PROBING (вместо сразу FREE_READING)."""
    text_lower = text.lower()

    if any(trigger in text_lower for trigger in SKIP_TRIGGERS):
        response = (
            "Ничего. Я уже вижу достаточно, чтобы заглянуть в твою карту.\n\n"
            "Но сначала... один вопрос."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "preparing_probing")
        await db.update_user_state(user_id, SofiaState.PROBING)
        # Генерируем вопрос прощупывания
        await _send_probing_question(update, user_id, user)
        return

    place = text.strip()[:200]
    await db.update_user_profile(user_id, birth_place=place)

    response = (
        f"{place}... Я чувствую этот край.\n\n"
        f"Но прежде чем открыть тебе Карту судьбы — один вопрос."
    )
    await update.message.reply_text(response)
    await db.save_message(user_id, "sofia", response, "preparing_probing")
    await db.update_user_state(user_id, SofiaState.PROBING)
    # Генерируем вопрос прощупывания
    await _send_probing_question(update, user_id, user)


async def _send_probing_question(update: Update, user_id: int, user: dict) -> None:
    """Генерирует и отправляет один вопрос прощупывания."""
    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date) if birth_date else ""

    try:
        question = await generate_probing_question(name, date_str)
        await update.message.reply_text(question)
        await db.save_message(user_id, "sofia", question, "probing_question")
        await db.increment_probing(user_id)
    except Exception as e:
        logger.error(f"Probing question error: {e}")
        # Fallback — статический вопрос
        fallback = "Скажи... был ли в твоей жизни период, когда тебе пришлось резко повзрослеть?"
        await update.message.reply_text(fallback)
        await db.save_message(user_id, "sofia", fallback, "probing_question")
        await db.increment_probing(user_id)


async def _handle_probing(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка ответа на вопрос прощупывания → переход к Карте судьбы."""
    probing_count = user.get("probing_count", 0) or 0

    # После 1 раунда прощупывания — открываем Карту судьбы
    if probing_count >= config.PROBING_ROUNDS:
        await _handle_free_reading(update, user_id, user, probing_answer=text)
        return

    # Если ещё нужны раунды — задаём следующий вопрос (редкий кейс)
    await _send_probing_question(update, user_id, user)


async def _handle_free_reading(update: Update, user_id: int, user: dict,
                                probing_answer: str = "") -> None:
    """Генерация бесплатной Карты судьбы (с крючком-вопросом в конце)."""
    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    birth_time = user.get("birth_time")
    birth_place = user.get("birth_place")

    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date) if birth_date else ""
    time_str = birth_time.strftime("%H:%M") if birth_time and hasattr(birth_time, "strftime") else None
    place_str = str(birth_place) if birth_place else None

    await update.message.reply_text("🗺️ Раскладываю карту... Дай мне минутку.")

    fate_card = await generate_fate_card(
        name=name,
        birth_date=date_str,
        birth_time=time_str,
        birth_place=place_str,
        probing_answer=probing_answer,
    )

    await db.save_message(user_id, "sofia", fate_card, "fate_card")
    await _send_long_message(update, f"📜 Твоя Карта судьбы\n\n{fate_card}")

    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await db.mark_onboarding_completed(user_id)

    follow_up = (
        "Ну вот, милый человек. Это твоя карта. Она не приговор — она зеркало.\n\n"
        "Хочешь поговорить о чём-то, что ты увидел? Или есть вопрос, который тебя тревожит?"
    )
    await update.message.reply_text(follow_up)
    await db.save_message(user_id, "sofia", follow_up, "reading_complete")


async def _handle_return(update: Update, user_id: int, user: dict, user_message: str) -> None:
    """Логика долгого отсутствия — София «вспоминала о тебе»."""
    name = user.get("name") or user.get("first_name") or "милый человек"

    try:
        facts = await db.get_memory_facts(user_id, min_importance=3)
        emotional = await db.get_emotional_memory(user_id, min_importance=3)
        last_topic = user.get("last_topic_summary") or ""

        greeting = await generate_return_greeting(name, facts, emotional, last_topic)
        await update.message.reply_text(greeting)
        await db.save_message(user_id, "sofia", greeting, "return_greeting")

        # Сохраняем сообщение пользователя уже после greeting
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await db.touch_last_seen(user_id)

        # Теперь отвечаем на само сообщение пользователя
        await _handle_conversation(update, user_id, user_message, user)
    except Exception as e:
        logger.error(f"Return greeting error: {e}")
        # Fallback — простой переход в диалог
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await _handle_conversation(update, user_id, user_message, user)


async def _handle_conversation(update: Update, user_id: int, text: str, user: dict) -> None:
    """Свободный диалог — основное состояние бота."""
    msg_count, facts, history, emotional = await asyncio.gather(
        db.increment_message_count(user_id),
        db.get_memory_facts(user_id, min_importance=3),
        db.get_recent_messages(user_id, limit=config.CONTEXT_MESSAGES_LIMIT),
        db.get_emotional_memory(user_id, min_importance=3),
    )

    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else (str(birth_date) if birth_date else "")
    gender = user.get("gender") or ""
    age_group = user.get("age_group") or ""

    response = await generate_response(
        user_name=name,
        birth_date=date_str,
        facts=facts,
        history=history,
        user_message=text,
        emotional=emotional,
        gender=gender,
        age_group=age_group,
    )

    await db.save_message(user_id, "sofia", response)
    await _send_long_message(update, response)

    # Извлекаем факты и эмоциональную память каждые 5 сообщений
    if msg_count > 0 and msg_count % config.MEMORY_EXTRACT_INTERVAL == 0:
        asyncio.create_task(_safe_extract_facts(user_id))

    # ─── Платный хук по концепции ───
    if (msg_count >= config.PAID_HOOK_MIN_MESSAGES
            and (msg_count - config.PAID_HOOK_MIN_MESSAGES) % config.PAID_HOOK_EVERY == 0):
        topic = await detect_topic(text)
        if topic != "general":
            await _send_paid_hook(update, user_id, user, topic)


async def _safe_extract_facts(user_id: int):
    """Безопасный фоновый вызов извлечения фактов."""
    try:
        await memory.extract_and_save_facts(user_id)
    except Exception as e:
        logger.error(f"Background extract_facts error: {e}")


async def _send_paid_hook(update: Update, user_id: int, user: dict, topic: str) -> None:
    """Отправляет переформулированный хук по концепции + inline-кнопка."""
    topic_names = {
        "relationship": "отношениях и том, почему в твою жизнь приходят определённые люди",
        "work": "деле, достатке и том, что тебя там держит",
        "health": "здоровье и том, на что стоит обратить внимание",
        "purpose": "предназначении и том, зачем ты пришёл в этот мир",
        "fear": "страхах и том, что они на самом деле тебе говорят",
    }
    topic_label = topic_names.get(topic, topic)

    hook = (
        f"В твоей карте есть ещё одна интересная сторона.\n\n"
        f"Она касается {topic_label}.\n\n"
        f"Но карта показывает только часть пути. Если хочешь, можно открыть глубже — "
        f"посмотреть ситуацию через карты."
    )
    await update.message.reply_text(hook, reply_markup=_deeper_keyboard())
    await db.save_message(user_id, "sofia", hook, "paid_hook")
    await db.update_user_state(user_id, SofiaState.PAID_HOOK)


async def _handle_paid_hook_response(update: Update, user_id: int, text: str, user: dict) -> None:
    """Реакция на хук: пользователь согласился / отказался / спросил цену."""
    text_lower = text.lower()

    if wants_deeper(text_lower) or any(w in text_lower for w in ["да", "хочу", "давай", "покажи"]):
        # Показываем варианты раскладов с кнопками
        crystals = user.get("crystals", 0)
        response = (
            "Хорошо. Чтобы заглянуть глубже, мне нужны карты. Вот что я могу предложить:\n\n"
            f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST} 💎\n"
            f"   Откроет текущую ситуацию, скрытое, помехи, помощь и направление.\n\n"
            f"🃏 Полный расклад судьбы (20 карт) — {config.TARO_FULL_COST} 💎\n"
            f"   Глубокий взгляд на все стороны жизни.\n\n"
            f"⭐ Персональный гороскоп — {config.HOROSCOPE_COST} 💎\n"
            f"   Энергетика текущего периода.\n\n"
            f"🗺️ Бесплатная карта — 0 💎 (раз в {config.FREE_CARD_COOLDOWN_HOURS}ч)\n"
            f"   Одна карта: «Что сейчас важно понять?»\n\n"
            f"У тебя сейчас {crystals} 💎."
        )
        await update.message.reply_text(response, reply_markup=_paid_reading_keyboard(crystals))
        await db.save_message(user_id, "sofia", response, "reading_options")
        await db.update_user_state(user_id, SofiaState.TARO_ASK_NUMBERS)
        return

    if any(w in text_lower for w in ["нет", "не сейчас", "позже", "не хочу", "отстань"]):
        response = "Хорошо, милый человек. Я никуда не денусь. Поговорим, когда будешь готов."
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "hook_declined")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    # Иначе — обычный ответ в диалог
    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await _handle_conversation(update, user_id, text, user)


async def _handle_taro_numbers(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка выбора расклада и чисел для Таро."""
    text_lower = text.lower()
    reading_type_state = detect_reading_type(text)

    # Объяснение / повторный показ вариантов
    if any(w in text_lower for w in ["как", "что", "вариант", "расклад", "карты", "сколько"]) and not reading_type_state:
        crystals = user.get("crystals", 0)
        response = (
            "Чтобы заглянуть глубже, мне нужны карты. Вот что я могу предложить:\n\n"
            f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST} 💎\n"
            f"🃏 Полный расклад (20 карт) — {config.TARO_FULL_COST} 💎\n"
            f"⭐ Гороскоп — {config.HOROSCOPE_COST} 💎\n"
            f"🗺️ Бесплатная карта — 0 💎\n\n"
            f"У тебя сейчас {crystals} 💎. Выбери кнопкой ниже или напиши: «малый», «полный», «гороскоп»."
        )
        await update.message.reply_text(response, reply_markup=_paid_reading_keyboard(crystals))
        await db.save_message(user_id, "sofia", response, "reading_options")
        return

    # Бесплатная 1-карта по тексту
    if wants_free_card(text_lower):
        await _handle_free_card(update, user_id, user)
        return

    # Выбрал тип расклада
    if reading_type_state:
        if reading_type_state == SofiaState.HOROSCOPE:
            await _execute_horoscope(update, user_id, user)
            return

        if reading_type_state == SofiaState.TARO_SMALL:
            cost = config.TARO_SMALL_COST
            card_count = 5
            type_name = "Малый расклад"
            reading_type_str = "small"
        else:  # TARO_FULL
            cost = config.TARO_FULL_COST
            card_count = 20
            type_name = "Полный расклад"
            reading_type_str = "full"

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

        await db.update_user_profile(user_id, reading_type=reading_type_str)
        response = (
            f"Хорошо, {type_name.lower()} так {type_name.lower()}.\n\n"
            f"Колода сегодня будет открываться через твой выбор.\n\n"
            f"Выбери {card_count} чисел от 1 до {config.MAX_TARO_NUMBER}. "
            f"Каждое число откроет карту. Напиши через пробел или запятую.\n\n"
            f"Например: 7 15 23 42 61"
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
        response = (
            f"Мне нужно {needed} чисел от 1 до {config.MAX_TARO_NUMBER}. "
            f"Ты указал(а) {len(numbers)}. Напиши все числа через пробел или запятую."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_paid_reading(update: Update, user_id: int, text: str, user: dict, state: str) -> None:
    """Состояния TARO_SMALL / TARO_FULL — ожидание чисел."""
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
        response = (
            f"Мне нужно {needed} чисел от 1 до {config.MAX_TARO_NUMBER}. "
            f"Ты указал(а) {len(numbers)}. Напиши все числа."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_horoscope_state(update: Update, user_id: int, text: str, user: dict) -> None:
    await _execute_horoscope(update, user_id, user)


async def _handle_free_card(update: Update, user_id: int, user: dict) -> None:
    """Бесплатная 1-карта Таро — «Что сейчас важно понять?»."""
    can = await db.can_get_free_card(user_id, config.FREE_CARD_COOLDOWN_HOURS)
    if not can:
        response = (
            f"Одну карту я уже для тебя сегодня открывала. "
            f"Следующая будет доступна через {config.FREE_CARD_COOLDOWN_HOURS} часов. "
            f"А пока — давай просто поговорим."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "free_card_cooldown")
        return

    name = user.get("name") or user.get("first_name") or "милый человек"

    # Контекст последних разговоров
    recent = await db.get_recent_messages(user_id, limit=4)
    context = " | ".join(m["content"][:80] for m in recent if m["role"] == "user") if recent else ""

    await update.message.reply_text("🗺️ Тяну для тебя карту...")

    card_text = await generate_single_card(name=name, question_context=context)

    await db.mark_free_card_used(user_id)
    await db.save_message(user_id, "sofia", card_text, "free_card")
    await _send_long_message(update, card_text)


async def _execute_taro_reading(update: Update, user_id: int, user: dict,
                                 numbers: list[int], full: bool, cost: int) -> None:
    """Исполняет расклад Таро."""
    type_name = "Полный расклад" if full else "Малый расклад"

    success = await db.spend_crystals(user_id, cost, type_name)
    if not success:
        crystals = await db.get_user_crystals(user_id)
        response = (
            f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. "
            f"Обратись к администратору."
        )
        await update.message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

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
        response = (
            f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. "
            f"Обратись к администратору."
        )
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
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else (str(birth_date) if birth_date else "")

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


# ─────────────────── CallbackQueryHandler (inline-кнопки) ───────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий inline-кнопок."""
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    user = await db.get_or_create_user(user_id, query.from_user.username, query.from_user.first_name)
    if not user:
        return

    if data == "reading:small":
        await _start_reading_flow(update, user_id, user, SofiaState.TARO_SMALL, "small", 5, config.TARO_SMALL_COST, "Малый расклад")
    elif data == "reading:full":
        await _start_reading_flow(update, user_id, user, SofiaState.TARO_FULL, "full", 20, config.TARO_FULL_COST, "Полный расклад")
    elif data == "reading:horoscope":
        # Для гороскопа числа не нужны
        await _execute_horoscope(update, user_id, user)
    elif data == "reading:free_card":
        await _handle_free_card(update, user_id, user)
    elif data == "reading:deeper":
        crystals = user.get("crystals", 0)
        response = (
            "Хорошо. Чтобы заглянуть глубже, мне нужны карты. Выбери, что тебе ближе:"
        )
        await _safe_reply(update, response, reply_markup=_paid_reading_keyboard(crystals))
        await db.save_message(user_id, "sofia", response, "reading_options")
        await db.update_user_state(user_id, SofiaState.TARO_ASK_NUMBERS)
    elif data == "reading:decline":
        response = "Хорошо, милый человек. Я никуда не денусь. Поговорим, когда будешь готов."
        await _safe_reply(update, response)
        await db.save_message(user_id, "sofia", response, "hook_declined")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)


async def _start_reading_flow(update: Update, user_id: int, user: dict,
                               state: SofiaState, reading_type_str: str,
                               card_count: int, cost: int, type_name: str) -> None:
    """Начинает поток расклада: проверка кристаллов + запрос чисел."""
    crystals = user.get("crystals", 0)
    if crystals < cost:
        response = (
            f"Мне нужно немного сил. У тебя {crystals} 💎, а для {type_name.lower()} нужно {cost} 💎. "
            f"Обратись к администратору для пополнения."
        )
        await _safe_reply(update, response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    await db.update_user_profile(user_id, reading_type=reading_type_str)
    response = (
        f"Хорошо, {type_name.lower()} так {type_name.lower()}.\n\n"
        f"Колода сегодня будет открываться через твой выбор.\n\n"
        f"Выбери {card_count} чисел от 1 до {config.MAX_TARO_NUMBER}. "
        f"Каждое число откроет карту. Напиши через пробел или запятую.\n\n"
        f"Например: 7 15 23 42 61"
    )
    await _safe_reply(update, response)
    await db.save_message(user_id, "sofia", response, "ask_numbers")
    await db.update_user_state(user_id, state)


# ─────────────────── Вспомогательные функции ───────────────────

async def _show_history(update: Update, user_id: int) -> None:
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
    name = user.get("name") or user.get("first_name") or "милый человек"
    crystals = user.get("crystals", 0)

    menu = (
        f"📋 Вот что можно сделать, {name}:\n\n"
        f"💬 Просто пиши — и мы поговорим\n"
        f"📊 «профиль» — твоя карточка\n"
        f"💎 «баланс» — сколько кристаллов\n"
        f"📜 «история» — последние сообщения\n\n"
        f"─── Расклады ───\n"
        f"🔮 «малый расклад» — 5 карт ({config.TARO_SMALL_COST} 💎)\n"
        f"🃏 «полный расклад» — 20 карт ({config.TARO_FULL_COST} 💎)\n"
        f"⭐ «гороскоп» — персональный ({config.HOROSCOPE_COST} 💎)\n"
        f"🗺️ «бесплатная карта» — 1 карта (0 💎, раз в {config.FREE_CARD_COOLDOWN_HOURS}ч)\n\n"
        f"У тебя {crystals} 💎"
    )
    # Кнопки для быстрого доступа
    await update.message.reply_text(menu, reply_markup=_paid_reading_keyboard(crystals))


def _parse_date(text: str):
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
    parts = re.split(r"[,\s]+", text.strip())
    numbers = []
    for part in parts:
        try:
            n = int(part.strip())
            if 1 <= n <= config.MAX_TARO_NUMBER:
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
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
