---
Task ID: 1
Agent: main
Task: Создание проекта Telegram-бота «София» — AI-гадалка/психолог

Work Log:
- Создана структура директорий /home/z/sofia-bot/ (api/, bot/)
- Написан config.py — загрузка переменных окружения через python-dotenv
- Написан bot/fsm.py — FSM с 11 состояниями, триггеры грубости, детектор типа расклада
- Написан bot/database.py — asyncpg пул, создание таблиц, полный CRUD (users, conversations, memory_facts, transactions)
- Написан bot/gemini.py — интеграция с Google Gemini 2.5 Flash: генерация ответов, карта судьбы, расклады Таро, гороскоп, извлечение фактов из памяти
- Написан bot/memory.py — сборка контекста из БД для Gemini (имя, факты, последние 20 сообщений)
- Написан bot/handlers.py — все обработчики: /start, /profile, /balance, /admin, FSM-маршрутизация, грубость, rate limiting
- Написан api/webhook.py — Flask WSGI для Vercel serverless, обработка webhook
- Написан local_polling.py — локальная разработка в режиме polling
- Написан set_webhook.py — утилита для установки webhook
- Написаны requirements.txt, vercel.json, .env, .env.example, .gitignore, README.md
- Исправлен баг: context.user_data заменён на поле reading_type в БД (надёжнее в serverless)
- Все файлы прошли проверку синтаксиса Python

Stage Summary:
- Полный проект создан в /home/z/sofia-bot/
- 10 Python-модулей + конфигурационные файлы
- Архитектура: Telegram → Vercel webhook → Flask → PTB Application → Gemini API
- База данных: PostgreSQL (Supabase/Vercel Postgres) через asyncpg
- Система памяти: автоматическое извлечение фактов каждые 5 сообщений
- Система кристаллов: 3 бесплатных, малый расклад (1💎), полный (3💎), гороскоп (2💎)
- Для запуска нужно: настроить DATABASE_URL в .env, установить зависимости, запустить local_polling.py
