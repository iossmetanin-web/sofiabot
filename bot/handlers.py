"""
Обработчики Telegram для бота София (концепция v2, round 4).

Поток:
START → ASK_NAME → ASK_BIRTH_DATE → ASK_BIRTH_TIME → ASK_BIRTH_PLACE
→ PROBING (1 вопрос прощупывания) → FREE_READING (Карта судьбы + крючок)
→ CONVERSATION → (платный хук по концепции) → TARO / HOROSCOPE / SINGLE_CARD
→ CONVERSATION

Round 4:
- Зодиак в онбординге и раскладах
- Реальные имена карт Таро
- /reset — сброс застрявшего FSM
- Админ-панель с inline-кнопками
- Cron checkin для неактивных пользователей
- Сводка диалога при длинной истории
- Исправлен баг: rate limit сохранял сообщение до проверки
- Исправлен баг: бесплатная карта не сбрасывала состояние
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
    wants_card_of_day,
    get_next_state,
    is_long_absence,
    infer_gender_from_name,
    age_group_from_birth_date,
    get_zodiac_sign,
    get_tarot_card_name,
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
    generate_thematic_reading,
    generate_horoscope,
    generate_single_card,
    generate_card_of_day,
    generate_birthday_greeting,
    generate_probing_question,
    generate_return_greeting,
    generate_daily_horoscope,
    generate_mood_checkin,
    generate_conversation_summary,
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
        await update.effective_message.reply_text(text, reply_markup=reply_markup)
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
        await update.effective_message.reply_text(part, reply_markup=markup)


async def _safe_reply(update: Update, text: str, reply_markup: InlineKeyboardMarkup = None):
    """Безопасная отправка с проверкой на callback vs message."""
    try:
        if update.callback_query:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
        else:
            await update.effective_message.reply_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"_safe_reply error: {e}")


# ─────────────────── Inline-клавиатуры ───────────────────


def _paid_reading_keyboard(crystals: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора расклада."""
    buttons = []
    # Тематические расклады (Round 5) — компактнее, дешевле
    thematic_row = []
    if crystals >= config.TARO_LOVE_COST:
        thematic_row.append(InlineKeyboardButton(
            f"❤️ Любовь {config.TARO_LOVE_COST}💎", callback_data="reading:love"
        ))
    if crystals >= config.TARO_DECISION_COST:
        thematic_row.append(InlineKeyboardButton(
            f"⚖️ Выбор {config.TARO_DECISION_COST}💎", callback_data="reading:decision"
        ))
    if thematic_row:
        buttons.append(thematic_row)
    if crystals >= config.TARO_CAREER_COST:
        buttons.append([InlineKeyboardButton(
            f"💼 Дело {config.TARO_CAREER_COST}💎", callback_data="reading:career"
        )])
    # Классические расклады
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
    buttons.append([InlineKeyboardButton("🃏 Карта дня", callback_data="reading:card_of_day")])
    buttons.append([InlineKeyboardButton("Открыть глубже", callback_data="reading:deeper")])
    return InlineKeyboardMarkup(buttons)


def _deeper_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура после хука — «Узнать полностью»."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Узнать полностью", callback_data="reading:deeper")],
        [InlineKeyboardButton("Не сейчас", callback_data="reading:decline")],
    ])


def _admin_keyboard() -> InlineKeyboardMarkup:
    """Админ-панель с inline-кнопками."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin:broadcast_prompt")],
    ])


# ─────────────────── Грубость ───────────────────

RUDENESS_RESPONSES = [
    "Слова бывают тяжелее камней. Попробуй сказать то же самое без злобы.\n\nЯ никуда не исчезну.",
    "Я понимаю, что тяжело. Но грубость редко помогает услышать друг друга.",
    "Мне неприятно продолжать разговор в таком тоне.",
    "Похоже, сегодня разговор не складывается.",
    "Я не хочу продолжать разговор, пока ты говоришь со мной таким образом.\n\nЕсли однажды захочешь поговорить спокойно, я буду здесь.",
]


# ─────────────────── Хелпер: получить зодиак ───────────────────

def _get_zodiac_from_user(user: dict) -> str:
    """Получает знак зодиака из данных пользователя."""
    birth_date = user.get("birth_date")
    if birth_date and hasattr(birth_date, "month"):
        name, symbol = get_zodiac_sign(birth_date)
        return f"{symbol} {name}" if name else ""
    return ""


# ─────────────────── Команда /start (с реферальной ссылкой) ───────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start. Поддержка рефералов: /start ref_<UID>."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name

    user = await db.get_or_create_user(user_id, username, first_name)
    await db.touch_last_seen(user_id)

    # ─── Реферальная обработка ───
    args = context.args if context.args else []
    if args and args[0].startswith("ref_"):
        try:
            ref_uid = int(args[0][4:])
            if user.get("message_count", 0) == 0 and ref_uid != user_id:
                ref_user = await db.get_user(ref_uid)
                if ref_user and not await db.was_referred(user_id):
                    await db.set_referred_by(user_id, ref_uid)
                    await db.add_crystals(
                        ref_uid, 1,
                        f"Реферальный бонус: пригласил {user_id}",
                        txn_type="add",
                    )
                    await db.mark_referral_reward_given(user_id)
                    logger.info(f"Referral: {user_id} referred by {ref_uid}, +1 💎 to {ref_uid}")
        except (ValueError, TypeError):
            pass

    # Если пользователь уже был — перезапускаем с тёплым приветствием
    if user.get("message_count", 0) > 0:
        name = user.get("name") or first_name or "милый человек"
        last_seen = user.get("last_seen_at")

        if is_long_absence(last_seen, config.RETURN_ABSENCE_HOURS):
            try:
                facts = await db.get_memory_facts(user_id, min_importance=3)
                emotional = await db.get_emotional_memory(user_id, min_importance=3)
                last_topic = user.get("last_topic_summary") or ""
                zodiac = _get_zodiac_from_user(user)
                greeting = await generate_return_greeting(name, facts, emotional, last_topic, zodiac)
                await update.effective_message.reply_text(greeting)
                await db.save_message(user_id, "sofia", greeting, "return_greeting")
            except Exception as e:
                logger.error(f"Return greeting on /start error: {e}")
                greeting = f"Здравствуй снова, {name}. Соскучилась по тебе. Что привело тебя ко мне сегодня?"
                await update.effective_message.reply_text(greeting)
                await db.save_message(user_id, "sofia", greeting, "greeting")
        else:
            greeting = (
                f"Здравствуй снова, {name}. "
                f"Соскучилась по тебе. Что привело тебя ко мне сегодня?"
            )
            await update.effective_message.reply_text(greeting)
            await db.save_message(user_id, "sofia", greeting, "greeting")

        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await db.touch_last_seen(user_id)
        return

    # Первый вход — концепция v2: разрушение ожидания бота
    greeting = (
        "🌙\n\nЗдравствуй...\n\n"
        "Я София.\n\n"
        "Не знаю, что именно привело тебя сюда сегодня, но случайных встреч бывает меньше, чем нам кажется.\n\n"
        "Как мне к тебе обращаться?"
    )
    await update.effective_message.reply_text(greeting)
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
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
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

    # Зодиак
    zodiac = _get_zodiac_from_user(user)
    zodiac_line = f"\n♊ Знак зодиака: {zodiac}" if zodiac else ""

    # Возраст
    age_group = user.get("age_group", "")
    age_map = {"young": "до 18", "young_adult": "18-30", "adult": "30-45",
               "mature": "45-60", "senior": "60+"}
    age_line = f"\n🎂 Возраст: {age_map.get(age_group, 'неизвестно')}" if age_group else ""

    # Рефералы
    ref_count = await db.get_referral_count(user_id)

    profile_text = (
        f"📜 Твоя карточка\n\n"
        f"👤 Имя: {name}{zodiac_line}{age_line}\n"
        f"📅 Дата рождения: {birth_date}\n"
        f"🕐 Время рождения: {birth_time}\n"
        f"📍 Место рождения: {birth_place}\n"
        f"💎 Кристаллы: {crystals}\n"
        f"💬 Сообщений: {msg_count}\n"
        f"🔗 Приглашено: {ref_count} чел.\n"
        f"📅 С нами с: {created}\n\n"
        f"─── Расклады ───\n"
        f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST} 💎\n"
        f"🃏 Полный расклад (20 карт) — {config.TARO_FULL_COST} 💎\n"
        f"⭐ Гороскоп — {config.HOROSCOPE_COST} 💎\n"
        f"🗺️ Бесплатная карта — 0 💎 (раз в {config.FREE_CARD_COOLDOWN_HOURS}ч)"
    )
    await update.effective_message.reply_text(profile_text)


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

    await update.effective_message.reply_text(text)


# ─────────────────── Команда /admin ───────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    if user_id != config.ADMIN_ID:
        await update.effective_message.reply_text("Эта команда тебе недоступна, милый человек.")
        return

    text = update.message.text or ""
    parts = text.split()

    if len(parts) >= 4 and parts[1] == "add":
        target_username = parts[2].lstrip("@")
        amount = 0
        try:
            amount = int(parts[3])
        except ValueError:
            await update.effective_message.reply_text("Укажи количество кристаллов числом.")
            return

        stats = await db.get_user_stats()
        target = None
        for s in stats:
            if s.get("username") == target_username:
                target = s
                break

        if not target:
            await update.effective_message.reply_text(f"Пользователь @{target_username} не найден.")
            return

        await db.add_crystals(
            target["user_id"], amount,
            f"Admin gift from {user_id}", txn_type="admin_gift",
        )
        new_balance = await db.get_user_crystals(target["user_id"])
        await update.effective_message.reply_text(
            f"✅ Начислено {amount} 💎 пользователю @{target_username}.\n"
            f"Новый баланс: {new_balance} 💎"
        )
        return

    # Показываем админ-панель с inline-кнопками
    await update.effective_message.reply_text(
        "👑 Панель администратора Софии",
        reply_markup=_admin_keyboard()
    )


# ─────────────────── Команда /stats (расширенная аналитика) ───────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Расширенная аналитика для админа."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    if user_id != config.ADMIN_ID:
        await update.effective_message.reply_text("Эта команда тебе недоступна, милый человек.")
        return

    try:
        s = await db.get_admin_analytics()
        lines = [
            "📊 Аналитика Софии\n",
            f"👥 Всего пользователей: {s['total_users']}",
            f"✅ Завершили онбординг: {s['onboarding_done']}",
            f"🟢 Активны за 24ч: {s['active_24h']}",
            f"📅 Активны за 7 дней: {s['active_7d']}",
            f"💬 Всего сообщений: {s['total_messages']}",
            f"💎 Кристаллов в обороте: {s['total_crystals']}",
            f"💰 Платящих пользователей: {s['paying_users']} ({s['conversion_pct']}%)",
            f"🛒 Платных транзакций: {s['paid_transactions']}",
            f"🔗 Пришли по рефералу: {s['referral_users']}",
            f"🌅 Подписаны на daily: {s['daily_subscribers']}",
            f"🚫 Заблокировано: {s['blocked_users']}",
            "",
            "🏆 Топ-5 активных:",
        ]
        for i, u in enumerate(s["top_active"], 1):
            name = u.get("name") or u.get("first_name") or u.get("username", "—")
            lines.append(f"  {i}. {name}: {u['message_count']} сообщ., {u['crystals']} 💎")
        await update.effective_message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"cmd_stats error: {e}", exc_info=True)
        await update.effective_message.reply_text(f"Не удалось собрать статистику: {e}")


# ─────────────────── Команда /today (ежедневное послание) ───────────────────

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бесплатное ежедневное послание от Софии (раз в день)."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    if not user.get("onboarding_completed"):
        await update.effective_message.reply_text(
            "Сначала давай познакомимся. Напиши /start и пройди короткий путь."
        )
        return

    if not await db.can_get_daily_horoscope(user_id):
        await update.effective_message.reply_text(
            "🌅 Я уже посылала тебе весточку сегодня. Завтра поговорим снова. "
            "А пока — если есть, что на сердце, я рядом."
        )
        return

    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date) if birth_date else ""
    zodiac = _get_zodiac_from_user(user)

    await update.effective_message.reply_text("🌅 Дай минутку, всматриваюсь в твой день...")

    try:
        emotional = await db.get_emotional_memory(user_id, min_importance=3)
        daily_msg = await generate_daily_horoscope(name=name, birth_date=date_str, emotional=emotional, zodiac=zodiac)
        await db.mark_daily_horoscope_used(user_id)
        await db.save_message(user_id, "sofia", daily_msg, "daily_horoscope")
        await update.effective_message.reply_text(daily_msg)
    except Exception as e:
        logger.error(f"cmd_today error: {e}", exc_info=True)
        await update.effective_message.reply_text(
            "Туман сегодня густой... Не вижу знаков дня. Попробуй ещё раз чуть позже."
        )


# ─────────────────── Команда /invite (реферальная ссылка) ───────────────────

async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает реферальную ссылку пользователя."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    bot_username = "oracultetris_bot"
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    ref_count = await db.get_referral_count(user_id)

    text = (
        f"🔗 Твоя ссылка, чтобы пригласить близких ко мне:\n\n"
        f"{ref_link}\n\n"
        f"Когда человек по ней придёт, ты получишь 1 💎 кристалл в знак благодарности.\n\n"
        f"Ты уже пригласил(а): {ref_count} человек(а)."
    )
    await update.effective_message.reply_text(text)


# ─────────────────── Команды /subscribe /unsubscribe ──────

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подписка на ежедневные послания."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    if user.get("daily_horoscope_opt_in"):
        await update.effective_message.reply_text(
            "Ты уже подписан(а) на мои утренние весточки. "
            "Напиши /today, чтобы получить сегодняшнюю, или просто жди — я напомню о себе."
        )
        return

    await db.set_daily_horoscope_opt_in(user_id, True)
    await update.effective_message.reply_text(
        "✅ Я буду думать о тебе по утрам. Напиши /today, когда захочешь получить послание дня. "
        "Отписаться можно в любой момент: /unsubscribe"
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отписка от ежедневных посланий."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    await db.set_daily_horoscope_opt_in(user_id, False)
    await update.effective_message.reply_text(
        "Хорошо, милый человек. Больше не буду тебя беспокоить по утрам. "
        "Но если захочешь поговорить — я всегда здесь."
    )


# ─────────────────── Команда /delete_my_data (GDPR) ───────────────────

async def cmd_delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрос на удаление всех данных пользователя (2-шаговое подтверждение)."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id

    await db.update_user_state(user_id, SofiaState.AWAITING_DELETE_CONFIRM)
    await update.effective_message.reply_text(
        "⚠️ Ты просишь стереть все наши разговоры, твою карту судьбы, всё, что я о тебе помнила.\n\n"
        "Это нельзя будет вернуть. Я забуду тебя совсем.\n\n"
        "Если ты уверен(а) — напиши: «удалить навсегда»\n"
        "Если передумал(а) — напиши что угодно другое."
    )


# ─────────────────── Команда /help ───────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Краткая справка."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    crystals = user.get("crystals", 0) if user else 0

    text = (
        "🌙 Я — София. Вот что я умею:\n\n"
        "💬 Просто пиши — и мы поговорим\n"
        "🌅 /today — послание дня (бесплатно, раз в день)\n"
        "🃏 /card_of_day — карта дня (раз в 20 часов)\n"
        "💚 /mood — я спрошу, как ты (с заботой)\n"
        "📜 /profile — твоя карточка\n"
        "💎 /balance — сколько кристаллов\n"
        "📖 «история» — последние сообщения\n"
        "🔗 /invite — пригласить близкого (+1 💎 за каждого)\n"
        "🌅 /subscribe — утренние весточки от меня\n"
        "📥 /export_my_history — наша история диалога\n"
        "🔄 /reset — начать разговор заново (если застрял)\n"
        "🚪 /delete_my_data — стереть все наши разговоры\n\n"
        "─── Расклады ───\n"
        f"❤️ «расклад на любовь» — 3 карты ({config.TARO_LOVE_COST} 💎)\n"
        f"⚖️ «расклад на выбор» — 3 карты ({config.TARO_DECISION_COST} 💎)\n"
        f"💼 «расклад на дело» — 5 карт ({config.TARO_CAREER_COST} 💎)\n"
        f"🔮 «малый расклад» — 5 карт ({config.TARO_SMALL_COST} 💎)\n"
        f"🃏 «полный расклад» — 20 карт ({config.TARO_FULL_COST} 💎)\n"
        f"⭐ «гороскоп» — персональный ({config.HOROSCOPE_COST} 💎)\n"
        f"🗺️ «бесплатная карта» — 1 карта (0 💎)\n\n"
        f"У тебя {crystals} 💎"
    )
    await update.effective_message.reply_text(text, reply_markup=_paid_reading_keyboard(crystals))


# ─────────────────── Команда /reset (сброс застрявшего FSM) ───────────────────

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сброс состояния в CONVERSATION — для случаев, когда FSM застрял."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    old_state = user.get("state", "CONVERSATION")
    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await db.update_user_profile(user_id, reading_type=None)

    if old_state != SofiaState.CONVERSATION:
        await update.effective_message.reply_text(
            "🔄 Хорошо, милый человек. Я сбросила всё, что застряло. "
            "Давай начнём разговор заново. О чём хочешь поговорить?"
        )
    else:
        await update.effective_message.reply_text(
            "У нас всё в порядке, милый человек. Просто пиши — и мы поговорим."
        )


# ─────────────────── Основной обработчик текста ───────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик всех текстовых сообщений."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # ─── Rate Limiting (ДО сохранения сообщения!) ───
    if not await _check_rate_limit(user_id):
        await update.effective_message.reply_text("Подожди чуток, милый человек... Я не успеваю.")
        return

    # ─── Получаем пользователя ───
    user = await db.get_or_create_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    state = user.get("state", SofiaState.START)
    is_blocked = user.get("is_blocked", False)

    # ─── Сохраняем сообщение пользователя (только после rate limit) ───
    await db.save_message(user_id, "user", text_stripped)

    # ─── Проверка на «извини» (снимает блокировку) ───
    if any(trigger in text_lower for trigger in SORRY_TRIGGERS):
        if is_blocked or user.get("rudeness_count", 0) > 0:
            await db.reset_rudeness(user_id)
            await db.update_user_state(user_id, SofiaState.CONVERSATION)
            response = "Ладно, милый человек. Всё забыто. Давай начнём сначала. О чём хочешь поговорить?"
            await update.effective_message.reply_text(response)
            await db.save_message(user_id, "sofia", response, "forgiveness")
            return

    # ─── Блокировка за грубость ───
    if is_blocked:
        await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(response)
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

    # ─── Карта дня (Round 5, в диалоге) ───
    if state == SofiaState.CONVERSATION and wants_card_of_day(text_lower):
        await _handle_card_of_day(update, user_id, user)
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
        elif state in (SofiaState.TARO_SMALL, SofiaState.TARO_FULL,
                        SofiaState.TARO_LOVE, SofiaState.TARO_CAREER, SofiaState.TARO_DECISION):
            await _handle_paid_reading(update, user_id, text_stripped, user, state)
        elif state == SofiaState.HOROSCOPE:
            await _handle_horoscope_state(update, user_id, text_stripped, user)
        elif state == SofiaState.PAID_HOOK:
            await _handle_paid_hook_response(update, user_id, text_stripped, user)
        elif state == SofiaState.BROADCAST:
            await _handle_broadcast(update, user_id, text_stripped, user)
        elif state == SofiaState.BLOCKED:
            await update.effective_message.reply_text(
                "Я пока не готова продолжать разговор. Если хочешь поговорить спокойно — скажи «извини»."
            )
        elif state == SofiaState.AWAITING_DELETE_CONFIRM:
            await _handle_delete_confirm(update, user_id, text_stripped)
        else:
            await db.update_user_state(user_id, SofiaState.CONVERSATION)
            await _handle_conversation(update, user_id, text_stripped, user)
    except Exception as e:
        logger.error(f"Error handling message from {user_id}: {e}", exc_info=True)
        await update.effective_message.reply_text(
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
    await update.effective_message.reply_text(response)
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
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "curiosity")
        return

    parsed_date = _parse_date(text.strip())
    if parsed_date:
        await db.update_user_profile(user_id, birth_date=parsed_date)
        age_group = age_group_from_birth_date(parsed_date)
        if age_group != "unknown":
            await db.update_user_profile(user_id, age_group=age_group)

        # Показываем знак зодиака
        zodiac_name, zodiac_symbol = get_zodiac_sign(parsed_date)
        zodiac_msg = f"\n\n{zodiac_symbol} {zodiac_name}... Я запомнила." if zodiac_name else "... Запомнила."

        response = (
            f"{parsed_date.strftime('%d %B %Y')}{zodiac_msg}\n\n"
            f"А время рождения помнишь? Это поможет точнее увидеть. "
            f"Если не помнишь — скажи «пропустить»."
        )
        await update.effective_message.reply_text(response)
        await db.update_user_state(user_id, SofiaState.ASK_BIRTH_TIME)
        await db.save_message(user_id, "sofia", response, "birth_date_saved")
    else:
        response = "Я не разобрала дату. Напиши в формате день.месяц.год (например, 15.03.1990)"
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_ask_birth_time(update: Update, user_id: int, text: str, user: dict) -> None:
    text_lower = text.lower()

    if any(trigger in text_lower for trigger in SKIP_TRIGGERS):
        response = (
            "Ничего, обойдёмся без времени. А место рождения помнишь? "
            "Если нет — скажи «пропустить»."
        )
        await update.effective_message.reply_text(response)
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

    await update.effective_message.reply_text(response)
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
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "preparing_probing")
        await db.update_user_state(user_id, SofiaState.PROBING)
        await _send_probing_question(update, user_id, user)
        return

    place = text.strip()[:200]
    await db.update_user_profile(user_id, birth_place=place)

    response = (
        f"{place}... Я чувствую этот край.\n\n"
        f"Но прежде чем открыть тебе Карту судьбы — один вопрос."
    )
    await update.effective_message.reply_text(response)
    await db.save_message(user_id, "sofia", response, "preparing_probing")
    await db.update_user_state(user_id, SofiaState.PROBING)
    await _send_probing_question(update, user_id, user)


async def _send_probing_question(update: Update, user_id: int, user: dict) -> None:
    """Генерирует и отправляет один вопрос прощупывания."""
    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else str(birth_date) if birth_date else ""
    zodiac = _get_zodiac_from_user(user)

    try:
        question = await generate_probing_question(name, date_str, zodiac)
        await update.effective_message.reply_text(question)
        await db.save_message(user_id, "sofia", question, "probing_question")
        await db.increment_probing(user_id)
    except Exception as e:
        logger.error(f"Probing question error: {e}")
        fallback = "Скажи... был ли в твоей жизни период, когда тебе пришлось резко повзрослеть?"
        await update.effective_message.reply_text(fallback)
        await db.save_message(user_id, "sofia", fallback, "probing_question")
        await db.increment_probing(user_id)


async def _handle_probing(update: Update, user_id: int, text: str, user: dict) -> None:
    """Обработка ответа на вопрос прощупывания → переход к Карте судьбы."""
    probing_count = user.get("probing_count", 0) or 0

    if probing_count >= config.PROBING_ROUNDS:
        await _handle_free_reading(update, user_id, user, probing_answer=text)
        return

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
    zodiac = _get_zodiac_from_user(user)

    await update.effective_message.reply_text("🗺️ Раскладываю карту... Дай мне минутку.")

    fate_card = await generate_fate_card(
        name=name,
        birth_date=date_str,
        birth_time=time_str,
        birth_place=place_str,
        probing_answer=probing_answer,
        zodiac=zodiac,
    )

    await db.save_message(user_id, "sofia", fate_card, "fate_card")
    await _send_long_message(update, f"📜 Твоя Карта судьбы\n\n{fate_card}")

    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await db.mark_onboarding_completed(user_id)

    follow_up = (
        "Ну вот, милый человек. Это твоя карта. Она не приговор — она зеркало.\n\n"
        "Хочешь поговорить о чём-то, что ты увидел? Или есть вопрос, который тебя тревожит?"
    )
    await update.effective_message.reply_text(follow_up)
    await db.save_message(user_id, "sofia", follow_up, "reading_complete")


async def _handle_return(update: Update, user_id: int, user: dict, user_message: str) -> None:
    """Логика долгого отсутствия — София «вспоминала о тебе»."""
    name = user.get("name") or user.get("first_name") or "милый человек"
    zodiac = _get_zodiac_from_user(user)

    try:
        facts = await db.get_memory_facts(user_id, min_importance=3)
        emotional = await db.get_emotional_memory(user_id, min_importance=3)
        last_topic = user.get("last_topic_summary") or ""

        greeting = await generate_return_greeting(name, facts, emotional, last_topic, zodiac)
        await update.effective_message.reply_text(greeting)
        await db.save_message(user_id, "sofia", greeting, "return_greeting")

        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await db.touch_last_seen(user_id)

        follow_up = "Если хочешь, расскажи — что нового с тех пор?"
        await update.effective_message.reply_text(follow_up)
        await db.save_message(user_id, "sofia", follow_up, "return_follow_up")
    except Exception as e:
        logger.error(f"Return greeting error: {e}")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        greeting = f"Здравствуй, {name}. Я вспоминала о тебе. Как ты?"
        await update.effective_message.reply_text(greeting)
        await db.save_message(user_id, "sofia", greeting, "return_greeting")


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
    zodiac = _get_zodiac_from_user(user)

    # Сводка диалога — если история длинная
    conversation_summary = ""
    if len(history) > 10:
        try:
            # Берём старую часть для сводки
            older = history[:-6]
            conversation_summary = await generate_conversation_summary(older)
        except Exception as e:
            logger.error(f"Summary generation error: {e}")

    response = await generate_response(
        user_name=name,
        birth_date=date_str,
        facts=facts,
        history=history,
        user_message=text,
        emotional=emotional,
        gender=gender,
        age_group=age_group,
        zodiac=zodiac,
        conversation_summary=conversation_summary,
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
    await update.effective_message.reply_text(hook, reply_markup=_deeper_keyboard())
    await db.save_message(user_id, "sofia", hook, "paid_hook")
    await db.update_user_state(user_id, SofiaState.PAID_HOOK)


async def _handle_paid_hook_response(update: Update, user_id: int, text: str, user: dict) -> None:
    """Реакция на хук: пользователь согласился / отказался / спросил цену."""
    text_lower = text.lower()

    if wants_deeper(text_lower) or any(w in text_lower for w in ["да", "хочу", "давай", "покажи"]):
        crystals = user.get("crystals", 0)
        response = (
            "Хорошо. Чтобы заглянуть глубже, мне нужны карты. Вот что я могу предложить:\n\n"
            f"❤️ Расклад на любовь (3 карты) — {config.TARO_LOVE_COST} 💎\n"
            f"   Ты, партнёр, связующая нить.\n\n"
            f"⚖️ Расклад на выбор (3 карты) — {config.TARO_DECISION_COST} 💎\n"
            f"   Два пути и то, что важно понять сердцем.\n\n"
            f"💼 Расклад на дело (5 карт) — {config.TARO_CAREER_COST} 💎\n"
            f"   Где ты сейчас, что питает, что мешает, возможность, совет.\n\n"
            f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST} 💎\n"
            f"   Текущая ситуация, скрытое, помехи, помощь, направление.\n\n"
            f"🃏 Полный расклад судьбы (20 карт) — {config.TARO_FULL_COST} 💎\n"
            f"   Глубокий взгляд на все стороны жизни.\n\n"
            f"⭐ Персональный гороскоп — {config.HOROSCOPE_COST} 💎\n"
            f"   Энергетика текущего периода.\n\n"
            f"🗺️ Бесплатная карта — 0 💎 (раз в {config.FREE_CARD_COOLDOWN_HOURS}ч)\n"
            f"   Одна карта: «Что сейчас важно понять?»\n\n"
            f"У тебя сейчас {crystals} 💎."
        )
        await update.effective_message.reply_text(response, reply_markup=_paid_reading_keyboard(crystals))
        await db.save_message(user_id, "sofia", response, "reading_options")
        await db.update_user_state(user_id, SofiaState.TARO_ASK_NUMBERS)
        return

    if any(w in text_lower for w in ["нет", "не сейчас", "позже", "не хочу", "отстань"]):
        response = "Хорошо, милый человек. Я никуда не денусь. Поговорим, когда будешь готов."
        await update.effective_message.reply_text(response)
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
            f"❤️ Расклад на любовь (3 карты) — {config.TARO_LOVE_COST} 💎\n"
            f"⚖️ Расклад на выбор (3 карты) — {config.TARO_DECISION_COST} 💎\n"
            f"💼 Расклад на дело (5 карт) — {config.TARO_CAREER_COST} 💎\n"
            f"🔮 Малый расклад (5 карт) — {config.TARO_SMALL_COST} 💎\n"
            f"🃏 Полный расклад (20 карт) — {config.TARO_FULL_COST} 💎\n"
            f"⭐ Гороскоп — {config.HOROSCOPE_COST} 💎\n"
            f"🗺️ Бесплатная карта — 0 💎\n\n"
            f"У тебя сейчас {crystals} 💎. Выбери кнопкой ниже или напиши: «любовь», «выбор», «дело», «малый», «полный», «гороскоп»."
        )
        await update.effective_message.reply_text(response, reply_markup=_paid_reading_keyboard(crystals))
        await db.save_message(user_id, "sofia", response, "reading_options")
        return

    # Бесплатная 1-карта по тексту
    if wants_free_card(text_lower):
        await _handle_free_card(update, user_id, user)
        return

    # Карта дня по тексту
    if wants_card_of_day(text_lower):
        await _handle_card_of_day(update, user_id, user)
        return

    # Выбрал тип расклада
    if reading_type_state:
        if reading_type_state == SofiaState.HOROSCOPE:
            await _execute_horoscope(update, user_id, user)
            return

        # Параметры по типу расклада
        spread_params = {
            SofiaState.TARO_SMALL:    (config.TARO_SMALL_COST, 5, "Малый расклад", "small"),
            SofiaState.TARO_FULL:     (config.TARO_FULL_COST, 20, "Полный расклад", "full"),
            SofiaState.TARO_LOVE:     (config.TARO_LOVE_COST, 3, "Расклад на любовь", "love"),
            SofiaState.TARO_CAREER:   (config.TARO_CAREER_COST, 5, "Расклад на дело", "career"),
            SofiaState.TARO_DECISION: (config.TARO_DECISION_COST, 3, "Расклад на выбор", "decision"),
        }
        if reading_type_state not in spread_params:
            return
        cost, card_count, type_name, reading_type_str = spread_params[reading_type_state]

        crystals = user.get("crystals", 0)
        if crystals < cost:
            response = (
                f"Мне нужно немного сил. У тебя {crystals} 💎, а для {type_name.lower()} нужно {cost} 💎. "
                f"Обратись к администратору для пополнения."
            )
            await update.effective_message.reply_text(response)
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
        await update.effective_message.reply_text(response)
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
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_paid_reading(update: Update, user_id: int, text: str, user: dict, state: str) -> None:
    """Состояния TARO_SMALL / TARO_FULL / TARO_LOVE / TARO_CAREER / TARO_DECISION — ожидание чисел."""
    numbers = _parse_numbers(text)

    # Параметры по типу расклада
    if state == SofiaState.TARO_SMALL:
        needed, full, cost, spread_type = 5, False, config.TARO_SMALL_COST, None
    elif state == SofiaState.TARO_FULL:
        needed, full, cost, spread_type = 20, True, config.TARO_FULL_COST, None
    elif state == SofiaState.TARO_LOVE:
        needed, full, cost, spread_type = 3, False, config.TARO_LOVE_COST, "love"
    elif state == SofiaState.TARO_CAREER:
        needed, full, cost, spread_type = 5, False, config.TARO_CAREER_COST, "career"
    elif state == SofiaState.TARO_DECISION:
        needed, full, cost, spread_type = 3, False, config.TARO_DECISION_COST, "decision"
    else:
        needed, full, cost, spread_type = 5, False, config.TARO_SMALL_COST, None

    if len(numbers) >= needed:
        if spread_type:
            await _execute_thematic_reading(update, user_id, user, numbers[:needed], spread_type, cost)
        else:
            await _execute_taro_reading(update, user_id, user, numbers[:needed], full, cost)
    else:
        response = (
            f"Мне нужно {needed} чисел от 1 до {config.MAX_TARO_NUMBER}. "
            f"Ты указал(а) {len(numbers)}. Напиши все числа."
        )
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "clarification")


async def _handle_horoscope_state(update: Update, user_id: int, text: str, user: dict) -> None:
    """Состояние HOROSCOPE — выполнить гороскоп."""
    crystals = user.get("crystals", 0)
    cost = config.HOROSCOPE_COST
    if crystals < cost:
        await update.effective_message.reply_text(
            f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎."
        )
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return
    await _execute_horoscope(update, user_id, user)


async def _handle_free_card(update: Update, user_id: int, user: dict) -> None:
    """Бесплатная 1-карта Таро — «Что сейчас важно понять?».
    BUGFIX: Сбрасываем состояние в CONVERSATION после карты."""
    can = await db.can_get_free_card(user_id, config.FREE_CARD_COOLDOWN_HOURS)
    if not can:
        response = (
            f"Одну карту я уже для тебя сегодня открывала. "
            f"Следующая будет доступна через {config.FREE_CARD_COOLDOWN_HOURS} часов. "
            f"А пока — давай просто поговорим."
        )
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "free_card_cooldown")
        # BUGFIX: Всегда возвращаем в CONVERSATION
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    name = user.get("name") or user.get("first_name") or "милый человек"

    # Контекст последних разговоров
    recent = await db.get_recent_messages(user_id, limit=4)
    context = " | ".join(m["content"][:80] for m in recent if m["role"] == "user") if recent else ""

    await update.effective_message.reply_text("🗺️ Тяну для тебя карту...")

    card_text = await generate_single_card(name=name, question_context=context)

    await db.mark_free_card_used(user_id)
    await db.save_message(user_id, "sofia", card_text, "free_card")
    await _send_long_message(update, card_text)
    # BUGFIX: Сбрасываем состояние в CONVERSATION после бесплатной карты
    await db.update_user_state(user_id, SofiaState.CONVERSATION)


async def _execute_taro_reading(update: Update, user_id: int, user: dict,
                                 numbers: list[int], full: bool, cost: int) -> None:
    """Исполняет расклад Таро с реальными именами карт."""
    type_name = "Полный расклад" if full else "Малый расклад"

    success = await db.spend_crystals(user_id, cost, type_name)
    if not success:
        crystals = await db.get_user_crystals(user_id)
        response = (
            f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. "
            f"Обратись к администратору."
        )
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    recent = await db.get_recent_messages(user_id, limit=4)
    topic_parts = [m["content"][:80] for m in recent if m["role"] == "user"]
    topic = " | ".join(topic_parts[-3:]) if topic_parts else "общий вопрос"

    name = user.get("name") or user.get("first_name") or "милый человек"

    # Маппинг чисел → имена карт
    card_names = {n: get_tarot_card_name(n) for n in numbers}

    await update.effective_message.reply_text("🗺️ Раскладываю карты... Дай мне минутку.")

    reading = await generate_taro_reading(
        name=name,
        question=topic,
        numbers=numbers,
        full=full,
        card_names=card_names,
    )

    type_label = "🃏 Полный расклад судьбы" if full else "🔮 Малый расклад"
    await db.save_message(user_id, "sofia", reading, f"taro_{'full' if full else 'small'}")
    await _send_long_message(update, f"{type_label}\n\n{reading}")
    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await db.update_user_profile(user_id, reading_type=None)


# ─── Round 5: тематический расклад (любовь/дело/выбор) ───

THEMATIC_LABELS = {
    "love": ("❤️ Расклад на любовь", "thematic_love"),
    "career": ("💼 Расклад на дело", "thematic_career"),
    "decision": ("⚖️ Расклад на выбор", "thematic_decision"),
}


async def _execute_thematic_reading(update: Update, user_id: int, user: dict,
                                     numbers: list[int], spread_type: str, cost: int) -> None:
    """Исполняет тематический расклад Таро (love/career/decision) с реальными именами карт."""
    label, msg_tag = THEMATIC_LABELS.get(spread_type, ("🔮 Тематический расклад", "thematic"))

    success = await db.spend_crystals(user_id, cost, label)
    if not success:
        crystals = await db.get_user_crystals(user_id)
        response = (
            f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. "
            f"Обратись к администратору."
        )
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    recent = await db.get_recent_messages(user_id, limit=4)
    topic_parts = [m["content"][:80] for m in recent if m["role"] == "user"]
    topic = " | ".join(topic_parts[-3:]) if topic_parts else "общий вопрос"

    name = user.get("name") or user.get("first_name") or "милый человек"
    card_names = {n: get_tarot_card_name(n) for n in numbers}

    await update.effective_message.reply_text("🗺️ Раскладываю карты... Дай мне минутку.")

    reading = await generate_thematic_reading(
        name=name,
        question=topic,
        numbers=numbers,
        spread_type=spread_type,
        card_names=card_names,
    )

    await db.save_message(user_id, "sofia", reading, msg_tag)
    await _send_long_message(update, f"{label}\n\n{reading}")
    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await db.update_user_profile(user_id, reading_type=None)


# ─── Round 5: карта дня ───

async def _handle_card_of_day(update: Update, user_id: int, user: dict) -> None:
    """Карта дня — ежедневная мини-практика через одну карту Таро. Cooldown 20ч."""
    can = await db.can_get_card_of_day(user_id, config.CARD_OF_DAY_COOLDOWN_HOURS)
    if not can:
        response = (
            f"Карту дня я уже для тебя сегодня открывала. "
            f"Следующая будет доступна через {config.CARD_OF_DAY_COOLDOWN_HOURS} часов. "
            f"А пока — давай просто поговорим, или загляни в /today."
        )
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "card_of_day_cooldown")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    name = user.get("name") or user.get("first_name") or "милый человек"
    zodiac = _get_zodiac_from_user(user)

    await update.effective_message.reply_text("🃏 Тяну для тебя карту дня... Дай минутку.")

    try:
        emotional = await db.get_emotional_memory(user_id, min_importance=2)
        card_msg = await generate_card_of_day(name=name, zodiac=zodiac, emotional=emotional)
        await db.mark_card_of_day_used(user_id)
        await db.save_message(user_id, "sofia", card_msg, "card_of_day")
        await _send_long_message(update, card_msg)
    except Exception as e:
        logger.error(f"Card of day error for {user_id}: {e}", exc_info=True)
        fallback = (
            f"Сегодня туман густой, {name}, не могу разглядеть карту. "
            f"Попробуй ещё раз чуть позже."
        )
        await update.effective_message.reply_text(fallback)
        await db.save_message(user_id, "sofia", fallback, "card_of_day_error")

    await db.update_user_state(user_id, SofiaState.CONVERSATION)


# ─── Round 5: рассылка администратора ───

async def _handle_broadcast(update: Update, user_id: int, text: str, user: dict) -> None:
    """Состояние BROADCAST — админ написал текст для рассылки.
    Если «отмена» — выходим без рассылки."""
    text_stripped = text.strip()

    if text_stripped.lower() in ("отмена", "cancel", "/cancel", "/отмена"):
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await update.effective_message.reply_text("📢 Рассылка отменена.")
        return

    if len(text_stripped) < 3:
        await update.effective_message.reply_text("Слишком короткий текст. Напиши подробнее или «отмена».")
        return

    # Считаем получателей и предупреждаем о лимите serverless
    total = await db.count_broadcast_recipients()
    await update.effective_message.reply_text(
        f"📢 Начинаю рассылку. Получателей: {total}. "
        f"Отправляю батчами по {config.BROADCAST_BATCH}..."
    )

    import asyncio as _aio
    sent = 0
    failed = 0
    offset = 0
    bot = update.get_bot()

    while True:
        batch = await db.get_broadcast_recipients(config.BROADCAST_BATCH, offset)
        if not batch:
            break

        for recipient_id in batch:
            try:
                await bot.send_message(chat_id=recipient_id, text=text_stripped)
                await db.mark_broadcast_sent(recipient_id)
                sent += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Broadcast to {recipient_id} failed: {e}")

            # Telegram rate limit ~30 msg/sec
            await _aio.sleep(config.BROADCAST_RATE_MS / 1000.0)

        offset += config.BROADCAST_BATCH

        # Сохраняем в историю как сообщение Софии (одно, от имени админа)
        await db.save_message(user_id, "sofia", f"[РАССЫЛКА] {text_stripped}", "admin_broadcast")

        # Если вышли за разумный предел serverless (Vercel ~10-60s) — прерываем
        if offset >= 500:
            await update.effective_message.reply_text(
                f"⚠️ Достигнут лимит 500 сообщений за запуск. Отправлено: {sent}. "
                f"Остальным досылай следующей командой /broadcast."
            )
            break

    await db.update_user_state(user_id, SofiaState.CONVERSATION)
    await update.effective_message.reply_text(
        f"✅ Рассылка завершена. Отправлено: {sent}, ошибок: {failed}."
    )


async def _execute_horoscope(update: Update, user_id: int, user: dict) -> None:
    """Исполняет гороскоп."""
    cost = config.HOROSCOPE_COST
    crystals = user.get("crystals", 0)

    if crystals < cost:
        response = (
            f"Не хватает кристаллов. У тебя {crystals} 💎, нужно {cost} 💎. "
            f"Обратись к администратору."
        )
        await update.effective_message.reply_text(response)
        await db.save_message(user_id, "sofia", response, "insufficient_crystals")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    success = await db.spend_crystals(user_id, cost, "Гороскоп")
    if not success:
        await update.effective_message.reply_text("Не удалось списать кристаллы. Попробуй позже.")
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        return

    name = user.get("name") or user.get("first_name") or "милый человек"
    birth_date = user.get("birth_date")
    date_str = birth_date.strftime("%d.%m.%Y") if hasattr(birth_date, "strftime") else (str(birth_date) if birth_date else "")
    zodiac = _get_zodiac_from_user(user)

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
        zodiac=zodiac,
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
    elif data == "reading:love":
        await _start_reading_flow(update, user_id, user, SofiaState.TARO_LOVE, "love", 3, config.TARO_LOVE_COST, "Расклад на любовь")
    elif data == "reading:career":
        await _start_reading_flow(update, user_id, user, SofiaState.TARO_CAREER, "career", 5, config.TARO_CAREER_COST, "Расклад на дело")
    elif data == "reading:decision":
        await _start_reading_flow(update, user_id, user, SofiaState.TARO_DECISION, "decision", 3, config.TARO_DECISION_COST, "Расклад на выбор")
    elif data == "reading:horoscope":
        await _execute_horoscope(update, user_id, user)
    elif data == "reading:free_card":
        await _handle_free_card(update, user_id, user)
    elif data == "reading:card_of_day":
        await _handle_card_of_day(update, user_id, user)
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
    elif data == "admin:stats":
        if user_id == config.ADMIN_ID:
            await cmd_stats(update, context)
    elif data == "admin:users":
        if user_id == config.ADMIN_ID:
            stats = await db.get_user_stats()
            if not stats:
                await _safe_reply(update, "Пока нет пользователей.")
                return
            lines = ["👥 Пользователи:\n"]
            for s in stats[:20]:
                name = s.get("name") or s.get("first_name") or s.get("username", "—")
                lines.append(
                    f"• {name} (@{s.get('username', '—')}): "
                    f"{s.get('crystals', 0)} 💎, "
                    f"{s.get('message_count', 0)} сообщ., "
                    f"сост: {s.get('state', '—')}"
                )
            await _safe_reply(update, "\n".join(lines))
    elif data == "admin:broadcast_prompt":
        if user_id == config.ADMIN_ID:
            await _safe_reply(update, "📢 Напиши текст рассылки, и я отправлю его всем пользователям. Для отмены напиши «отмена».")
            await db.update_user_state(user_id, SofiaState.BROADCAST)


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
        await update.effective_message.reply_text("У нас пока нет истории диалога.")
        return

    lines = []
    for msg in messages:
        role = "👤 Ты" if msg["role"] == "user" else "👵 София"
        content = msg["content"][:150] + ("..." if len(msg["content"]) > 150 else "")
        lines.append(f"{role}: {content}")

    await update.effective_message.reply_text("📜 Последние сообщения:\n\n" + "\n\n".join(lines))


async def _show_menu(update: Update, user: dict) -> None:
    name = user.get("name") or user.get("first_name") or "милый человек"
    crystals = user.get("crystals", 0)
    zodiac = _get_zodiac_from_user(user)
    zodiac_line = f"\n♊ {zodiac}" if zodiac else ""

    menu = (
        f"📋 Вот что можно сделать, {name}:{zodiac_line}\n\n"
        f"💬 Просто пиши — и мы поговорим\n"
        f"🌅 /today — послание дня (бесплатно)\n"
        f"🃏 /card_of_day — карта дня\n"
        f"💚 /mood — я спрошу, как ты\n"
        f"📊 «профиль» — твоя карточка\n"
        f"💎 «баланс» — сколько кристаллов\n"
        f"📜 «история» — последние сообщения\n"
        f"📥 /export_my_history — наша история\n"
        f"🔗 /invite — пригласить близкого (+1 💎)\n"
        f"🌅 /subscribe — утренние весточки\n"
        f"🔄 /reset — начать заново\n\n"
        f"─── Расклады ───\n"
        f"❤️ «расклад на любовь» — 3 карты ({config.TARO_LOVE_COST} 💎)\n"
        f"⚖️ «расклад на выбор» — 3 карты ({config.TARO_DECISION_COST} 💎)\n"
        f"💼 «расклад на дело» — 5 карт ({config.TARO_CAREER_COST} 💎)\n"
        f"🔮 «малый расклад» — 5 карт ({config.TARO_SMALL_COST} 💎)\n"
        f"🃏 «полный расклад» — 20 карт ({config.TARO_FULL_COST} 💎)\n"
        f"⭐ «гороскоп» — персональный ({config.HOROSCOPE_COST} 💎)\n"
        f"🗺️ «бесплатная карта» — 1 карта (0 💎, раз в {config.FREE_CARD_COOLDOWN_HOURS}ч)\n\n"
        f"У тебя {crystals} 💎"
    )
    await update.effective_message.reply_text(menu, reply_markup=_paid_reading_keyboard(crystals))


def _parse_date(text: str):
    from datetime import date as date_type

    text = text.strip().replace(",", ".").replace("-", ".").replace("/", ".")
    formats = ["%d.%m.%Y", "%d.%m.%y", "%Y.%m.%d"]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt).date()
            current_year = datetime.now().year
            if 1900 <= parsed.year <= current_year:
                return parsed
        except ValueError:
            continue

    match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
    if match:
        try:
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if year < 100:
                year += 1900 if year >= 40 else 2000
            current_year = datetime.now().year
            if 1900 <= year <= current_year and 1 <= month <= 12 and 1 <= day <= 31:
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


# ─────────────────── Подтверждение удаления данных ───────────────────

async def _handle_delete_confirm(update: Update, user_id: int, text: str) -> None:
    """Обработка ответа на запрос удаления данных."""
    text_lower = text.lower().strip()
    if "удалить навсегда" in text_lower:
        await update.effective_message.reply_text(
            "Прощай, милый человек. Я стираю всё, что знала о тебе. "
            "Если когда-нибудь вернёшься — я буду рада, но начну с чистого листа."
        )
        try:
            await db.delete_user_data(user_id)
            logger.info(f"User {user_id} data deleted (GDPR)")
        except Exception as e:
            logger.error(f"Delete user data error for {user_id}: {e}")
            await update.effective_message.reply_text(
                "Что-то не получилось стереть записи. Попробуй позже или обратись к администратору."
            )
    else:
        # Отмена
        await db.update_user_state(user_id, SofiaState.CONVERSATION)
        await update.effective_message.reply_text(
            "Хорошо. Я ничего не стану стирать. Я останусь здесь, если захочешь поговорить."
        )


# ─────────────────── Команда /export_my_history ───────────────────

async def cmd_export_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экспорт истории диалога пользователя."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    if not user.get("onboarding_completed"):
        await update.effective_message.reply_text(
            "У нас пока нет истории. Сначала давай познакомимся — напиши /start."
        )
        return

    try:
        messages = await db.get_recent_messages(user_id, limit=100)
        if not messages:
            await update.effective_message.reply_text("У нас пока нет истории диалога.")
            return

        name = user.get("name") or user.get("first_name") or "Пользователь"
        lines = [f"📜 История разговоров с Софией — {name}\n{'='*40}\n"]
        for msg in messages:
            who = "👵 София" if msg["role"] == "sofia" else "👤 Ты"
            content = msg["content"][:300]
            lines.append(f"{who}: {content}\n")

        text = "\n".join(lines)

        if len(text) > 4000:
            text = text[:3950] + "\n\n... (показаны последние 100 сообщений)"

        await update.effective_message.reply_text(text)
    except Exception as e:
        logger.error(f"cmd_export_history error: {e}", exc_info=True)
        await update.effective_message.reply_text("Не удалось загрузить историю. Попробуй позже.")


# ─────────────────── Команда /mood (проверка настроения) ───────────────────

async def cmd_mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """София спрашивает о настроении — заботливая проверка."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    if not user.get("onboarding_completed"):
        await update.effective_message.reply_text(
            "Сначала давай познакомимся. Напиши /start и пройди короткий путь."
        )
        return

    name = user.get("name") or user.get("first_name") or "милый человек"
    zodiac = _get_zodiac_from_user(user)

    try:
        emotional = await db.get_emotional_memory(user_id, min_importance=2)
        last_topic = user.get("last_topic_summary") or ""
        checkin = await generate_mood_checkin(name, emotional, last_topic, zodiac)
        await update.effective_message.reply_text(checkin)
        await db.save_message(user_id, "sofia", checkin, "mood_checkin")
    except Exception as e:
        logger.error(f"cmd_mood error: {e}", exc_info=True)
        fallback = f"{name}, как ты сегодня? Я тут подумала о тебе..."
        await update.effective_message.reply_text(fallback)
        await db.save_message(user_id, "sofia", fallback, "mood_checkin")


# ─────────────────── Round 5: /card_of_day ───────────────────

async def cmd_card_of_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карта дня — ежедневная мини-практика через одну карту Таро."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if not user:
        await update.effective_message.reply_text("Напиши /start, чтобы начать.")
        return

    if not user.get("onboarding_completed"):
        await update.effective_message.reply_text(
            "Сначала давай познакомимся. Напиши /start и пройди короткий путь."
        )
        return

    await _handle_card_of_day(update, user_id, user)


# ─────────────────── Round 5: /broadcast (admin) ───────────────────

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск рассылки администратором. Переводит в состояние BROADCAST."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id

    if user_id != config.ADMIN_ID:
        await update.effective_message.reply_text("Эта команда только для администратора.")
        return

    await db.update_user_state(user_id, SofiaState.BROADCAST)
    await update.effective_message.reply_text(
        "📢 Напиши текст рассылки — я отправлю его всем активным пользователям. "
        "Для отмены напиши «отмена»."
    )


# ─────────────────── Голосовые сообщения ───────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка голосовых сообщений — София реагирует тепло, но просит текст."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_or_create_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )

    await db.touch_last_seen(user_id)

    name = user.get("name") or user.get("first_name") or "милый человек"
    state = user.get("state", SofiaState.START)

    if state in (SofiaState.ASK_NAME, SofiaState.ASK_BIRTH_DATE,
                 SofiaState.ASK_BIRTH_TIME, SofiaState.ASK_BIRTH_PLACE,
                 SofiaState.PROBING, SofiaState.TARO_ASK_NUMBERS,
                 SofiaState.TARO_SMALL, SofiaState.TARO_FULL,
                 SofiaState.TARO_LOVE, SofiaState.TARO_CAREER, SofiaState.TARO_DECISION,
                 SofiaState.AWAITING_DELETE_CONFIRM):
        await update.effective_message.reply_text(
            f"Слышу тебя, {name}. Но мне нужно, чтобы ты написал(а) это текстом — "
            f"я пока не умею слушать голос. Напиши, пожалуйста."
        )
        return

    response = (
        f"Слышу твой голос, {name}. Жаль, что не могу разобрать слова — "
        f"мои уши ещё не настолько чуткие. Напиши мне текстом, о чём хочешь рассказать?"
    )
    await update.effective_message.reply_text(response)
    await db.save_message(user_id, "user", "[голосовое сообщение]")
    await db.save_message(user_id, "sofia", response, "voice_fallback")


# ─────────────────── Стикеры и фото ───────────────────

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реакция на стикер — тёплая, но с просьбой текста."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_or_create_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    await db.touch_last_seen(user_id)

    name = user.get("name") or user.get("first_name") or "милый человек"
    import random
    reactions = [
        f"Красивый стикер, {name}. Но мне было бы легче понять тебя, если бы ты написал(а) словами.",
        f"Я вижу, ты хочешь что-то сказать, {name}. Напиши мне — я прочитаю.",
        f"Иногда один стикер стоит тысячи слов. Но я бы всё же предпочла услышать твои слова, {name}.",
    ]
    response = random.choice(reactions)
    await update.effective_message.reply_text(response)
    await db.save_message(user_id, "user", "[стикер]")
    await db.save_message(user_id, "sofia", response, "sticker_fallback")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реакция на фото."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_or_create_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    await db.touch_last_seen(user_id)

    name = user.get("name") or user.get("first_name") or "милый человек"
    caption = update.message.caption
    if caption and len(caption.strip()) > 2:
        await db.save_message(user_id, "user", f"[фото] {caption.strip()}")
        update.message.text = caption.strip()
        await handle_message(update, context)
        return

    response = (
        f"Вижу, ты прислал(а) мне картинку, {name}. Жаль, мои глаза пока "
        f"не видят изображения. Расскажи мне текстом, что на ней — и мы поговорим."
    )
    await update.effective_message.reply_text(response)
    await db.save_message(user_id, "user", "[фото]")
    await db.save_message(user_id, "sofia", response, "photo_fallback")


# ─────────────────── Видео-кружки ───────────────────

async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реакция на видео-кружок — София просит текст."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = await db.get_or_create_user(
        user_id,
        update.effective_user.username,
        update.effective_user.first_name,
    )
    await db.touch_last_seen(user_id)

    name = user.get("name") or user.get("first_name") or "милый человек"
    response = (
        f"Вижу твоё видео, {name}. Жаль, мои глаза пока не видят движущихся картинок. "
        f"Расскажи мне текстом, что хотела(а) показать — и мы поговорим."
    )
    await update.effective_message.reply_text(response)
    await db.save_message(user_id, "user", "[видео-кружок]")
    await db.save_message(user_id, "sofia", response, "video_note_fallback")


# ─────────────────── Настройка обработчиков ───────────────────

def setup_handlers(application: Application) -> None:
    """Регистрирует все обработчики. Вызывается из webhook.py."""
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("invite", cmd_invite))
    application.add_handler(CommandHandler("subscribe", cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    application.add_handler(CommandHandler("delete_my_data", cmd_delete_my_data))
    application.add_handler(CommandHandler("export_my_history", cmd_export_history))
    application.add_handler(CommandHandler("mood", cmd_mood))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("card_of_day", cmd_card_of_day))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CallbackQueryHandler(handle_callback))
    # Голосовые, стикеры, фото, видео-кружки — ДО текстового обработчика
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    # Текстовый обработчик — последний
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
