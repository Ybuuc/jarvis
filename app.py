import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DB_PATH = os.getenv("APP_DB_PATH", str(APP_DIR / "data" / "assistant.sqlite3"))
ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Qyzylorda")

app = Flask(__name__, static_folder=None)
_worker_lock = threading.Lock()
_workers_started = False


def db():
    import sqlite3
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT 'Личное',
          status TEXT NOT NULL DEFAULT 'new',
          deadline TEXT,
          remind_at TEXT,
          reminded_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_dict(row):
    return dict(row)


def list_tasks(status=None):
    with db() as con:
        if status:
            rows = con.execute("SELECT * FROM tasks WHERE status=? ORDER BY COALESCE(deadline, '9999')", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tasks ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'active' THEN 1 WHEN 'waiting' THEN 2 WHEN 'review' THEN 3 ELSE 4 END, COALESCE(deadline, '9999')").fetchall()
    return [task_dict(row) for row in rows]


def create_task(title, detail="", category="Личное", deadline=None, remind_at=None):
    stamp = now_iso()
    with db() as con:
        cur = con.execute("INSERT INTO tasks(title, detail, category, status, deadline, remind_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                          (title.strip(), detail.strip(), category.strip() or "Личное", "new", deadline or None, remind_at or None, stamp, stamp))
        row = con.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone()
    return task_dict(row)


def update_task(task_id, status=None, deadline=None, remind_at=None, detail=None):
    allowed = {"new", "active", "waiting", "review", "done"}
    with db() as con:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not row:
            return None
        values = dict(row)
        if status in allowed:
            values["status"] = status
        if deadline is not None:
            values["deadline"] = deadline or None
        if remind_at is not None:
            values["remind_at"] = remind_at or None
            values["reminded_at"] = None
        if detail is not None:
            values["detail"] = detail
        values["updated_at"] = now_iso()
        con.execute("UPDATE tasks SET status=?, deadline=?, remind_at=?, reminded_at=?, detail=?, updated_at=? WHERE id=?",
                    (values["status"], values["deadline"], values["remind_at"], values["reminded_at"], values["detail"], values["updated_at"], int(task_id)))
        row = con.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    return task_dict(row)


def auth_ok():
    if not ACCESS_TOKEN:
        return False
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return hmac.compare_digest(supplied.encode(), ACCESS_TOKEN.encode())


@app.before_request
def ensure_workers():
    global _workers_started
    if _workers_started or not (TELEGRAM_TOKEN and TELEGRAM_OWNER_ID):
        return
    with _worker_lock:
        if not _workers_started:
            threading.Thread(target=telegram_poll_loop, daemon=True, name="telegram-poller").start()
            threading.Thread(target=reminder_loop, daemon=True, name="task-reminders").start()
            _workers_started = True


@app.get("/")
def home():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC_DIR, "manifest.webmanifest")


@app.get("/sw.js")
def service_worker():
    return send_from_directory(STATIC_DIR, "sw.js", mimetype="application/javascript")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "ai_configured": bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY and AZURE_OPENAI_DEPLOYMENT), "telegram_configured": bool(TELEGRAM_TOKEN and TELEGRAM_OWNER_ID)})


@app.route("/api/<path:_path>", methods=["GET", "POST", "PATCH", "DELETE"])
def api_not_found(_path):
    return jsonify({"error": "Not found"}), 404


@app.get("/api/tasks")
def api_tasks():
    if not auth_ok():
        return jsonify({"error": "Нужен код доступа"}), 401
    return jsonify(list_tasks())


@app.post("/api/tasks")
def api_create_task():
    if not auth_ok():
        return jsonify({"error": "Нужен код доступа"}), 401
    data = request.get_json(force=True)
    title = str(data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "Укажи название задачи"}), 400
    task = create_task(title, str(data.get("detail", "")), str(data.get("category", "Личное")), data.get("deadline"), data.get("remind_at"))
    return jsonify(task), 201


@app.patch("/api/tasks/<int:task_id>")
def api_update_task(task_id):
    if not auth_ok():
        return jsonify({"error": "Нужен код доступа"}), 401
    data = request.get_json(force=True)
    task = update_task(task_id, data.get("status"), data.get("deadline"), data.get("remind_at"), data.get("detail"))
    if not task:
        return jsonify({"error": "Задача не найдена"}), 404
    return jsonify(task)


@app.get("/api/messages")
def api_messages():
    if not auth_ok():
        return jsonify({"error": "Нужен код доступа"}), 401
    with db() as con:
        rows = con.execute("SELECT role, content, created_at FROM messages ORDER BY id DESC LIMIT 80").fetchall()
    return jsonify([dict(row) for row in reversed(rows)])


def add_message(role, content):
    with db() as con:
        con.execute("INSERT INTO messages(role, content, created_at) VALUES(?,?,?)", (role, content, now_iso()))


def ai_chat(user_text):
    if not (AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY and AZURE_OPENAI_DEPLOYMENT):
        return "ИИ пока не подключён. Добавь Azure OpenAI endpoint, deployment и key в настройки приложения. Задачи при этом можно записывать на доску вручную."
    with db() as con:
        history = con.execute("SELECT role, content FROM messages ORDER BY id DESC LIMIT 12").fetchall()
    local_now = datetime.now(ZoneInfo(APP_TIMEZONE)).isoformat(timespec="minutes")
    system_prompt = (f"Ты личный помощник студента. Отвечай по-русски или по-казахски, как пишет пользователь. "
                     f"Помогай разбирать учебные и личные задачи пошагово. Не утверждай, что выполнил работу в фоне, если этого не делал. "
                     f"Локальные дата и время пользователя: {local_now} ({APP_TIMEZONE}). "
                     "Если записываешь срок или напоминание из естественной речи, преобразуй его в ISO 8601 с часовым смещением пользователя. "
                     "Новые задачи можно создать на доске через инструмент create_task. Начатой считается только задача со статусом active; напоминания о старте относятся только к статусу new.")
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": row["role"], "content": row["content"]} for row in reversed(history)]
    tools = [{"type": "function", "function": {"name": "create_task", "description": "Создать карточку новой задачи, если пользователь просит записать или сохранить задачу.", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "detail": {"type": "string"}, "category": {"type": "string"}, "deadline": {"type": "string", "description": "ISO date/time или пустая строка"}, "remind_at": {"type": "string", "description": "ISO date/time или пустая строка"}}, "required": ["title"]}}},
             {"type": "function", "function": {"name": "list_tasks", "description": "Показать задачи пользователя.", "parameters": {"type": "object", "properties": {"status": {"type": "string", "enum": ["new", "active", "waiting", "review", "done", ""]}}}}},
             {"type": "function", "function": {"name": "update_task", "description": "Поменять статус задачи. Используй active только если пользователь явно начал работу.", "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["new", "active", "waiting", "review", "done"]}}, "required": ["task_id", "status"]}}}]
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
    headers = {"api-key": AZURE_OPENAI_KEY, "Content-Type": "application/json"}
    for _ in range(3):
        response = requests.post(url, headers=headers, json={"messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.4}, timeout=45)
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]
        calls = result.get("tool_calls") or []
        if not calls:
            return result.get("content") or "Готово."
        messages.append(result)
        for call in calls:
            name = call["function"]["name"]
            args = json.loads(call["function"].get("arguments") or "{}")
            if name == "create_task":
                output = create_task(args.get("title", "Новая задача"), args.get("detail", ""), args.get("category", "Личное"), args.get("deadline"), args.get("remind_at"))
            elif name == "list_tasks":
                output = list_tasks(args.get("status") or None)
            elif name == "update_task":
                output = update_task(args.get("task_id"), args.get("status")) or {"error": "Задача не найдена"}
            else:
                output = {"error": "Неизвестная команда"}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output, ensure_ascii=False)})
    return "Выполнил действие. Что ещё сделать?"


@app.post("/api/chat")
def api_chat():
    if not auth_ok():
        return jsonify({"error": "Нужен код доступа"}), 401
    data = request.get_json(force=True)
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "Пустое сообщение"}), 400
    add_message("user", text)
    try:
        answer = ai_chat(text)
    except Exception as exc:
        app.logger.exception("AI request failed")
        answer = "Не удалось связаться с ИИ. Проверь Azure OpenAI settings и попробуй ещё раз."
    add_message("assistant", answer)
    return jsonify({"answer": answer})


def telegram_call(method, payload=None, timeout=35):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def telegram_send(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_OWNER_ID):
        return
    telegram_call("sendMessage", {"chat_id": TELEGRAM_OWNER_ID, "text": text[:4000]})


def telegram_poll_loop():
    offset = 0
    while True:
        try:
            data = telegram_call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]}, timeout=35)
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                sender = str((message.get("from") or {}).get("id", ""))
                text = (message.get("text") or "").strip()
                if sender != str(TELEGRAM_OWNER_ID) or not text:
                    continue
                if text == "/start":
                    telegram_send("Я твой помощник. Напиши задачу обычным сообщением или спроси, чем я могу помочь. Команда /tasks покажет открытые задачи.")
                elif text == "/tasks":
                    open_items = [x for x in list_tasks() if x["status"] != "done"]
                    telegram_send("Открытых задач пока нет." if not open_items else "Твои задачи:\n" + "\n".join(f"#{x['id']} · {x['title']} — {x['status']}" for x in open_items))
                else:
                    add_message("user", text)
                    try:
                        answer = ai_chat(text)
                    except Exception:
                        app.logger.exception("Telegram AI request failed")
                        answer = "Не удалось связаться с ИИ. Попробуй позже."
                    add_message("assistant", answer)
                    telegram_send(answer)
        except Exception:
            app.logger.exception("Telegram poll failed")
            time.sleep(5)


def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            with db() as con:
                due = con.execute("SELECT * FROM tasks WHERE status='new' AND remind_at IS NOT NULL AND reminded_at IS NULL").fetchall()
            for row in due:
                try:
                    when = datetime.fromisoformat(row["remind_at"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when <= now:
                    telegram_send(f"Напоминание: задача «{row['title']}» ещё не начата.\nСрок: {row['deadline'] or 'не задан'}\nНапиши «начал #{row['id']}», «готово #{row['id']}» или отложи напоминание на доске.")
                    with db() as con:
                        con.execute("UPDATE tasks SET reminded_at=?, updated_at=? WHERE id=? AND status='new' AND reminded_at IS NULL", (now_iso(), now_iso(), row["id"]))
        except Exception:
            app.logger.exception("Reminder check failed")
        time.sleep(30)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
