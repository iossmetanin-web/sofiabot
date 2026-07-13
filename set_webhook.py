#!/usr/bin/env python3
"""
Установка webhook для Telegram бота на Vercel.

Использование:
    python set_webhook.py

Убедись, что в .env указаны TELEGRAM_BOT_TOKEN и WEBHOOK_URL.
"""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not TOKEN:
    print("❌ TELEGRAM_BOT_TOKEN не задан в .env")
    sys.exit(1)

if not WEBHOOK_URL:
    print("❌ WEBHOOK_URL не задан в .env")
    print("   Пример: https://your-app.vercel.app/api/webhook")
    sys.exit(1)

# Установка webhook
url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
payload = {
    "url": WEBHOOK_URL,
    "allowed_updates": ["message"],
    "drop_pending_updates": True,
}

print(f"🔧 Установка webhook: {WEBHOOK_URL}")
response = requests.post(url, json=payload)
result = response.json()

if result.get("ok"):
    print(f"✅ Webhook установлен: {result}")
else:
    print(f"❌ Ошибка: {result}")
    sys.exit(1)

# Проверка
info_url = f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo"
info_response = requests.get(info_url)
info = info_response.json()

if info.get("ok"):
    webhook_info = info["result"]
    print(f"\n📋 Webhook Info:")
    print(f"   URL: {webhook_info.get('url', '—')}")
    print(f"   Pending updates: {webhook_info.get('pending_update_count', 0)}")
    print(f"   Last error: {webhook_info.get('last_error_message', 'нет')}")
