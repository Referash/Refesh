import os
import json
import time
import re
import threading
from datetime import datetime, timedelta

import requests
import telebot
from telebot import types
from flask import Flask

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SITE_URL = os.getenv("SITE_URL", "https://refesh.onrender.com/")

DATA_FILE = "data.json"
TASK_LIFETIME_DAYS = int(os.getenv("TASK_LIFETIME_DAYS", "7"))  # через сколько дней старое задание удаляется

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
    """Пингует сайт каждые 5 минут, чтобы Render не засыпал."""
    while True:
        try:
            url = (SITE_URL or "https://refesh.onrender.com/").strip()
            if not url.startswith("http"):
                url = "https://" + url

            r = requests.get(url, timeout=15)
            print(f"PING OK: {url} | status={r.status_code}")
        except Exception as e:
            print("PING ERROR:", e)

        time.sleep(300)  # 5 минут

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_dt(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now()

def dt_text(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def empty_data():
    return {
        "last_task_id": 0,
        "last_request_id": 0,
        "users": {},
        "tasks": {},
        "requests": {},
        "completed": {},
        "archived_tasks": {}
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
        # Если файл вдруг сломался, не затираем его молча, а сохраняем копию.
        try:
            broken_name = DATA_FILE + ".broken_" + str(int(time.time()))
            os.replace(DATA_FILE, broken_name)
            print("DATA FILE BROKEN, BACKUP:", broken_name)
        except Exception:
            pass
        data = empty_data()
        save_data(data)
        return data

    base = empty_data()
    for key in base:
        if key not in data:
            data[key] = base[key]

    # Миграция старых заданий, чтобы новые поля не ломали старые data.json.
    changed = False
    for task_id, task in list(data.get("tasks", {}).items()):
        created = task.get("created_at") or now()
        if "expires_at" not in task:
            task["expires_at"] = dt_text(parse_dt(created) + timedelta(days=TASK_LIFETIME_DAYS))
            changed = True
        if "done_users" not in task:
            task["done_users"] = []
            changed = True
        if "pending_users" not in task:
            task["pending_users"] = []
            changed = True
        if "status" not in task:
            task["status"] = "active"
            changed = True

    if changed:
        save_data(data)

    return data

def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def get_user(data, user_id, username=""):
    user_id = str(user_id)

    if user_id not in data["users"]:
        data["users"][user_id] = {
            "balance": 10,
            "created_tasks": 0,
            "completed_tasks": 0,
            "earned": 0,
            "username": username or "",
            "created_at": now()
        }

    if username:
        data["users"][user_id]["username"] = username

    return data["users"][user_id]

def cleanup_old_tasks(data):
    """Удаляет старые задания и возвращает владельцу неиспользованный резерв."""
    deleted = []
    current = datetime.now()

    for task_id, task in list(data.get("tasks", {}).items()):
        expires_at = parse_dt(task.get("expires_at", task.get("created_at", now())))
        if current < expires_at:
            continue

        owner = get_user(data, task.get("owner_id"))
        left = max(0, int(task.get("limit", 0)) - int(task.get("done", 0)))
        refund = left * int(task.get("reward", 0))
        owner["balance"] += refund

        data["archived_tasks"][str(task_id)] = {
            **task,
            "status": "expired",
            "expired_at": now(),
            "refund": refund
        }
        del data["tasks"][str(task_id)]
        deleted.append((str(task_id), int(task.get("owner_id", 0)), refund))

        # Все висящие заявки по старому заданию удаляем, чтобы не было повторок.
        for req_id, req in list(data.get("requests", {}).items()):
            if str(req.get("task_id")) == str(task_id):
                del data["requests"][req_id]

    return deleted

def load_clean_data():
    data = load_data()
    deleted = cleanup_old_tasks(data)
    if deleted:
        save_data(data)
        for task_id, owner_id, refund in deleted:
            try:
                bot.send_message(
                    int(owner_id),
                    f"🗑 <b>Задание #{task_id} автоматически удалено.</b>\n\n"
                    f"Причина: задание старше {TASK_LIFETIME_DAYS} дней.\n"
                    f"💎 Возврат за невыполненные рефы: {refund}"
                )
            except Exception:
                pass
    return data

def finish_task_if_needed(data, task_id):
    task = data["tasks"].get(str(task_id))
    if not task:
        return False

    if int(task.get("done", 0)) < int(task.get("limit", 0)):
        return False

    data["archived_tasks"][str(task_id)] = {
        **task,
        "status": "completed",
        "finished_at": now(),
        "refund": 0
    }
    del data["tasks"][str(task_id)]

    # На всякий случай удаляем все заявки по закрытому заданию.
    for req_id, req in list(data.get("requests", {}).items()):
        if str(req.get("task_id")) == str(task_id):
            del data["requests"][req_id]

    return True

def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Добавить ссылку")
    kb.row("📋 Задания", "👤 Профиль")
    kb.row("📊 Мои задания")
    return kb

def cancel_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("❌ Отмена")
    return kb

@bot.message_handler(commands=["start"])
def start(message):
    data = load_clean_data()
    get_user(data, message.from_user.id, message.from_user.username or "")
    save_data(data)

    bot.send_message(
        message.chat.id,
        "👋 <b>Добро пожаловать в Реферальный бот!</b>\n\n"
        "🔄 Здесь пользователи обмениваются рефералами.\n\n"
        "📈 Добавляй свои ссылки, выполняй задания других участников "
        "и получай новых рефералов.\n\n"
        "🎁 Новому пользователю начисляется 10 баллов.\n\n"
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
            "Пример:\nЗапустить бота и подписаться на спонсоров."
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
            "💎 <b>Укажите награду за 1 выполненного реферала.</b>\n\n"
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

        data = load_clean_data()
        user = get_user(data, uid, message.from_user.username or "")

        if user["balance"] < reward:
            bot.send_message(
                message.chat.id,
                f"❌ Недостаточно баллов на балансе.\n\n"
                f"💎 Награда: {reward}\n"
                f"💰 Ваш баланс: {user['balance']}\n\n"
                f"Укажите награду меньше или пополните баланс.",
                reply_markup=main_kb()
            )
            states.pop(uid, None)
            return

        state["reward"] = reward
        state["step"] = "limit"

        bot.send_message(
            message.chat.id,
            "👥 <b>Сколько рефералов нужно?</b>\n\n"
            "Пример: 4"
        )
        return

    if state["step"] == "limit":
        try:
            limit = int(message.text.strip())
            if limit <= 0:
                raise ValueError
        except Exception:
            bot.send_message(message.chat.id, "⚠️ Количество должно быть числом. Например: 4")
            return

        data = load_clean_data()
        user = get_user(data, uid, message.from_user.username or "")

        total_price = int(state["reward"]) * limit

        if user["balance"] < total_price:
            bot.send_message(
                message.chat.id,
                f"❌ Недостаточно баллов на балансе.\n\n"
                f"🎁 Награда за 1 рефа: {state['reward']}\n"
                f"👥 Нужно рефов: {limit}\n"
                f"💎 Всего нужно: {total_price}\n"
                f"💰 Ваш баланс: {user['balance']}\n\n"
                f"Уменьшите награду или количество рефов.",
                reply_markup=main_kb()
            )
            states.pop(uid, None)
            return

        user["balance"] -= total_price

        data["last_task_id"] += 1
        task_id = str(data["last_task_id"])

        created_at = datetime.now()
        data["tasks"][task_id] = {
            "id": task_id,
            "owner_id": uid,
            "owner_username": message.from_user.username or "",
            "link": state["link"],
            "description": state["description"],
            "reward": int(state["reward"]),
            "limit": limit,
            "done": 0,
            "done_users": [],
            "pending_users": [],
            "reserved_balance": total_price,
            "status": "active",
            "created_at": dt_text(created_at),
            "expires_at": dt_text(created_at + timedelta(days=TASK_LIFETIME_DAYS))
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
            f"👥 Нужно рефов: {limit}\n"
            f"📊 Пришло рефов: 0/{limit}\n"
            f"🕒 Автоудаление через: {TASK_LIFETIME_DAYS} дней\n\n"
            f"💎 Списано с баланса: {total_price}\n"
            f"💰 Остаток: {user['balance']}",
            reply_markup=main_kb()
        )

@bot.message_handler(func=lambda m: m.text == "📋 Задания")
def show_tasks(message):
    data = load_clean_data()
    uid = str(message.from_user.id)

    available = []

    for task_id, task in data["tasks"].items():
        if str(task["owner_id"]) == uid:
            continue

        if int(task.get("done", 0)) >= int(task.get("limit", 0)):
            continue

        if uid in [str(x) for x in task.get("done_users", [])]:
            continue

        if uid in [str(x) for x in task.get("pending_users", [])]:
            continue

        # если пользователь уже отправлял заявку или уже выполнил это задание — не показываем только ему
        already = False

        for req in data["requests"].values():
            if str(req["task_id"]) == str(task_id) and str(req["worker_id"]) == uid:
                already = True
                break

        completed_key = f"{task_id}:{uid}"
        if completed_key in data["completed"]:
            already = True

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
        f"📊 Рефов пришло: {task['done']}/{task['limit']}\n"
        f"🕒 Активно до: {task.get('expires_at', 'не указано')}\n\n"
        f"1️⃣ Нажмите «Перейти»\n"
        f"2️⃣ Выполните условия\n"
        f"3️⃣ Нажмите «Я выполнил»\n\n"
        f"Создатель задания проверит, пришёл ли реферал.",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("done:"))
def done_task(call):
    task_id = call.data.split(":")[1]
    data = load_clean_data()
    uid = str(call.from_user.id)

    if task_id not in data["tasks"]:
        bot.answer_callback_query(call.id, "Задание уже завершено.")
        return

    task = data["tasks"][task_id]

    if int(task.get("done", 0)) >= int(task.get("limit", 0)):
        finish_task_if_needed(data, task_id)
        save_data(data)
        bot.answer_callback_query(call.id, "Задание уже набрало нужных рефов.")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    if str(task["owner_id"]) == uid:
        bot.answer_callback_query(call.id, "Нельзя выполнять своё задание.")
        return

    completed_key = f"{task_id}:{uid}"
    if uid in [str(x) for x in task.get("done_users", [])] or uid in [str(x) for x in task.get("pending_users", [])]:
        bot.answer_callback_query(call.id, "Вы уже отправляли это задание.")
        return

    if completed_key in data["completed"]:
        bot.answer_callback_query(call.id, "Вы уже выполняли это задание.")
        return

    for req in data["requests"].values():
        if str(req["task_id"]) == str(task_id) and str(req["worker_id"]) == uid:
            bot.answer_callback_query(call.id, "Вы уже отправили заявку.")
            return

    data["last_request_id"] += 1
    request_id = str(data["last_request_id"])

    task.setdefault("pending_users", []).append(uid)

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

    worker_name = "@" + call.from_user.username if call.from_user.username else uid

    try:
        bot.send_message(
            int(task["owner_id"]),
            f"📩 <b>Новая заявка на проверку #{request_id}</b>\n\n"
            f"📋 Задание #{task_id}\n"
            f"👤 Пользователь: {worker_name}\n"
            f"🎁 Награда: {task['reward']} балл\n"
            f"📊 Сейчас рефов: {task['done']}/{task['limit']}\n\n"
            f"📝 Условие:\n{task['description']}\n\n"
            f"📎 Ссылка:\n{task['link']}\n\n"
            f"Проверьте, пришёл ли реферал.\n\n"
            f"👇 Нажмите кнопку <b>Одобрить/Отклонить</b>.\n"
            f"Если кнопки не нажимаются — напишите в чат:\n"
            f"<code>одобрить {request_id}</code>\n"
            f"или\n"
            f"<code>отклонить {request_id}</code>",
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

def process_request_decision(action, request_id, actor_id, chat_id=None, callback_call=None):
    """Обработка заявки через кнопку или текстом в чат.
    action: approve / reject
    request_id: номер заявки
    actor_id: кто принимает решение (должен быть создателем задания)
    """
    data = load_clean_data()
    request_id = str(request_id).strip()

    def answer(text):
        if callback_call:
            bot.answer_callback_query(callback_call.id, text)
        elif chat_id:
            bot.send_message(chat_id, text, reply_markup=main_kb())

    if request_id not in data["requests"]:
        answer("⚠️ Заявка уже обработана или не найдена.")
        if callback_call:
            try:
                bot.edit_message_reply_markup(callback_call.message.chat.id, callback_call.message.message_id, reply_markup=None)
            except Exception:
                pass
        return

    req = data["requests"][request_id]

    if str(req["owner_id"]) != str(actor_id):
        answer("⚠️ Это может подтвердить только создатель задания.")
        return

    task_id = str(req["task_id"])
    worker_id = str(req["worker_id"])
    owner_id = str(req["owner_id"])
    reward = int(req["reward"])

    if action == "reject":
        owner = get_user(data, owner_id)
        owner["balance"] += reward

        if task_id in data["tasks"]:
            data["tasks"][task_id]["pending_users"] = [
                str(x) for x in data["tasks"][task_id].get("pending_users", [])
                if str(x) != worker_id
            ]

        del data["requests"][request_id]
        save_data(data)

        try:
            bot.send_message(
                int(worker_id),
                "❌ <b>Задание отклонено.</b>\n\n"
                "Реферал не засчитался."
            )
        except Exception:
            pass

        text = (
            f"❌ <b>Заявка #{request_id} отклонена.</b>\n\n"
            f"💎 {reward} балл возвращён вам на баланс."
        )

        if callback_call:
            try:
                bot.edit_message_text(text, callback_call.message.chat.id, callback_call.message.message_id)
            except Exception:
                bot.send_message(callback_call.message.chat.id, text)
            bot.answer_callback_query(callback_call.id, "Отклонено.")
        else:
            bot.send_message(chat_id, text, reply_markup=main_kb())
        return

    # approve
    worker = get_user(data, worker_id)
    worker["balance"] += reward
    worker["completed_tasks"] += 1
    worker["earned"] += reward

    completed_key = f"{task_id}:{worker_id}"
    data["completed"][completed_key] = {
        "task_id": task_id,
        "worker_id": worker_id,
        "approved_at": now()
    }

    if task_id in data["tasks"]:
        task = data["tasks"][task_id]
        task["pending_users"] = [str(x) for x in task.get("pending_users", []) if str(x) != worker_id]
        if worker_id not in [str(x) for x in task.get("done_users", [])]:
            task.setdefault("done_users", []).append(worker_id)
            task["done"] = int(task.get("done", 0)) + 1

        done = int(task.get("done", 0))
        limit = int(task.get("limit", 0))
        task_done_text = f"{done}/{limit}"
        task_finished = finish_task_if_needed(data, task_id)
    else:
        task_done_text = "завершено"
        task_finished = True

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

    if task_finished:
        text = (
            f"✅ <b>Заявка #{request_id} одобрена.</b>\n\n"
            f"🎁 Награда начислена пользователю.\n"
            f"📊 Рефов пришло: {task_done_text}\n\n"
            f"🏁 Лимит выполнен, задание удалено."
        )
    else:
        text = (
            f"✅ <b>Заявка #{request_id} одобрена.</b>\n\n"
            f"🎁 Награда начислена пользователю.\n"
            f"📊 Рефов пришло: {task_done_text}"
        )

    if callback_call:
        try:
            bot.edit_message_text(text, callback_call.message.chat.id, callback_call.message.message_id)
        except Exception:
            bot.send_message(callback_call.message.chat.id, text)
        bot.answer_callback_query(callback_call.id, "Одобрено.")
    else:
        bot.send_message(chat_id, text, reply_markup=main_kb())

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve:") or call.data.startswith("reject:"))
def approve_reject(call):
    action, request_id = call.data.split(":")
    process_request_decision(action, request_id, call.from_user.id, callback_call=call)

@bot.message_handler(func=lambda m: bool(m.text) and re.match(r"^\s*(одобрить|отклонить|approve|reject)\s+\d+\s*$", m.text.lower()))
def approve_reject_by_text(message):
    parts = message.text.lower().strip().split()
    word = parts[0]
    request_id = parts[1]

    action = "approve" if word in ("одобрить", "approve") else "reject"
    process_request_decision(action, request_id, message.from_user.id, chat_id=message.chat.id)

@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def profile(message):
    data = load_clean_data()
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

@bot.message_handler(func=lambda m: m.text == "📊 Мои задания")
def my_tasks(message):
    data = load_clean_data()
    uid = str(message.from_user.id)

    my = []
    for task_id, task in data["tasks"].items():
        if str(task["owner_id"]) == uid:
            my.append(task)

    if not my:
        bot.send_message(message.chat.id, "📭 У вас нет активных заданий.", reply_markup=main_kb())
        return

    text = "📊 <b>Ваши активные задания</b>\n\n"

    for task in my:
        text += (
            f"📋 <b>Задание #{task['id']}</b>\n"
            f"📝 {task['description']}\n"
            f"🎁 Награда: {task['reward']} балл\n"
            f"📊 Рефов пришло: {task['done']}/{task['limit']}\n"
            f"💎 Резерв на невыполненные: {max(0, (int(task['limit']) - int(task['done'])) * int(task['reward']))}\n"
            f"🕒 Активно до: {task.get('expires_at', 'не указано')}\n\n"
        )

    bot.send_message(message.chat.id, text, reply_markup=main_kb())

@bot.message_handler(commands=["stats"])
def stats(message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return

    data = load_clean_data()

    bot.send_message(
        message.chat.id,
        f"📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: {len(data['users'])}\n"
        f"📋 Активных заданий: {len(data['tasks'])}\n"
        f"⏳ Заявок на проверке: {len(data['requests'])}\n"
        f"✅ Выполнений в истории: {len(data['completed'])}\n"
        f"🗂 Архив заданий: {len(data.get('archived_tasks', {}))}"
    )

@bot.message_handler(commands=["delete_task"])
def delete_task(message):
    if ADMIN_ID != 0 and message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.send_message(message.chat.id, "Использование:\n/delete_task ID")
        return

    task_id = parts[1]
    data = load_clean_data()

    if task_id not in data["tasks"]:
        bot.send_message(message.chat.id, "⚠️ Задание не найдено.")
        return

    task = data["tasks"][task_id]
    owner = get_user(data, task["owner_id"])

    left = int(task["limit"]) - int(task["done"])
    refund = max(0, left * int(task["reward"]))
    owner["balance"] += refund

    del data["tasks"][task_id]

    to_delete = []
    for req_id, req in data["requests"].items():
        if str(req["task_id"]) == str(task_id):
            to_delete.append(req_id)

    for req_id in to_delete:
        del data["requests"][req_id]

    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ Задание #{task_id} удалено.\n"
        f"💎 Возврат владельцу: {refund}"
    )

def cleanup_loop():
    while True:
        try:
            load_clean_data()
        except Exception as e:
            print("CLEANUP ERROR:", e)
        time.sleep(3600)

if __name__ == "__main__":
    threading.Thread(target=run_site, daemon=True).start()
    threading.Thread(target=keep_ping, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    print("BOT STARTED ✅")

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60)
        except Exception as e:
            print("BOT ERROR:", e)
            time.sleep(5)
