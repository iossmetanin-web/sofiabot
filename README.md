# 👵 София — AI-гадалка / психолог

Telegram-бот в образе мудрой русской бабушки-староверки Софии (1883 г., Енисей, тайга). Ведёт живой диалог, создаёт «Карту судьбы», делает расклады Таро и гороскопы.

## 🏗 Архитектура

```
sofia-bot/
├── api/
│   └── webhook.py          # Vercel serverless entry point (Flask)
├── bot/
│   ├── __init__.py
│   ├── handlers.py         # Обработчики Telegram
│   ├── fsm.py              # Машина состояний
│   ├── gemini.py           # Интеграция с Gemini API
│   ├── database.py         # Работа с PostgreSQL (asyncpg)
│   └── memory.py           # Логика памяти и извлечения фактов
├── config.py               # Переменные окружения
├── local_polling.py        # Локальная разработка (polling)
├── set_webhook.py          # Установка webhook для Vercel
├── requirements.txt
├── vercel.json
└── .env                    # Не коммитить!
```

## ⚙️ Установка и запуск (локально)

### 1. Клонирование

```bash
git clone https://github.com/YOUR_USERNAME/sofia-bot.git
cd sofia-bot
```

### 2. Создание виртуального окружения

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows
```

### 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4. Настройка окружения

Скопируй `.env.example` в `.env` и заполни:

```bash
cp .env.example .env
```

Обязательные переменные:
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather
- `GEMINI_API_KEY` — ключ от Google AI Studio
- `DATABASE_URL` — PostgreSQL connection string
- `ADMIN_ID` — твой Telegram ID (для /admin)

### 5. Подготовка базы данных

Убедись, что PostgreSQL запущен и доступен по `DATABASE_URL`. Таблицы создаются автоматически при первом запуске.

### 6. Запуск в режиме polling

```bash
python local_polling.py
```

## 🚀 Деплой на Vercel

### 1. Создай репозиторий на GitHub

```bash
git init
git add .
git commit -m "Initial commit: Sofia bot"
git remote add origin https://github.com/YOUR_USERNAME/sofia-bot.git
git push -u origin main
```

### 2. Подключи в Vercel

1. Зайди на [vercel.com](https://vercel.com)
2. Нажми **Import Project** → выбери репозиторий `sofia-bot`
3. Framework Preset: **Other**
4. Root Directory: `/` (по умолчанию)

### 3. Установи переменные окружения

В Vercel Dashboard → Settings → Environment Variables:

| Ключ | Значение |
|------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен бота |
| `GEMINI_API_KEY` | Ключ Gemini |
| `DATABASE_URL` | PostgreSQL connection string |
| `ADMIN_ID` | Твой Telegram ID |
| `WEBHOOK_URL` | `https://your-app.vercel.app/api/webhook` |

### 4. Установи webhook

После первого деплоя выполни:

```bash
python set_webhook.py
```

Или вручную:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-app.vercel.app/api/webhook"
```

### 5. Проверь

Отправь `/start` боту в Telegram.

## 💎 Система кристаллов

- Новый пользователь получает **3 бесплатных кристалла**
- 🔮 Малый расклад (5 карт) — **1 💎**
- 🃏 Полный расклад (20 карт) — **3 💎**
- ⭐ Гороскоп — **2 💎**

Пополнение через админа:
```
/admin add @username 5
```

## 📋 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и начало онбординга |
| `/profile` | Профиль и баланс кристаллов |
| `/balance` | Только баланс |
| `/admin` | Статистика (только для ADMIN_ID) |
| `меню` / `помощь` | Текстовое меню |
| `баланс` / `кристаллы` | Баланс |
| `извини` / `прости` | Снять блокировку за грубость |

## 🧠 Память

Бот запоминает факты о пользователе:
- Боли и проблемы
- Отношения
- Работа и цели
- Страхи и обещания
- Черты характера

Факты извлекаются автоматически каждые 5 сообщений через Gemini и используются для персонализации ответов.

## ⚠️ Ограничения

- Vercel Free Tier: таймаут serverless-функций — **10 секунд**. Gemini обычно отвечает за 3-7 секунд, но при нагрузке могут быть проблемы. Рекомендуется Vercel Pro (60 секунд).
- PostgreSQL: рекомендуется Supabase (бесплатный тариф) или Vercel Postgres.
- Inline-кнопки не используются — только текстовый диалог.

## 📄 Лицензия

Приватный проект. Все права защищены.
