"""
Работа с PostgreSQL через asyncpg.
Connection-per-request: каждое обращение открывает соединение и сразу закрывает.
НЕТ пула — на Vercel Serverless пул убивает PostgreSQL зомби-коннектами.

Концепция v2: добавлены
- колонки users: last_seen_at, gender, age_group, probing_count, last_free_card_at, last_topic_summary
- таблица emotional_memory (главная боль, близкие, обещания, незакрытые вопросы, события)
"""
import os
import asyncpg
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


async def get_conn():
    """Открываем соединение на ОДИН запрос и сразу закрываем."""
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    """Создание таблиц и миграции при первом запуске. Безопасно (IF NOT EXISTS)."""
    conn = await get_conn()
    try:
        # ─── users ───
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                name TEXT,
                birth_date DATE,
                birth_time TIME,
                birth_place TEXT,
                crystals INTEGER DEFAULT 3,
                state TEXT DEFAULT 'START',
                rudeness_count INTEGER DEFAULT 0,
                is_blocked BOOLEAN DEFAULT FALSE,
                message_count INTEGER DEFAULT 0,
                reading_type TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Концепция v2 — новые колонки users (безопасная миграция)
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT NULL')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS age_group TEXT DEFAULT NULL')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS probing_count INTEGER DEFAULT 0')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS last_free_card_at TIMESTAMP')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS last_topic_summary TEXT')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE')
        # Концепция v2 round 2 — рефералы и daily horoscope
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_reward_given BOOLEAN DEFAULT FALSE')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_horoscope_date DATE')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_horoscope_opt_in BOOLEAN DEFAULT FALSE')

        # ─── conversations ───
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'sofia')),
                content TEXT NOT NULL,
                emotion_tag TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ─── memory_facts (существующая) ───
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS memory_facts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                fact_type TEXT NOT NULL,
                fact_content TEXT NOT NULL,
                importance INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ─── emotional_memory (НОВАЯ — концепция v2) ───
        # Типы: main_pain, loved_one, promise, unfinished_question, life_event, fear, goal, breakthrough
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS emotional_memory (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                context TEXT,
                importance INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ─── transactions ───
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                type TEXT NOT NULL CHECK (type IN ('spend', 'add', 'admin_gift')),
                amount INTEGER NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # ─── rate_limits ───
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS rate_limits (
                user_id BIGINT PRIMARY KEY,
                last_message_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Индексы
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversations_user_created
            ON conversations(user_id, created_at DESC)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_memory_facts_user_importance
            ON memory_facts(user_id, importance DESC)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_emotional_memory_user_importance
            ON emotional_memory(user_id, importance DESC)
        ''')
        logger.info("Database tables initialized (concept v2 migration applied)")
    finally:
        await conn.close()


# ─── Users ───

async def get_user(user_id: int) -> Optional[dict]:
    """Возвращает пользователя как dict или None."""
    conn = await get_conn()
    try:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def create_user(user_id: int, username: str, first_name: str) -> dict:
    """Создаёт пользователя с 3 кристаллами, возвращает его."""
    conn = await get_conn()
    try:
        await conn.execute(
            """INSERT INTO users (user_id, username, first_name, crystals, state, last_seen_at)
               VALUES ($1, $2, $3, 3, 'START', NOW())
               ON CONFLICT (user_id) DO NOTHING""",
            user_id, username, first_name,
        )
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        logger.info(f"New user created: {user_id}")
        return dict(row)
    finally:
        await conn.close()


async def get_or_create_user(user_id: int, username: str = None, first_name: str = None) -> dict:
    """Возвращает пользователя или создаёт нового."""
    user = await get_user(user_id)
    if user is None:
        user = await create_user(user_id, username or "", first_name or "")
    return user


async def update_user_state(user_id: int, state: str):
    """Обновляет состояние FSM."""
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET state = $1, updated_at = NOW(), last_seen_at = NOW() WHERE user_id = $2",
            state, user_id,
        )
    finally:
        await conn.close()


async def update_user_profile(user_id: int, **fields):
    """Универсальное обновление профиля."""
    if not fields:
        return
    conn = await get_conn()
    try:
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k} = ${len(vals) + 1}")
            vals.append(v)
        vals.append(user_id)
        query = f"UPDATE users SET {', '.join(sets)}, updated_at = NOW() WHERE user_id = ${len(vals)}"
        await conn.execute(query, *vals)
    finally:
        await conn.close()


async def touch_last_seen(user_id: int):
    """Обновляет время последней активности (без изменения state)."""
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET last_seen_at = NOW(), updated_at = NOW() WHERE user_id = $1",
            user_id,
        )
    finally:
        await conn.close()


async def increment_rudeness(user_id: int) -> int:
    """Увеличивает счётчик грубости, возвращает новое значение."""
    conn = await get_conn()
    try:
        new_count = await conn.fetchval(
            """UPDATE users
               SET rudeness_count = rudeness_count + 1, updated_at = NOW()
               WHERE user_id = $1
               RETURNING rudeness_count""",
            user_id,
        )
        return new_count
    finally:
        await conn.close()


async def reset_rudeness(user_id: int):
    """Сбрасывает счётчик грубости и блокировку."""
    conn = await get_conn()
    try:
        await conn.execute(
            """UPDATE users
               SET rudeness_count = 0, is_blocked = FALSE, updated_at = NOW()
               WHERE user_id = $1""",
            user_id,
        )
    finally:
        await conn.close()


async def increment_message_count(user_id: int) -> int:
    """Увеличивает счётчик сообщений, возвращает новое значение."""
    conn = await get_conn()
    try:
        new_count = await conn.fetchval(
            """UPDATE users
               SET message_count = message_count + 1, updated_at = NOW(), last_seen_at = NOW()
               WHERE user_id = $1
               RETURNING message_count""",
            user_id,
        )
        return new_count
    finally:
        await conn.close()


async def increment_probing(user_id: int) -> int:
    """Увеличивает счётчик вопросов прощупывания."""
    conn = await get_conn()
    try:
        new_count = await conn.fetchval(
            """UPDATE users SET probing_count = probing_count + 1, updated_at = NOW()
               WHERE user_id = $1 RETURNING probing_count""",
            user_id,
        )
        return new_count or 0
    finally:
        await conn.close()


async def mark_onboarding_completed(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET onboarding_completed = TRUE, updated_at = NOW() WHERE user_id = $1",
            user_id,
        )
    finally:
        await conn.close()


async def get_user_crystals(user_id: int) -> int:
    conn = await get_conn()
    try:
        crystals = await conn.fetchval(
            "SELECT crystals FROM users WHERE user_id = $1", user_id
        )
        return crystals or 0
    finally:
        await conn.close()


async def spend_crystals(user_id: int, amount: int, description: str = "") -> bool:
    conn = await get_conn()
    try:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT crystals FROM users WHERE user_id = $1 FOR UPDATE", user_id
            )
            if current is None or current < amount:
                return False
            await conn.execute(
                "UPDATE users SET crystals = crystals - $1, updated_at = NOW() WHERE user_id = $2",
                amount, user_id,
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description) VALUES ($1, 'spend', $2, $3)",
                user_id, amount, description,
            )
            logger.info(f"User {user_id} spent {amount} crystals: {description}")
            return True
    finally:
        await conn.close()


async def add_crystals(user_id: int, amount: int, description: str = "", txn_type: str = "add"):
    conn = await get_conn()
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET crystals = crystals + $1, updated_at = NOW() WHERE user_id = $2",
                amount, user_id,
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description) VALUES ($1, $2, $3, $4)",
                user_id, txn_type, amount, description,
            )
            logger.info(f"User {user_id} received {amount} crystals: {description}")
    finally:
        await conn.close()


# ─── Conversations ───

async def save_message(user_id: int, role: str, content: str, emotion_tag: str = None):
    conn = await get_conn()
    try:
        await conn.execute(
            "INSERT INTO conversations (user_id, role, content, emotion_tag) VALUES ($1, $2, $3, $4)",
            user_id, role, content, emotion_tag,
        )
    finally:
        await conn.close()


async def get_recent_messages(user_id: int, limit: int = 8) -> list[dict]:
    conn = await get_conn()
    try:
        rows = await conn.fetch(
            "SELECT role, content, emotion_tag, created_at FROM conversations WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
        return [dict(r) for r in reversed(rows)]
    finally:
        await conn.close()


async def get_message_count(user_id: int) -> int:
    conn = await get_conn()
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE user_id = $1", user_id
        )
        return count or 0
    finally:
        await conn.close()


async def get_last_user_message(user_id: int) -> Optional[dict]:
    """Последнее сообщение пользователя (для темы возвращения)."""
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            """SELECT content, created_at FROM conversations
               WHERE user_id = $1 AND role = 'user'
               ORDER BY created_at DESC LIMIT 1""",
            user_id,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


async def update_last_topic_summary(user_id: int, summary: str):
    """Сохраняет краткую сводку последней темы разговора."""
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET last_topic_summary = $1, updated_at = NOW() WHERE user_id = $2",
            summary[:300], user_id,
        )
    finally:
        await conn.close()


# ─── Memory Facts ───

async def save_memory_fact(user_id: int, fact_type: str, content: str, importance: int = 3):
    conn = await get_conn()
    try:
        existing = await conn.fetchval(
            """SELECT id FROM memory_facts
               WHERE user_id = $1 AND fact_type = $2 AND fact_content = $3""",
            user_id, fact_type, content,
        )
        if existing:
            await conn.execute(
                """UPDATE memory_facts
                   SET importance = GREATEST(importance, $1), updated_at = NOW()
                   WHERE id = $2""",
                importance, existing,
            )
        else:
            await conn.execute(
                """INSERT INTO memory_facts (user_id, fact_type, fact_content, importance)
                   VALUES ($1, $2, $3, $4)""",
                user_id, fact_type, content, importance,
            )
    finally:
        await conn.close()


async def get_memory_facts(user_id: int, min_importance: int = 3) -> list[dict]:
    conn = await get_conn()
    try:
        rows = await conn.fetch(
            """SELECT fact_type, fact_content, importance FROM memory_facts
               WHERE user_id = $1 AND importance >= $2
               ORDER BY importance DESC, updated_at DESC LIMIT 10""",
            user_id, min_importance,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ─── Emotional Memory (НОВАЯ — концепция v2) ───

async def save_emotional_memory(
    user_id: int, memory_type: str, content: str, context: str = "", importance: int = 3
):
    """Сохраняет эмоциональный факт (главная боль, близкий человек, обещание...)."""
    conn = await get_conn()
    try:
        # Дедупликация по типу и содержимому
        existing = await conn.fetchval(
            """SELECT id FROM emotional_memory
               WHERE user_id = $1 AND memory_type = $2 AND content = $3""",
            user_id, memory_type, content,
        )
        if existing:
            await conn.execute(
                """UPDATE emotional_memory
                   SET importance = GREATEST(importance, $1), context = $2, updated_at = NOW()
                   WHERE id = $3""",
                importance, context, existing,
            )
        else:
            await conn.execute(
                """INSERT INTO emotional_memory (user_id, memory_type, content, context, importance)
                   VALUES ($1, $2, $3, $4, $5)""",
                user_id, memory_type, content, context, importance,
            )
    finally:
        await conn.close()


async def get_emotional_memory(user_id: int, min_importance: int = 3) -> list[dict]:
    """Возвращает эмоциональные факты о пользователе."""
    conn = await get_conn()
    try:
        rows = await conn.fetch(
            """SELECT memory_type, content, context, importance FROM emotional_memory
               WHERE user_id = $1 AND importance >= $2
               ORDER BY importance DESC, updated_at DESC LIMIT 10""",
            user_id, min_importance,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def get_top_emotional_memory(user_id: int) -> Optional[dict]:
    """Самый важный эмоциональный факт (для приветствия возвращения)."""
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            """SELECT memory_type, content, importance FROM emotional_memory
               WHERE user_id = $1 ORDER BY importance DESC, updated_at DESC LIMIT 1""",
            user_id,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


# ─── Free 1-card cooldown ───

async def can_get_free_card(user_id: int, cooldown_hours: int = 24) -> bool:
    conn = await get_conn()
    try:
        last = await conn.fetchval(
            "SELECT last_free_card_at FROM users WHERE user_id = $1", user_id
        )
        if not last:
            return True
        if hasattr(last, "tzinfo") and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        diff = (datetime.now(timezone.utc) - last).total_seconds()
        return diff >= cooldown_hours * 3600
    finally:
        await conn.close()


async def mark_free_card_used(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET last_free_card_at = NOW(), updated_at = NOW() WHERE user_id = $1",
            user_id,
        )
    finally:
        await conn.close()


# ─── Rate Limiting (через БД, не в памяти) ───

async def check_rate_limit(user_id: int, min_interval_seconds: float = 2.0) -> bool:
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT last_message_at FROM rate_limits WHERE user_id = $1", user_id
        )
        if row:
            last = row["last_message_at"]
            if last:
                from datetime import datetime as _dt, timezone as _tz
                if hasattr(last, "tzinfo") and last.tzinfo is None:
                    last = last.replace(tzinfo=_tz.utc)
                diff = (_dt.now(_tz.utc) - last).total_seconds()
                if diff < min_interval_seconds:
                    return False
        return True
    finally:
        await conn.close()


async def update_rate_limit(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute(
            """INSERT INTO rate_limits (user_id, last_message_at)
               VALUES ($1, NOW())
               ON CONFLICT (user_id) DO UPDATE SET last_message_at = NOW()""",
            user_id,
        )
    finally:
        await conn.close()


# ─── Admin ───

async def get_user_stats() -> list[dict]:
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            SELECT user_id, username, first_name, name,
                   crystals, state, message_count,
                   rudeness_count, is_blocked, created_at, last_seen_at,
                   onboarding_completed
            FROM users ORDER BY created_at DESC
        """)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ─── Referrals (концепция v2 round 2) ───

async def set_referred_by(user_id: int, referred_by: int):
    """Устанавливает реферера (только если ещё не установлен и не сам себя)."""
    if user_id == referred_by:
        return
    conn = await get_conn()
    try:
        await conn.execute(
            """UPDATE users SET referred_by = $1
               WHERE user_id = $2 AND referred_by IS NULL""",
            referred_by, user_id,
        )
    finally:
        await conn.close()


async def get_referral_count(user_id: int) -> int:
    """Сколько пользователей пришли по реферальной ссылке этого пользователя."""
    conn = await get_conn()
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by = $1", user_id
        )
        return count or 0
    finally:
        await conn.close()


async def mark_referral_reward_given(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET referral_reward_given = TRUE WHERE user_id = $1",
            user_id,
        )
    finally:
        await conn.close()


async def was_referred(user_id: int) -> bool:
    conn = await get_conn()
    try:
        v = await conn.fetchval(
            "SELECT referred_by IS NOT NULL FROM users WHERE user_id = $1", user_id
        )
        return bool(v)
    finally:
        await conn.close()


# ─── Daily horoscope ───

async def can_get_daily_horoscope(user_id: int) -> bool:
    """True, если сегодня пользователь ещё не получал daily horoscope.
    Используем CURRENT_DATE из PostgreSQL для консистентности с mark_daily_horoscope_used."""
    conn = await get_conn()
    try:
        result = await conn.fetchval(
            "SELECT (last_daily_horoscope_date IS NULL OR last_daily_horoscope_date < CURRENT_DATE) FROM users WHERE user_id = $1",
            user_id,
        )
        return bool(result)
    finally:
        await conn.close()


async def mark_daily_horoscope_used(user_id: int):
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET last_daily_horoscope_date = CURRENT_DATE WHERE user_id = $1",
            user_id,
        )
    finally:
        await conn.close()


async def set_daily_horoscope_opt_in(user_id: int, opt_in: bool):
    conn = await get_conn()
    try:
        await conn.execute(
            "UPDATE users SET daily_horoscope_opt_in = $1 WHERE user_id = $2",
            opt_in, user_id,
        )
    finally:
        await conn.close()


async def get_daily_horoscope_subscribers() -> list[dict]:
    """Пользователи, подписанные на daily horoscope и завершившие онбординг."""
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            SELECT user_id, name, first_name, birth_date, birth_time, birth_place
            FROM users
            WHERE daily_horoscope_opt_in = TRUE
              AND onboarding_completed = TRUE
              AND is_blocked = FALSE
              AND birth_date IS NOT NULL
            ORDER BY user_id
            LIMIT 100
        """)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ─── Admin analytics (расширенная) ───

async def get_admin_analytics() -> dict:
    """Сводная статистика для /stats. Оптимизировано — один запрос вместо 12."""
    conn = await get_conn()
    try:
        # Единый запрос с несколькими агрегатами — вместо 12 отдельных коннектов
        row = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE last_seen_at > NOW() - INTERVAL '24 hours') AS active_24h,
                (SELECT COUNT(*) FROM users WHERE last_seen_at > NOW() - INTERVAL '7 days') AS active_7d,
                (SELECT COUNT(*) FROM users WHERE onboarding_completed = TRUE) AS onboarding_done,
                (SELECT COALESCE(SUM(crystals), 0) FROM users) AS total_crystals,
                (SELECT COUNT(*) FROM conversations) AS total_messages,
                (SELECT COUNT(*) FROM transactions WHERE type = 'spend') AS paid_transactions,
                (SELECT COUNT(DISTINCT user_id) FROM transactions WHERE type = 'spend') AS paying_users,
                (SELECT COUNT(*) FROM users WHERE is_blocked = TRUE) AS blocked_users,
                (SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL) AS referral_users,
                (SELECT COUNT(*) FROM users WHERE daily_horoscope_opt_in = TRUE) AS daily_subscribers
        """)

        total_users = row["total_users"] or 0
        paying_users = row["paying_users"] or 0
        conversion = round((paying_users / total_users * 100), 1) if total_users else 0.0

        # Топ-5 активных (второй запрос)
        top_active = await conn.fetch("""
            SELECT name, first_name, username, message_count, crystals
            FROM users WHERE is_blocked = FALSE
            ORDER BY message_count DESC LIMIT 5
        """)
        top_active = [dict(r) for r in top_active]

        return {
            "total_users": total_users,
            "active_24h": row["active_24h"] or 0,
            "active_7d": row["active_7d"] or 0,
            "onboarding_done": row["onboarding_done"] or 0,
            "total_crystals": row["total_crystals"] or 0,
            "total_messages": row["total_messages"] or 0,
            "paid_transactions": row["paid_transactions"] or 0,
            "paying_users": paying_users,
            "conversion_pct": conversion,
            "blocked_users": row["blocked_users"] or 0,
            "referral_users": row["referral_users"] or 0,
            "daily_subscribers": row["daily_subscribers"] or 0,
            "top_active": top_active,
        }
    finally:
        await conn.close()


# ─── GDPR: удаление данных пользователя ───

async def delete_user_data(user_id: int) -> bool:
    """Полное удаление всех данных пользователя. Возвращает True при успехе."""
    conn = await get_conn()
    try:
        async with conn.transaction():
            # FK ON DELETE CASCADE сработает для conversations, memory_facts,
            # emotional_memory, transactions. rate_limits — без FK, удалим вручную.
            await conn.execute("DELETE FROM rate_limits WHERE user_id = $1", user_id)
            result = await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
            # result вида "DELETE 1" или "DELETE 0"
            return "1" in result.split()[-1] if result else False
    finally:
        await conn.close()
