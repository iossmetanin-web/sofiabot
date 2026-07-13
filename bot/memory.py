"""
Логика памяти и извлечения фактов.
Сборка контекста из БД для Gemini (8 последних сообщений + важные факты).
Адаптировано под connection-per-request (database.py).
"""
import logging

from bot import database as db
from bot.gemini import extract_memory_facts

logger = logging.getLogger(__name__)


async def build_context(user_id: int) -> str:
    """
    Собирает полный контекст пользователя для Gemini.
    """
    try:
        user = await db.get_user(user_id)
        if not user:
            return ""

        facts = await db.get_memory_facts(user_id, min_importance=3)

        parts = []

        # Основная информация
        info_lines = []
        name = user.get("name") or user.get("first_name")
        if name:
            info_lines.append(f"Имя: {name}")

        birth_date = user.get("birth_date")
        if birth_date:
            if hasattr(birth_date, "strftime"):
                info_lines.append(f"Дата рождения: {birth_date.strftime('%d.%m.%Y')}")
            else:
                info_lines.append(f"Дата рождения: {birth_date}")

        if user.get("birth_time"):
            bt = user["birth_time"]
            info_lines.append(f"Время рождения: {bt.strftime('%H:%M') if hasattr(bt, 'strftime') else bt}")

        if user.get("birth_place"):
            info_lines.append(f"Место рождения: {user['birth_place']}")

        info_lines.append(f"Состояние: {user.get('state', 'CONVERSATION')}")
        info_lines.append(f"Кристаллов: {user.get('crystals', 0)}")

        if info_lines:
            parts.append("[Информация о пользователе]\n" + "\n".join(info_lines))

        # Факты
        if facts:
            type_names = {
                "pain": "Боль", "relationship": "Отношения", "work": "Дело",
                "family": "Семья", "goal": "Цель", "fear": "Страх",
                "promise": "Обещание", "personality": "Характер", "health": "Здоровье",
            }
            fact_lines = []
            for f in facts[:8]:
                label = type_names.get(f["fact_type"], f["fact_type"])
                fact_lines.append(f"- {label}: {f['fact_content']}")
            parts.append("[Ключевые факты]\n" + "\n".join(fact_lines))

        return "\n\n".join(parts)

    except Exception as e:
        logger.error(f"build_context error for user {user_id}: {e}")
        return ""


async def should_extract_facts(user_id: int) -> bool:
    """Определяет, нужно ли извлекать факты из диалога."""
    try:
        count = await db.get_message_count(user_id)
        return count > 0 and count % 5 == 0
    except Exception as e:
        logger.error(f"should_extract_facts error: {e}")
        return False


async def extract_and_save_facts(user_id: int) -> None:
    """Извлекает факты из последних сообщений и сохраняет в память."""
    try:
        messages = await db.get_recent_messages(user_id, limit=6)
        if not messages:
            return

        facts = await extract_memory_facts(messages)

        for fact in facts:
            await db.save_memory_fact(
                user_id=user_id,
                fact_type=fact.get("fact_type", "personality"),
                content=str(fact.get("fact_content", ""))[:500],
                importance=min(5, max(1, int(fact.get("importance", 3)))),
            )

        if facts:
            logger.info(f"Saved {len(facts)} facts for user {user_id}")

    except Exception as e:
        logger.error(f"extract_and_save_facts error for user {user_id}: {e}")


async def get_fate_context_for_reading(user_id: int) -> str:
    """Краткий контекст для расклада Таро."""
    try:
        messages = await db.get_recent_messages(user_id, limit=4)
        facts = await db.get_memory_facts(user_id, min_importance=3)

        parts = []
        if facts:
            fact_lines = [f"- {f['fact_type']}: {f['fact_content']}" for f in facts[:5]]
            parts.append("Факты: " + "; ".join(fact_lines))

        if messages:
            msg_lines = [f"{m['role']}: {m['content'][:100]}" for m in messages[-3:]]
            parts.append("Последний диалог: " + " | ".join(msg_lines))

        return "; ".join(parts)

    except Exception as e:
        logger.error(f"get_fate_context_for_reading error: {e}")
        return ""
