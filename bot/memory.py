"""
Логика памяти и извлечения фактов.
Сборка контекста из БД для LLM (8 последних сообщений + важные факты + эмоциональная память).
Концепция v2: эмоциональная память (главная боль, близкие, обещания, незакрытые вопросы).
"""
import logging

from bot import database as db
from bot.gemini import extract_memory_facts, extract_emotional_memory

logger = logging.getLogger(__name__)


async def build_context(user_id: int) -> str:
    """Собирает полный контекст пользователя для LLM."""
    try:
        user = await db.get_user(user_id)
        if not user:
            return ""

        facts, emotional = await _gather_memory(user_id)

        parts = []
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

        if emotional:
            em_names = {
                "main_pain": "Главная боль", "loved_one": "Близкий человек",
                "promise": "Обещание (себе)", "unfinished_question": "Незакрытый вопрос",
                "life_event": "Событие жизни", "fear": "Страх", "goal": "Цель",
                "breakthrough": "Прорыв",
            }
            em_lines = []
            for em in emotional[:5]:
                label = em_names.get(em["memory_type"], em["memory_type"])
                em_lines.append(f"- {label}: {em['content']}")
            parts.append("[Эмоциональная память]\n" + "\n".join(em_lines))

        return "\n\n".join(parts)

    except Exception as e:
        logger.error(f"build_context error for user {user_id}: {e}")
        return ""


async def _gather_memory(user_id: int):
    """Параллельно получает facts и emotional (один запрос — два SELECT, но asyncpg не пулит)."""
    import asyncio
    facts_task = db.get_memory_facts(user_id, min_importance=3)
    em_task = db.get_emotional_memory(user_id, min_importance=3)
    return await asyncio.gather(facts_task, em_task)


async def should_extract_facts(user_id: int) -> bool:
    """Определяет, нужно ли извлекать факты из диалога."""
    try:
        count = await db.get_message_count(user_id)
        return count > 0 and count % 5 == 0
    except Exception as e:
        logger.error(f"should_extract_facts error: {e}")
        return False


async def extract_and_save_facts(user_id: int) -> None:
    """Извлекает факты и эмоциональную память из последних сообщений и сохраняет."""
    try:
        messages = await db.get_recent_messages(user_id, limit=8)
        if not messages:
            return

        import asyncio
        facts, emotional = await asyncio.gather(
            extract_memory_facts(messages),
            extract_emotional_memory(messages),
        )

        for fact in facts:
            await db.save_memory_fact(
                user_id=user_id,
                fact_type=fact.get("fact_type", "personality"),
                content=str(fact.get("fact_content", ""))[:500],
                importance=min(5, max(1, int(fact.get("importance", 3)))),
            )

        for em in emotional:
            await db.save_emotional_memory(
                user_id=user_id,
                memory_type=em.get("memory_type", "life_event"),
                content=str(em.get("content", ""))[:500],
                context="auto-extracted",
                importance=min(5, max(1, int(em.get("importance", 3)))),
            )

        if facts or emotional:
            logger.info(f"Saved {len(facts)} facts + {len(emotional)} emotional for user {user_id}")

        # Сохраняем краткую сводку последней темы
        if messages:
            last_user_msgs = [m["content"][:80] for m in messages[-3:] if m["role"] == "user"]
            if last_user_msgs:
                summary = " | ".join(last_user_msgs[-2:])
                await db.update_last_topic_summary(user_id, summary)

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
