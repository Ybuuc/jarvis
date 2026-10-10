"""Tests for the Study Assistant backend. Standard library only (unittest).

Run from anywhere:
    python3 -I -B tests/test_app.py -v

No network, no real Telegram/Azure credentials: everything external is stubbed
and each test uses its own temporary SQLite file.
"""
import importlib.util
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
# APP_UNDER_TEST lets the same tests be pointed at another copy of app.py.
APP_PATH = Path(os.environ.get("APP_UNDER_TEST", PROJECT_DIR / "app.py"))
AUTH = {"Authorization": "Bearer test-token"}


class StopLoop(BaseException):
    """Raised to break out of the infinite worker loops (escapes `except Exception`)."""


def load_app(env=None):
    base_env = {
        "APP_ACCESS_TOKEN": "test-token",
        "APP_DB_PATH": os.path.join(tempfile.mkdtemp(prefix="sa_test_"), "db.sqlite3"),
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_OWNER_ID": "",
        "AZURE_OPENAI_ENDPOINT": "",
        "AZURE_OPENAI_KEY": "",
        "AZURE_OPENAI_DEPLOYMENT": "",
        "LLM_BASE_URL": "",
        "LLM_API_KEY": "",
        "LLM_MODEL": "",
        "AI_TEMPERATURE": "0.4",
        "APP_TIMEZONE": "Asia/Qyzylorda",
    }
    base_env.update(env or {})
    with mock.patch.dict(os.environ, base_env):
        spec = importlib.util.spec_from_file_location("app_under_test", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def run_reminder_once(module):
    def stop(_seconds):
        raise StopLoop()

    with mock.patch.object(module.time, "sleep", stop):
        try:
            module.reminder_loop()
        except StopLoop:
            pass


class StaticAndApiTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_app()
        self.client = self.mod.app.test_client()

    def test_every_asset_referenced_by_index_html_is_served(self):
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        html = index.get_data(as_text=True)
        paths = re.findall(r'(?:src|href)="(/[^"]*)"', html)
        self.assertIn("/style.css", paths)
        self.assertIn("/app.js", paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_service_worker_precache_list_is_all_served(self):
        sw = (PROJECT_DIR / "static" / "sw.js").read_text(encoding="utf-8")
        shell = re.search(r"SHELL=\[(.*?)\]", sw).group(1)
        for path in re.findall(r"'(/[^']*)'", shell):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_api_requires_token(self):
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        self.assertEqual(self.client.get("/api/tasks", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/api/tasks", headers=AUTH).status_code, 200)

    def test_task_lifecycle_through_api(self):
        created = self.client.post("/api/tasks", json={"title": "Лабораторная"}, headers=AUTH)
        self.assertEqual(created.status_code, 201)
        task = created.get_json()
        self.assertEqual(task["status"], "new")
        patched = self.client.patch(f"/api/tasks/{task['id']}", json={"status": "review"}, headers=AUTH)
        self.assertEqual(patched.get_json()["status"], "review")
        listed = self.client.get("/api/tasks", headers=AUTH).get_json()
        self.assertEqual([t["status"] for t in listed], ["review"])
        self.assertEqual(self.client.patch("/api/tasks/9999", json={"status": "done"}, headers=AUTH).status_code, 404)

    def test_health_is_public_and_reports_unconfigured(self):
        data = self.client.get("/api/health").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["ai_configured"])
        self.assertFalse(data["telegram_configured"])


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_app()
        self.sent = []
        self.mod.telegram_send = self.sent.append

    def reminded(self):
        return {t["title"]: t["reminded_at"] is not None for t in self.mod.list_tasks()}

    def test_naive_and_aware_times_are_both_handled(self):
        self.mod.create_task("naive", remind_at="2020-01-01T10:00:00")
        self.mod.create_task("utc", remind_at="2020-01-01T10:00:00Z")
        self.mod.create_task("offset", remind_at="2020-01-01T10:00:00+05:00")
        run_reminder_once(self.mod)
        self.assertEqual(self.reminded(), {"naive": True, "utc": True, "offset": True})
        self.assertEqual(len(self.sent), 3)

    def test_future_reminder_is_not_sent(self):
        self.mod.create_task("later", remind_at="2999-01-01T00:00:00Z")
        run_reminder_once(self.mod)
        self.assertEqual(self.sent, [])

    def test_each_reminder_is_sent_only_once(self):
        self.mod.create_task("once", remind_at="2020-01-01T10:00:00Z")
        run_reminder_once(self.mod)
        run_reminder_once(self.mod)
        run_reminder_once(self.mod)
        self.assertEqual(len(self.sent), 1)

    def test_started_task_gets_no_reminder(self):
        task = self.mod.create_task("started", remind_at="2020-01-01T10:00:00Z")
        self.mod.update_task(task["id"], status="active")
        run_reminder_once(self.mod)
        self.assertEqual(self.sent, [])

    def test_unparsable_time_does_not_block_other_reminders(self):
        self.mod.create_task("garbage", remind_at="not-a-date")
        self.mod.create_task("good", remind_at="2020-01-01T10:00:00Z")
        run_reminder_once(self.mod)
        self.assertEqual(self.reminded(), {"garbage": False, "good": True})

    def test_send_failure_does_not_block_others_and_is_retried(self):
        self.mod.create_task("fails", remind_at="2020-01-01T10:00:00Z")
        self.mod.create_task("works", remind_at="2020-01-01T10:00:00Z")

        def flaky(text):
            if "fails" in text:
                raise RuntimeError("telegram down")
            self.sent.append(text)

        self.mod.telegram_send = flaky
        run_reminder_once(self.mod)
        self.assertEqual(self.reminded(), {"fails": False, "works": True})
        # next cycle: Telegram is back, the failed one is delivered exactly once
        self.mod.telegram_send = self.sent.append
        run_reminder_once(self.mod)
        run_reminder_once(self.mod)
        self.assertEqual(self.reminded(), {"fails": True, "works": True})
        self.assertEqual(sum("fails" in s for s in self.sent), 1)


class WorkerStartupTests(unittest.TestCase):
    def test_workers_start_at_import_when_telegram_is_configured(self):
        with mock.patch.object(threading, "Thread") as fake_thread:
            mod = load_app({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_OWNER_ID": "1"})
            self.assertEqual(fake_thread.call_count, 2)
            targets = {call.kwargs["target"].__name__ for call in fake_thread.call_args_list}
            self.assertEqual(targets, {"telegram_poll_loop", "reminder_loop"})
            mod.start_workers()
            mod.app.test_client().get("/api/health")
            self.assertEqual(fake_thread.call_count, 2, "workers must not start twice")

    def test_no_workers_without_telegram_configuration(self):
        with mock.patch.object(threading, "Thread") as fake_thread:
            mod = load_app()
            mod.app.test_client().get("/api/health")
            self.assertEqual(fake_thread.call_count, 0)


class TelegramCommandTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_app()

    def test_start_and_done_commands(self):
        task = self.mod.create_task("Эссе")
        tid = task["id"]
        self.assertIn("в работе", self.mod.handle_status_command(f"начал #{tid}"))
        self.assertEqual(self.mod.list_tasks()[0]["status"], "active")
        self.assertIn("готово", self.mod.handle_status_command(f"Готово #{tid}"))
        self.assertEqual(self.mod.list_tasks()[0]["status"], "done")

    def test_accepts_spelling_variants(self):
        for text in ("начал #1", "Начал #1", "начала 1", "  готово #1. ", "сделал #1!"):
            with self.subTest(text=text):
                self.assertIsNotNone(self.mod.STATUS_COMMAND.match(text))

    def test_finished_task_is_not_reopened_by_start(self):
        tid = self.mod.create_task("Done already")["id"]
        self.mod.update_task(tid, status="done")
        reply = self.mod.handle_status_command(f"начал #{tid}")
        self.assertIn("уже завершена", reply)
        self.assertEqual(self.mod.list_tasks()[0]["status"], "done")

    def test_unknown_task_and_non_commands(self):
        self.assertIn("не найдена", self.mod.handle_status_command("начал #999"))
        for text in ("привет", "начал", "помоги с #3", "начал работу над #3"):
            with self.subTest(text=text):
                self.assertIsNone(self.mod.handle_status_command(text))

    def test_poll_loop_handles_commands_without_ai_and_ignores_strangers(self):
        mod = self.mod
        mod.TELEGRAM_OWNER_ID = "42"
        tid = mod.create_task("Задача")["id"]
        sent, ai_calls = [], []
        mod.telegram_send = sent.append
        mod.ai_chat = lambda text: ai_calls.append(text) or "ответ ИИ"
        updates = iter([{"ok": True, "result": [
            {"update_id": 1, "message": {"from": {"id": 7}, "text": f"готово #{tid}"}},
            {"update_id": 2, "message": {"from": {"id": 42}, "text": f"начал #{tid}"}},
        ]}])

        def fake_call(method, payload=None, timeout=35):
            try:
                return next(updates)
            except StopIteration:
                raise StopLoop()

        mod.telegram_call = fake_call
        try:
            mod.telegram_poll_loop()
        except StopLoop:
            pass
        self.assertEqual(ai_calls, [], "status commands must not reach the AI")
        self.assertEqual(mod.list_tasks()[0]["status"], "active", "stranger's message must be ignored")
        self.assertEqual(len(sent), 1)
        self.assertIn("в работе", sent[0])


class FakeResponse:
    def __init__(self, message):
        self._message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": self._message}]}


def tool_call_message(name, arguments, call_id="call_1"):
    call = {"type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
    if call_id is not None:
        call["id"] = call_id
    return {"role": "assistant", "content": None, "tool_calls": [call]}


LLM_ENV = {"LLM_BASE_URL": "https://example.test/v1/", "LLM_API_KEY": "secret-key", "LLM_MODEL": "some-model"}
AZURE_ENV = {"AZURE_OPENAI_ENDPOINT": "https://az.test/", "AZURE_OPENAI_KEY": "az-key", "AZURE_OPENAI_DEPLOYMENT": "dep"}


class AiProviderTests(unittest.TestCase):
    def run_chat(self, env, responses):
        mod = load_app(env)
        with mock.patch.object(mod.requests, "post", side_effect=[FakeResponse(r) for r in responses]) as post:
            answer = mod.ai_chat("Запиши задачу")
        return mod, post, answer

    def test_not_configured(self):
        mod = load_app()
        self.assertIn("ИИ пока не подключён", mod.ai_chat("привет"))
        health = mod.app.test_client().get("/api/health").get_json()
        self.assertFalse(health["ai_configured"])
        self.assertIsNone(health["ai_provider"])

    def test_openai_compatible_provider_request_and_tool_loop(self):
        mod, post, answer = self.run_chat(LLM_ENV, [
            tool_call_message("create_task", {"title": "Сдать лабораторную"}),
            {"role": "assistant", "content": "Записал."},
        ])
        self.assertEqual(answer, "Записал.")
        self.assertEqual([t["title"] for t in mod.list_tasks()], ["Сдать лабораторную"])
        first = post.call_args_list[0]
        self.assertEqual(first.args[0], "https://example.test/v1/chat/completions")
        self.assertEqual(first.kwargs["headers"]["Authorization"], "Bearer secret-key")
        self.assertNotIn("api-key", first.kwargs["headers"])
        self.assertEqual(first.kwargs["json"]["model"], "some-model")
        self.assertEqual(first.kwargs["json"]["temperature"], 0.4)
        self.assertTrue(first.kwargs["json"]["tools"])
        health = mod.app.test_client().get("/api/health").get_json()
        self.assertEqual((health["ai_configured"], health["ai_provider"]), (True, "openai-compatible"))

    def test_azure_request_is_unchanged(self):
        mod, post, answer = self.run_chat(AZURE_ENV, [{"role": "assistant", "content": "Привет"}])
        self.assertEqual(answer, "Привет")
        call = post.call_args_list[0]
        self.assertEqual(call.args[0], "https://az.test/openai/deployments/dep/chat/completions?api-version=2024-10-21")
        self.assertEqual(call.kwargs["headers"]["api-key"], "az-key")
        self.assertNotIn("model", call.kwargs["json"])
        self.assertEqual(mod.app.test_client().get("/api/health").get_json()["ai_provider"], "azure")

    def test_generic_provider_takes_priority_over_azure(self):
        _mod, post, _answer = self.run_chat({**AZURE_ENV, **LLM_ENV}, [{"role": "assistant", "content": "ok"}])
        self.assertTrue(post.call_args_list[0].args[0].startswith("https://example.test/v1/"))

    def test_incomplete_generic_settings_fall_back_to_azure(self):
        env = {**AZURE_ENV, "LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "k"}  # no LLM_MODEL
        _mod, post, _answer = self.run_chat(env, [{"role": "assistant", "content": "ok"}])
        self.assertTrue(post.call_args_list[0].args[0].startswith("https://az.test/"))

    def test_temperature_is_configurable_and_can_be_omitted(self):
        _m, post, _a = self.run_chat({**LLM_ENV, "AI_TEMPERATURE": ""}, [{"role": "assistant", "content": "ok"}])
        self.assertNotIn("temperature", post.call_args_list[0].kwargs["json"])
        _m, post, _a = self.run_chat({**LLM_ENV, "AI_TEMPERATURE": "1"}, [{"role": "assistant", "content": "ok"}])
        self.assertEqual(post.call_args_list[0].kwargs["json"]["temperature"], 1.0)

    def test_tool_call_without_id_does_not_crash(self):
        mod, post, answer = self.run_chat(LLM_ENV, [
            tool_call_message("create_task", {"title": "Без id"}, call_id=None),
            {"role": "assistant", "content": "Готово"},
        ])
        self.assertEqual(answer, "Готово")
        tool_messages = [m for m in post.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "")


if __name__ == "__main__":
    unittest.main()
