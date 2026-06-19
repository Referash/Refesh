import os
import json
import time
import threading
from datetime import datetime

import requests
import telebot
from telebot import types
from flask import Flask

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SITE_URL = os.getenv("SITE_URL", "")

DATA_FILE = "data.json"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
states = {}


@app.route("/")
def home():
    return "Bot is alive ✅"


def run_site():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def keep_ping():
    while True:
        try:
            if SITE_URL:
                requests.get(SITE_URL, timeout=10)
                print("PING OK")
        except Exception as e:
            print("PING ERROR:", e)
        time.sleep(300)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def empty_data():
    return {
        "last_task_id": 0,
        "last_request_id": 0,
        "users": {},
        "tasks": {},
        "requests": {}
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        data = empty_data()
        save_data(data)
        return data

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = empty_data()
        save_data(data)
        return data

    base = empty_data()
    for key in base:
        if key not in data:
            data[key] = base[key]

    return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(data, user_id, username=""):
    user_id = str(user_id)

    if user_id not in data["users"]:
        data["users"][user_id] = {
            "balance": 0,
            "created_tasks": 0,
            "completed_tasks": 0,
            "earned": 0,
            "username": username,
            "created_at": now()
        }

    if username:
        data["users"][user_id]["username"] = username

    return data["users"][user_id]


def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить ссылку")
    kb.row("📋 Задания", "👤 Профиль")
    return kb


def cancel_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("❌ Отмена")
    return kb


@bot.message_handler(commands=["start"])
def start(message):
    data = load_data()
    get_user(data, message.from_user.id, message.from_user.username or "")
    save_data(data)

    bot.send_message(
        message.chat.id,
        "👋 <b>Добро пожаловать в Реферальный бот!</b>\n\n"
        "🔄 Здесь пользователи обмениваются рефералами.\n\n"
        "📈 Добавляй свои ссылки, выполняй задания других участников "
        "и получай новых рефералов.\n\n"
        "👇 Выберите действие:",
        reply_markup=main_kb()
    )


@bot.message_handler(func=lambda m: m.text == "➕ Добавить ссылку")
def add_link(message):
    states[message.from_user.id] = {"step": "link"}

    bot.send_message(
        message.chat.id,
        "📎 <b>Отправьте вашу реферальную ссылку.</b>\n\n"
        "Пример:\nhttps://t.me/YourBot?start=123456",
        reply_markup=cancel_kb()
    )


@bot.message_handler(func=lambda m: m.from_user.id in states)
def state_handler(message):
    uid = message.from_user.id
    state = states[uid]

    if message.text == "❌ Отмена":
        states.pop(uid, None)
        bot.send_message(message.chat.id, "❌ Отменено.", reply_markup=main_kb())
        return

    if state["step"] == "link":
        link = message.text.strip()

        if not (
            link.startswith("https://t.me/")
            or link.startswith("http://t.me/")
            or link.startswith("t.me/")
        ):
            bot.send_message(
                message.chat.id,
                "⚠️ Отправьте Telegram ссылку.\n\n"
                "Пример:\nhttps://t.me/YourBot?start=123456"
            )
            return

        state["link"] = link
        state["step"] = "description"

        bot.send_message(
            message.chat.id,
            "📝 <b>Напишите, что нужно сделать пользователю.</b>\n\n"
            "Пример:\nЗапустить бота и выполнить 3 задания."
        )
        return

    if state["step"] == "description":
        desc = message.text.strip()

        if len(desc) < 5:
            bot.send_message(message.chat.id, "⚠️ Описание слишком короткое.")
            return

        state["description"] = desc
        state["step"] = "reward"

        bot.send_message(
            message.chat.id,
            "💎 <b>Укажите награду за выполнение.</b>\n\n"
            "Пишите числом.\nПример: 1"
        )
        return

    if state["step"] == "reward":
        try:
            reward = int(message.text.strip())
            if reward <= 0:
                raise ValueError
        except Exception:
            bot.send_message(message.chat.id, "⚠️ Награда должна быть числом. Например: 1")
            return

        state["reward"] = reward
        state["step"] = "limit"

        bot.send_message(
            message.chat.id,
            "👥 <b>Сколько людей нужно?</b>\n\n"
            "Пример: 10"
        )
        return

    if state["step"] == "limit":
        try:
            limit = int(message.text.strip())
            if limit <= 0:
                raise ValueError
        except Exception:
            bot.send_message(message.chat.id, "⚠️ Количество должно быть числом. Например: 10")
            return

        data = load_data()
        user = get_user(data, uid, message.from_user.username or "")

        data["last_task_id"] += 1
        task_id = str(data["last_task_id"])

        data["tasks"][task_id] = {
            "id": task_id,
            "owner_id": uid,
            "owner_username": message.from_user.username or "",
            "link": state["link"],
            "description": state["description"],
            "reward": state["reward"],
            "limit": limit,
            "done": 0,
            "created_at": now()
        }

        user["created_tasks"] += 1
        save_data(data)
        states.pop(uid, None)

        bot.send_message(
            message.chat.id,
            f"✅ <b>Задание #{task_id} создано!</b>\n\n"
            f"📎 Ссылка: {state['link']}\n"
            f"📝 Описание: {state['description']}\n"
            f"🎁 Награда: {state['reward']} балл\n"
            f"👥 Нужно: {limit}\n"
            f"📊 Выполнено: 0/{limit}",
            reply_markup=main_kb()
        )


@bot.message_handler(func=lambda m: m.text == "📋 Задания")
def show_tasks(message):
    data = load_data()
    uid = message.from_user.id

    available = []

    for task_id, task in data["tasks"].items():
        if int(task["owner_id"]) == uid:
            continue

        already = False
        for req in data["requests"].values():
            if str(req["task_id"]) == str(task_id) and int(req["worker_id"]) == uid:
                already = True
                break

        if not already:
            available.append(task)

    if not available:
        bot.send_message(message.chat.id, "📭 Сейчас нет доступных заданий.", reply_markup=main_kb())
        return

    task = available[0]

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🚀 Перейти", url=task["link"]))
    kb.add(types.InlineKeyboardButton("✅ Я выполнил", callback_data=f"done:{task['id']}"))

    bot.send_message(
        message.chat.id,
        f"📋 <b>Задание #{task['id']}</b>\n\n"
        f"📝 {task['description']}\n"
        f"🎁 Награда: {task['reward']} балл\n"
        f"📊 Выполнено: {task['done']}/{task['limit']}\n\n"
        f"1️⃣ Нажмите «Перейти»\n"
        f"2️⃣ Выполните условия\n"
        f"3️⃣ Нажмите «Я выполнил»\n\n"
        f"Создатель задания проверит, пришёл ли реферал.",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("done:"))
def done_task(call):
    task_id = call.data.split(":")[1]
    data = load_data()
    uid = call.from_user.id

    if task_id not in data["tasks"]:
        bot.answer_callback_query(call.id, "Задание уже удалено.")
        return

    task = data["tasks"][task_id]

    if int(task["owner_id"]) == uid:
        bot.answer_callback_query(call.id, "Нельзя выполнять своё задание.")
        return

    for req in data["requests"].values():
        if str(req["task_id"]) == str(task_id) and int(req["worker_id"]) == uid:
            bot.answer_callback_query(call.id, "Вы уже отправили заявку.")
            return

    data["last_request_id"] += 1
    request_id = str(data["last_request_id"])

    data["requests"][request_id] = {
        "id": request_id,
        "task_id": task_id,
        "owner_id": task["owner_id"],
        "worker_id": uid,
        "worker_username": call.from_user.username or "",
        "reward": task["reward"],
        "status": "pending",
        "created_at": now()
    }

    save_data(data)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{request_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{request_id}")
    )

    worker_name = "@" + call.from_user.username if call.from_user.username else str(uid)

    try:
        bot.send_message(
            int(task["owner_id"]),
            f"📩 <b>Новая заявка на проверку</b>\n\n"
            f"📋 Задание #{task_id}\n"
            f"👤 Пользователь: {worker_name}\n\n"
            f"📝 Условие:\n{task['description']}\n\n"
            f"📎 Ссылка:\n{task['link']}\n\n"
            f"Проверьте, пришёл ли реферал.\n"
            f"Если пришёл — нажмите ✅ Одобрить.",
            reply_markup=kb
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Заявка отправлена.")
    bot.send_message(
        call.message.chat.id,
        "✅ <b>Заявка отправлена на проверку.</b>\n\n"
        "Создатель проверит, пришёл ли реферал."
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("approve:") or call.data.startswith("reject:"))
def approve_reject(call):
    action, request_id = call.data.split(":")
    data = load_data()

    if request_id not in data["requests"]:
        bot.answer_callback_query(call.id, "Заявка уже обработана.")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    req = data["requests"][request_id]

    if int(req["owner_id"]) != call.from_user.id:
        bot.answer_callback_query(call.id, "Это может подтвердить только создатель задания.")
        return

    task_id = str(req["task_id"])
    worker_id = str(req["worker_id"])
    reward = int(req["reward"])

    if action == "reject":
        del data["requests"][request_id]
        save_data(data)

        try:
            bot.send_message(
                int(worker_id),
                "❌ <b>Задание отклонено.</b>\n\n"
                "Создатель не подтвердил, что реферал пришёл."
            )
        except Exception:
            pass

        bot.edit_message_text("❌ Вы отклонили заявку.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Отклонено.")
        return

    worker = get_user(data, worker_id)

    worker["balance"] += reward
    worker["completed_tasks"] += 1
    worker["earned"] += reward

    if task_id in data["tasks"]:
        data["tasks"][task_id]["done"] += 1

        if data["tasks"][task_id]["done"] >= data["tasks"][task_id]["limit"]:
            del data["tasks"][task_id]

    del data["requests"][request_id]
    save_data(data)

    try:
        bot.send_message(
            int(worker_id),
            f"✅ <b>Задание подтверждено!</b>\n\n"
            f"🎁 На баланс начислено: +{reward}\n"
            f"💎 Ваш баланс: {worker['balance']}"
        )
    except Exception:
        pass

    bot.edit_message_text(
        "✅ Вы одобрили заявку.\n\n"
        "🎁 Награда начислена пользователю.",
        call.message.chat.id,
        call.message.message_id
    )
    bot.answer_callback_query(call.id, "Одобрено.")


@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def profile(message):
    data = load_data()
    user = get_user(data, message.from_user.id, message.from_user.username or "")
    save_data(data)

    bot.send_message(
        message.chat.id,
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"💎 Баланс: {user['balance']}\n"
        f"📋 Создано заданий: {user['created_tasks']}\n"
        f"✅ Выполнено заданий: {user['completed_tasks']}\n"
        f"🎁 Получено наград: {user['earned']}",
        reply_markup=main_kb()
    )


@bot.message_handler(commands=["stats"])
def stats(message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return

    data = load_data()

    bot.send_message(
        message.chat.id,
        f"📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: {len(data['users'])}\n"
        f"📋 Активных заданий: {len(data['tasks'])}\n"
        f"⏳ Заявок на проверке: {len(data['requests'])}"
    )


@bot.message_handler(commands=["tasks"])
def admin_tasks(message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return

    data = load_data()

    if not data["tasks"]:
        bot.send_message(message.chat.id, "📭 Активных заданий нет.")
        return

    text = "📋 <b>Активные задания:</b>\n\n"

    for task_id, task in data["tasks"].items():
        text += (
            f"#{task_id} | {task['done']}/{task['limit']}\n"
            f"👤 owner: {task['owner_id']}\n"
            f"🎁 reward: {task['reward']}\n\n"
        )

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["delete_task"])
def delete_task(message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.send_message(message.chat.id, "Использование:\n/delete_task ID")
        return

    task_id = parts[1]
    data = load_data()

    if task_id not in data["tasks"]:
        bot.send_message(message.chat.id, "⚠️ Задание не найдено.")
        return

    del data["tasks"][task_id]

    to_delete = []
    for req_id, req in data["requests"].items():
        if str(req["task_id"]) == str(task_id):
            to_delete.append(req_id)

    for req_id in to_delete:
        del data["requests"][req_id]

    save_data(data)

    bot.send_message(message.chat.id, f"✅ Задание #{task_id} удалено.")


if __name__ == "__main__":
    threading.Thread(target=run_site, daemon=True).start()
    threading.Thread(target=keep_ping, daemon=True).start()

    print("BOT STARTED ✅")

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60)
        except Exception as e:
            print("BOT ERROR:", e)
            time.sleep(5)
