"""
Работа с PostgreSQL через asyncpg.
Connection-per-request: каждое обращение открывает соединение и сразу закрывает.
НЕТ пула — на Vercel Serverless пул убивает PostgreSQL зомби-коннектами.
"""
import os
import asyncpg
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


async def get_conn():
    """Открываем соединение на ОДИН запрос и сразу закрываем."""
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    """Создание таблиц при первом запуске."""
    conn = await get_conn()
    try:
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
        # Индексы
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversations_user_created
            ON conversations(user_id, created_at DESC)
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_memory_facts_user_importance
            ON memory_facts(user_id, importance DESC)
        ''')
        logger.info("Database tables initialized")
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
            """INSERT INTO users (user_id, username, first_name, crystals, state)
               VALUES ($1, $2, $3, 3, 'START')
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
            "UPDATE users SET state = $1, updated_at = NOW() WHERE user_id = $2",
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
               SET message_count = message_count + 1, updated_at = NOW()
               WHERE user_id = $1
               RETURNING message_count""",
            user_id,
        )
        return new_count
    finally:
        await conn.close()


async def get_user_crystals(user_id: int) -> int:
    """Возвращает текущий баланс кристаллов."""
    conn = await get_conn()
    try:
        crystals = await conn.fetchval(
            "SELECT crystals FROM users WHERE user_id = $1", user_id
        )
        return crystals or 0
    finally:
        await conn.close()


async def spend_crystals(user_id: int, amount: int, description: str = "") -> bool:
    """Списывает кристаллы, если хватает. Возвращает True при успехе."""
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
    """Начисляет кристаллы пользователю."""
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
    """Сохраняет сообщение в историю диалога."""
    conn = await get_conn()
    try:
        await conn.execute(
            "INSERT INTO conversations (user_id, role, content, emotion_tag) VALUES ($1, $2, $3, $4)",
            user_id, role, content, emotion_tag,
        )
    finally:
        await conn.close()


async def get_recent_messages(user_id: int, limit: int = 8) -> list[dict]:
    """Последние N сообщений (8 по умолчанию — чтобы уложиться в таймаут Vercel)."""
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
    """Общее количество сообщений в диалоге."""
    conn = await get_conn()
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE user_id = $1", user_id
        )
        return count or 0
    finally:
        await conn.close()


# ─── Memory Facts ───

async def save_memory_fact(user_id: int, fact_type: str, content: str, importance: int = 3):
    """Сохраняет факт о пользователе в память (с дедупликацией)."""
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
    """Возвращает важные факты о пользователе."""
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


# ─── Admin ───

async def get_user_stats() -> list[dict]:
    """Статистика пользователей для админа."""
    conn = await get_conn()
    try:
        rows = await conn.fetch("""
            SELECT user_id, username, first_name, name,
                   crystals, state, message_count,
                   rudeness_count, is_blocked, created_at
            FROM users ORDER BY created_at DESC
        """)
        return [dict(r) for r in rows]
    finally:
        await conn.close()
