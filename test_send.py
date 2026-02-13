import asyncio
import os
from aiogram import Bot
from dotenv import load_dotenv

async def test_channel_send():
    # Загружаем переменные из .env
    load_dotenv()
    
    token = os.getenv("BOT_TOKEN")
    # ALERTS_CHAT_ID может быть списком через запятую или одним ID
    chat_id_raw = os.getenv("ALERTS_CHAT_ID")
    
    if not token or not chat_id_raw:
        print("❌ Ошибка: Проверь BOT_TOKEN и ALERTS_CHAT_ID в .env файле")
        return

    bot = Bot(token=token)
    
    # Пробуем отправить в первый попавшийся ID из конфига
    target_id = chat_id_raw.split(",")[0].strip()
    
    print(f"🚀 Пробую отправить тестовое сообщение в: {target_id}...")
    
    try:
        msg = await bot.send_message(
            chat_id=target_id,
            text="🔔 *Тестовое сообщение*\n\nЕсли ты это видишь, значит бот может писать в этот канал/чат!",
            parse_mode="Markdown"
        )
        print(f"✅ Успешно отправлено! ID сообщения: {msg.message_id}")
    except Exception as e:
        print(f"❌ Ошибка при отправке: {e}")
        print("\n💡 Возможные причины:")
        print("1. Бот не добавлен в канал/чат.")
        print("2. Бот не назначен администратором с правом отправки сообщений.")
        print("3. Неправильный ID (для каналов должен начинаться с -100...).")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(test_channel_send())
