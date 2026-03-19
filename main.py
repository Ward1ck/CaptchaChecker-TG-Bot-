import asyncio
import os
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ChatPermissions
import random
import sqlite3
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# Храним состояние капчи {user_id: {"answer": int, "chat_id": int}}
captcha_answers = {}

# ID бота (заполним при старте)
BOT_ID = None

# Простое хранилище проверенных пользователей (SQLite)
DB_PATH = os.path.join(os.path.dirname(__file__), 'verified.sqlite3')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                verified_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

def is_user_verified(chat_id: int, user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT 1 FROM verified_users WHERE chat_id=? AND user_id=? LIMIT 1",
            (chat_id, user_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()

def mark_user_verified(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO verified_users (chat_id, user_id, verified_at) VALUES (?, ?, strftime('%s','now'))",
            (chat_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()

# Задаём ограниченные права - запрещаем писать
restricted_permissions = ChatPermissions(
    can_send_messages=False,
    can_send_media_messages=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False
)

# Разрешённые права
full_permissions = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False
)

@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def on_user_joined(message: types.Message):
    for user in message.new_chat_members:
        # Не обрабатываем самого бота и администраторов/создателя
        if BOT_ID is not None and user.id == BOT_ID:
            continue
        try:
            member = await bot.get_chat_member(message.chat.id, user.id)
            if member.status in ("administrator", "creator"):
                continue
        except Exception:
            # Если не удалось получить статус — продолжаем как обычно
            pass
        # Если уже верифицирован ранее — ничего не делаем
        if is_user_verified(message.chat.id, user.id):
            continue
        try:
            # Ограничиваем нового пользователя в чате
            await bot.restrict_chat_member(chat_id=message.chat.id,
                                          user_id=user.id,
                                          permissions=restricted_permissions)
            # Генерируем простую капчу (сложение)
            a = random.randint(1, 10)
            b = random.randint(1, 10)
            captcha_question = f"Привет, {user.full_name}! Чтобы писать в группе, нужно решить капчу:\nСколько будет {a} + {b}?"
            captcha_answer = a + b

            captcha_answers[user.id] = {"answer": captcha_answer, "chat_id": message.chat.id, "question": captcha_question}

            # Отправляем капчу в личные сообщения
            try:
                await bot.send_message(user.id, captcha_question)
            except Exception:
                # Не удалось отправить ЛС - уведомляем в группе
                await message.reply(f"{user.get_mention(as_html=True)}, пожалуйста, разреши боту писать тебе в личные сообщения и повторно войди в группу, чтобы пройти капчу.",
                                    parse_mode='HTML')

            # Запускаем задачу по таймауту проверки капчи
            asyncio.create_task(wait_captcha_timeout(message.chat.id, user.id))

        except Exception as e:
            print(f"Error restricting user {user.id}: {e}")

@dp.message_handler(lambda message: message.chat.type == 'private')
async def on_private_message(message: types.Message):
    user_id = message.from_user.id
    if user_id in captcha_answers:
        try:
            answer = int(message.text.strip())
            if answer == captcha_answers[user_id]["answer"]:
                # Снимаем ограничения в группе
                target_chat_id = captcha_answers[user_id]["chat_id"]

                await bot.restrict_chat_member(chat_id=target_chat_id,
                                              user_id=user_id,
                                              permissions=full_permissions,
                                              until_date=0)
                await message.answer("Капча пройдена! Теперь вы можете писать в группе.")
                mark_user_verified(target_chat_id, user_id)
                captcha_answers.pop(user_id, None)
            else:
                q = captcha_answers[user_id].get("question", "Попробуйте ещё раз решить пример.")
                await message.answer(f"Ответ неверный, попробуйте еще раз.\n{q}")
        except ValueError:
            q = captcha_answers[user_id].get("question", "Попробуйте ещё раз решить пример.")
            await message.answer(f"Пожалуйста, введите только число.\n{q}")

@dp.message_handler(lambda message: message.chat.type in ("group", "supergroup"))
async def on_group_message(message: types.Message):
    user_id = message.from_user.id
    if user_id in captcha_answers:
        try:
            # Если пользователь еще не прошел капчу для этого чата — удаляем его сообщение
            if captcha_answers[user_id]["chat_id"] == message.chat.id:
                await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception as e:
            print(f"Error deleting message from unverified user {user_id}: {e}")

async def wait_captcha_timeout(chat_id: int, user_id: int, timeout=300):
    await asyncio.sleep(timeout)
    if user_id in captcha_answers and captcha_answers[user_id]["chat_id"] == chat_id:
        try:
            # Если капча не пройдена - кикаем пользователя
            await bot.kick_chat_member(chat_id=chat_id, user_id=user_id)
            captcha_answers.pop(user_id, None)
            # Можно уведомить группу
            await bot.send_message(chat_id, f"Пользователь {user_id} был удален за несоблюдение капчи.")
        except Exception as e:
            print(f"Error kicking user {user_id}: {e}")

if __name__ == '__main__':
    async def on_startup(dispatcher):
        global BOT_ID
        init_db()
        me = await bot.get_me()
        BOT_ID = me.id

    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
