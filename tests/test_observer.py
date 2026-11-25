# -*- coding: utf-8 -*-
"""Тесты наблюдателя «Agent Office». Только unittest, синтетические данные, без сети и subprocess.

Запуск из корня репозитория:
    python -m unittest discover -s tests
или:
    python tests/test_observer.py
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
OBSERVER_PATH = HERE / "observer.py"

_spec = importlib.util.spec_from_file_location("agent_office_observer", OBSERVER_PATH)
obs = importlib.util.module_from_spec(_spec)
sys.modules["agent_office_observer"] = obs
_spec.loader.exec_module(obs)

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
SECRET_MARKERS = (
    "SECRET_TOKEN_abc123",
    "sk-TESTSECRET999",
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "C:\\Users\\victim\\passwords.txt",
    "evil<tool>",
    "session-uuid-777",
)


def iso(delta_seconds):
    return (NOW + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def codex_line(delta, top, payload):
    return json.dumps({"timestamp": iso(delta), "type": top, "payload": payload}, ensure_ascii=False)


def claude_line(delta, top, **extra):
    data = {"timestamp": iso(delta), "type": top}
    data.update(extra)
    return json.dumps(data, ensure_ascii=False)


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state_dir = self.root / "tasks" / "sessions" / "state"
        self.state_dir.mkdir(parents=True)
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def write(self, name, lines, directory=None):
        path = (directory or self.logs) / name
        text = "\n".join(lines)
        path.write_text(text + ("\n" if lines else ""), encoding="utf-8")
        return path

    def agent(self, snapshot, agent_id):
        for item in snapshot["agents"]:
            if item["id"] == agent_id:
                return item
        self.fail("агент не найден: " + agent_id)


class TimestampTests(unittest.TestCase):
    def test_zones_are_normalized_to_utc(self):
        expected = NOW
        for value in ("2026-11-25T12:00:00Z", "2026-11-25T15:00:00+03:00",
                      "2026-11-25T05:00:00-07:00", "2026-11-25T12:00:00",
                      "2026-11-25T12:00:00.1234567Z", "2026-11-25 15:00:00+0300"):
            parsed = obs.parse_timestamp(value, NOW)
            self.assertIsNotNone(parsed, value)
            self.assertEqual(parsed.replace(microsecond=0), expected, value)

    def test_epoch_seconds_and_millis(self):
        seconds = NOW.timestamp()
        self.assertEqual(obs.parse_timestamp(seconds, NOW), NOW)
        self.assertEqual(obs.parse_timestamp(seconds * 1000, NOW), NOW)

    def test_garbage_and_future_rejected(self):
        for value in (None, True, "", "garbage", "2026-13-40T99:00:00Z", ["x"], {"a": 1},
                      "2026-11-25T12:00:00+99:00", "9" * 100):
            self.assertIsNone(obs.parse_timestamp(value, NOW), repr(value))
        future = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(obs.parse_timestamp(future, NOW))


class SnapshotSafetyTests(TempCase):
    def test_dangerous_fields_never_reach_snapshot(self):
        codex = self.write("codex.jsonl", [
            codex_line(-30, "response_item", {
                "type": "function_call", "name": "evil<tool>",
                "arguments": json.dumps({"cmd": "curl http://x/ SECRET_TOKEN_abc123"}),
                "call_id": "session-uuid-777"}),
            codex_line(-20, "response_item", {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "<script>alert(1)</script> sk-TESTSECRET999"}]}),
            codex_line(-10, "response_item", {
                "type": "function_call_output", "output": "C:\\Users\\victim\\passwords.txt"}),
        ])
        claude = self.write("claude.jsonl", [
            claude_line(-15, "assistant", message={"content": [
                {"type": "text", "text": "<img src=x onerror=alert(1)>"},
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "C:\\Users\\victim\\passwords.txt", "new_string": "SECRET_TOKEN_abc123"}}]}),
            claude_line(-5, "user", message={"content": [
                {"type": "tool_result", "content": "sk-TESTSECRET999"}]}),
        ])
        cli = self.write("cli.json", [json.dumps({
            "agent_id": "claude-cli", "task": "C:\\Users\\victim\\passwords.txt",
            "status": "working", "event_at": iso(-3), "tool": "evil<tool>",
            "session_id": "session-uuid-777"})])
        snapshot = obs.build_snapshot(codex, claude, cli, NOW)
        dumped = json.dumps(snapshot, ensure_ascii=False)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, dumped)
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["observer"]["mode"], "read_only")
        self.assertEqual({a["id"] for a in snapshot["agents"]}, {"codex", "claude-cli", "claude-vscode"})
        # неизвестный инструмент даёт только общую свежую активность
        self.assertEqual(self.agent(snapshot, "claude-cli")["action"], "Свежая активность")
        self.assertEqual(self.agent(snapshot, "claude-cli")["task"], "Поручение Codex")

    def test_names_and_roles_are_set_by_code(self):
        codex = self.write("codex.jsonl", [json.dumps({
            "timestamp": iso(-5), "type": "response_item", "name": "<b>Hacker</b>", "role": "<i>root</i>",
            "payload": {"type": "function_call", "name": "exec", "role": "<i>root</i>"}})])
        snapshot = obs.build_snapshot(codex, None, None, NOW)
        agent = self.agent(snapshot, "codex")
        self.assertEqual(agent["name"], "Codex")
        self.assertEqual(agent["role"], "Руководитель сессии")
        self.assertNotIn("Hacker", json.dumps(snapshot, ensure_ascii=False))

    def test_sources_are_not_modified(self):
        codex = self.write("codex.jsonl", [codex_line(-5, "response_item", {"type": "function_call", "name": "exec"})])
        before = hashlib.sha256(codex.read_bytes()).hexdigest()
        obs.build_snapshot(codex, None, None, NOW)
        self.assertEqual(before, hashlib.sha256(codex.read_bytes()).hexdigest())


class LogReadingTests(TempCase):
    def test_partial_last_line_is_ignored(self):
        good = codex_line(-5, "response_item", {"type": "function_call", "name": "apply_patch"})
        path = self.logs / "codex.jsonl"
        path.write_text(good + "\n" + '{"timestamp":"' + iso(-1) + '","type":"response_item","payload":{"type":"function_c',
                        encoding="utf-8")
        snapshot = obs.build_snapshot(path, None, None, NOW)
        agent = self.agent(snapshot, "codex")
        self.assertEqual(agent["status"], "editing")
        self.assertEqual(agent["action"], "Правит файл")
        self.assertEqual(agent["event_at"], iso(-5))

    def test_tail_is_size_limited_and_first_line_dropped(self):
        line = codex_line(-1000, "response_item", {"type": "function_call", "name": "exec"})
        count = (obs.TAIL_BYTES * 3) // (len(line) + 1)
        lines = [line] * count + [codex_line(-4, "response_item", {"type": "function_call", "name": "read_file"})]
        path = self.write("codex.jsonl", lines)
        self.assertGreater(path.stat().st_size, obs.TAIL_BYTES * 2)
        tail = obs.read_tail(path)
        self.assertLessEqual(sum(len(item) + 1 for item in tail), obs.TAIL_BYTES + 1)
        for raw in tail:
            if raw.strip():
                json.loads(raw.decode("utf-8"))  # ни одной обрезанной строки
        snapshot = obs.build_snapshot(path, None, None, NOW)
        self.assertEqual(self.agent(snapshot, "codex")["status"], "reading")

    def test_missing_and_invalid_sources(self):
        missing = self.logs / "nope.jsonl"
        directory = self.logs / "dir.jsonl"
        directory.mkdir()
        wrong_suffix = self.write("notes.txt", [codex_line(-1, "response_item", {"type": "function_call", "name": "exec"})])
        garbage = self.logs / "garbage.jsonl"
        garbage.write_bytes(b"\xff\xfe\x00\x01{{{{\nnot json\n[1,2,3]\n\"x\"\n")
        self.assertEqual(obs.source_state(missing, ".jsonl"), "missing")
        self.assertEqual(obs.source_state(directory, ".jsonl"), "unreadable")
        self.assertEqual(obs.source_state(wrong_suffix, ".jsonl"), "unreadable")
        self.assertEqual(obs.source_state(None, ".jsonl"), "not_configured")
        self.assertEqual(obs.source_state("bad\x00path.jsonl", ".jsonl"), "unreadable")
        for path in (missing, directory, wrong_suffix, garbage):
            snapshot = obs.build_snapshot(path, None, None, NOW)
            agent = self.agent(snapshot, "codex")
            self.assertEqual(agent["status"], "unknown")
            self.assertEqual(agent["action"], "Нет данных")
            self.assertIsNone(agent["event_at"])

    def test_one_failed_source_does_not_hide_others(self):
        claude = self.write("claude.jsonl", [claude_line(-5, "assistant", message={"content": [
            {"type": "tool_use", "name": "Read", "input": {}}]})])
        snapshot = obs.build_snapshot(self.logs / "missing.jsonl", claude, None, NOW)
        self.assertEqual(self.agent(snapshot, "codex")["status"], "unknown")
        self.assertEqual(self.agent(snapshot, "claude-vscode")["status"], "reading")
        self.assertEqual(snapshot["observer"]["sources"]["codex_log"], "missing")
        self.assertEqual(snapshot["observer"]["sources"]["claude_log"], "ok")

    def test_stale_event_is_not_reported_as_active(self):
        old = self.write("codex.jsonl", [codex_line(-200, "response_item", {"type": "function_call", "name": "apply_patch"})])
        agent = self.agent(obs.build_snapshot(old, None, None, NOW), "codex")
        self.assertEqual(agent["status"], "stale")
        self.assertEqual(agent["action"], "Нет свежих событий")
        self.assertEqual(agent["event_at"], iso(-200))
        fresh = self.write("fresh.jsonl", [codex_line(-10, "response_item", {"type": "function_call", "name": "apply_patch"})])
        agent = self.agent(obs.build_snapshot(fresh, None, None, NOW), "codex")
        self.assertEqual(agent["status"], "editing")

    def test_task_complete_vs_item_completed(self):
        items = [
            codex_line(-40, "event_msg", {"type": "task_started"}),
            codex_line(-30, "response_item", {"type": "function_call", "name": "exec"}),
            codex_line(-20, "event_msg", {"type": "item_completed"}),
        ]
        path = self.write("codex.jsonl", items)
        agent = self.agent(obs.build_snapshot(path, None, None, NOW), "codex")
        self.assertNotEqual(agent["status"], "done")
        self.assertEqual(agent["status"], "working")
        items.append(codex_line(-10, "event_msg", {"type": "task_complete"}))
        path = self.write("codex2.jsonl", items)
        agent = self.agent(obs.build_snapshot(path, None, None, NOW), "codex")
        self.assertEqual(agent["status"], "done")
        self.assertEqual(agent["action"], "Завершено")

    def test_tool_output_is_not_success(self):
        path = self.write("codex.jsonl", [
            codex_line(-5, "response_item", {"type": "function_call_output", "output": "ok"})])
        agent = self.agent(obs.build_snapshot(path, None, None, NOW), "codex")
        self.assertNotEqual(agent["status"], "done")

    def test_unknown_tool_is_generic_activity(self):
        path = self.write("codex.jsonl", [
            codex_line(-5, "response_item", {"type": "function_call", "name": "mystery_tool"})])
        snapshot = obs.build_snapshot(path, None, None, NOW)
        self.assertEqual(self.agent(snapshot, "codex")["action"], "Свежая активность")
        self.assertNotIn("mystery_tool", json.dumps(snapshot))

    def test_events_are_collapsed_and_limited(self):
        lines = []
        for index in range(60):
            name = "exec" if index % 2 else "apply_patch"
            lines.append(codex_line(-80 + index, "response_item", {"type": "function_call", "name": name}))
        path = self.write("codex.jsonl", lines)
        snapshot = obs.build_snapshot(path, None, None, NOW)
        self.assertLessEqual(len(snapshot["events"]), obs.MAX_EVENTS_TOTAL)
        self.assertLessEqual(len(snapshot["events"]), obs.MAX_EVENTS_PER_SOURCE)
        for event in snapshot["events"]:
            self.assertEqual(set(event), {"agent_id", "at", "action"})
            self.assertIn(event["action"], obs.ACTION_LABELS.values())


class CliStateTests(TempCase):
    def test_cli_state_allowlist(self):
        good = self.write("cli.json", [json.dumps({
            "agent_id": "claude-cli", "task": "Офис агентов", "status": "working",
            "event_at": iso(-4), "tool": "Edit", "session_id": "session-uuid-777"})])
        snapshot = obs.build_snapshot(None, None, good, NOW)
        agent = self.agent(snapshot, "claude-cli")
        self.assertEqual(agent["status"], "editing")
        self.assertEqual(agent["task"], "Офис агентов")
        self.assertNotIn("session-uuid-777", json.dumps(snapshot))

        done = self.write("cli_done.json", [json.dumps({
            "agent_id": "claude-cli", "status": "done", "event_at": iso(-400)})])
        self.assertEqual(self.agent(obs.build_snapshot(None, None, done, NOW), "claude-cli")["status"], "done")

        error = self.write("cli_err.json", [json.dumps({
            "agent_id": "claude-cli", "status": "error", "event_at": iso(-4)})])
        self.assertEqual(self.agent(obs.build_snapshot(None, None, error, NOW), "claude-cli")["status"], "error")

    def test_cli_state_rejects_bad_content(self):
        cases = {
            "status.json": {"agent_id": "claude-cli", "status": "hacked", "event_at": iso(-1)},
            "agent.json": {"agent_id": "someone-else", "status": "working", "event_at": iso(-1)},
            "time.json": {"agent_id": "claude-cli", "status": "working", "event_at": "yesterday"},
            "list.json": [1, 2, 3],
        }
        for name, data in cases.items():
            path = self.write(name, [json.dumps(data)])
            agent = self.agent(obs.build_snapshot(None, None, path, NOW), "claude-cli")
            self.assertEqual(agent["status"], "unknown", name)
            self.assertIsNone(agent["event_at"], name)

    def test_cli_state_size_limit(self):
        path = self.logs / "huge.json"
        body = json.dumps({"agent_id": "claude-cli", "status": "working", "event_at": iso(-1)})
        path.write_text(body + " " * (obs.STATE_MAX_BYTES + 10), encoding="utf-8")
        agent = self.agent(obs.build_snapshot(None, None, path, NOW), "claude-cli")
        self.assertEqual(agent["status"], "unknown")

    def test_cli_working_without_known_tool_is_generic(self):
        path = self.write("cli.json", [json.dumps({
            "agent_id": "claude-cli", "status": "working", "event_at": iso(-4), "tool": ["Edit"]})])
        agent = self.agent(obs.build_snapshot(None, None, path, NOW), "claude-cli")
        self.assertEqual(agent["status"], "working")


class OutputTests(TempCase):
    def test_output_accepts_any_existing_directory(self):
        self.assertEqual(obs.validate_output(self.root / "agent-office.json", []).parent, self.root)
        for path in (self.state_dir / "missing" / "agent-office.json", self.state_dir / "1.json", ""):
            with self.assertRaises(obs.OutputError):
                obs.validate_output(path, [])

    def test_output_cannot_equal_source(self):
        target = self.state_dir / "agent-office.json"
        for kwargs in ({"codex": target}, {"claude": target}, {"cli": target}):
            sources = [kwargs.get("codex"), kwargs.get("claude"), kwargs.get("cli")]
            with self.assertRaises(obs.OutputError):
                obs.validate_output(target, sources)
        target.write_text("{}", encoding="utf-8")
        alias = self.state_dir / ".." / "state" / "agent-office.json"
        with self.assertRaises(obs.OutputError):
            obs.validate_output(target, [alias])

    def test_atomic_write_and_no_leftovers(self):
        target = self.state_dir / "agent-office.json"
        target.write_text("old", encoding="utf-8")
        codex = self.write("codex.jsonl", [codex_line(-5, "response_item", {"type": "function_call", "name": "exec"})])
        other = self.state_dir / "1.json"
        other.write_text('{"keep":true}', encoding="utf-8")
        code = obs.main(["--codex-log", str(codex), "--output", str(target), "--once"])
        self.assertEqual(code, 0)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["observer"]["mode"], "read_only")
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), ["1.json", "agent-office.json"])
        self.assertEqual(other.read_text(encoding="utf-8"), '{"keep":true}')

    def test_main_refuses_collision(self):
        target = self.state_dir / "agent-office-cli.json"
        target.write_text(json.dumps({"agent_id": "claude-cli", "status": "done", "event_at": iso(-1)}), encoding="utf-8")
        before = target.read_bytes()
        code = obs.main(["--cli-state", str(target), "--output", str(target), "--once"])
        self.assertEqual(code, 2)
        self.assertEqual(before, target.read_bytes())

    def test_main_accepts_portable_directory(self):
        outside = self.root / "agent-office.json"
        code = obs.main(["--output", str(outside), "--once"])
        self.assertEqual(code, 0)
        self.assertTrue(outside.exists())

    def test_russian_text_written_as_utf8(self):
        target = self.state_dir / "agent-office.json"
        self.assertEqual(obs.main(["--output", str(target), "--once"]), 0)
        raw = target.read_bytes()
        raw.decode("utf-8")
        self.assertIn("Нет данных".encode("utf-8"), raw)

    def test_interval_and_duration_are_clamped(self):
        self.assertEqual(obs.clamp(0.0, obs.MIN_INTERVAL, obs.MAX_INTERVAL), obs.MIN_INTERVAL)
        self.assertEqual(obs.clamp(9999, obs.MIN_INTERVAL, obs.MAX_INTERVAL), obs.MAX_INTERVAL)
        self.assertEqual(obs.clamp(10 ** 9, 1.0, obs.MAX_DURATION), obs.MAX_DURATION)

    def test_duration_stops_loop(self):
        target = self.state_dir / "agent-office.json"
        started = obs.time.monotonic()
        code = obs.main(["--output", str(target), "--interval", "0.5", "--duration", "1"])
        self.assertEqual(code, 0)
        self.assertLess(obs.time.monotonic() - started, 5)
        self.assertTrue(target.exists())


class NoNetworkTests(TempCase):
    def test_imports_are_stdlib_and_safe(self):
        tree = ast.parse(OBSERVER_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {"__future__", "argparse", "json", "os", "re", "sys", "time", "tempfile", "collections", "datetime", "pathlib"}
        self.assertLessEqual(imported, allowed)
        for forbidden in ("socket", "subprocess", "urllib", "http", "requests", "ctypes", "shutil"):
            self.assertNotIn(forbidden, imported)

    def test_run_does_not_touch_network_or_processes(self):
        codex = self.write("codex.jsonl", [codex_line(-5, "response_item", {"type": "function_call", "name": "exec"})])
        target = self.state_dir / "agent-office.json"

        def boom(*args, **kwargs):
            raise AssertionError("сеть или процессы запрещены")

        with mock.patch.object(socket, "socket", boom), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(os, "system", boom):
            self.assertEqual(obs.main(["--codex-log", str(codex), "--output", str(target), "--once"]), 0)

    def test_html_has_no_network_calls(self):
        html = (HERE / "index.html").read_text(encoding="utf-8")
        for token in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "innerHTML",
                      "insertAdjacentHTML", "alert(", "http://", "https://", "cdn."):
            self.assertNotIn(token, html, token)


def collab_call(delta, name, call_id, arguments=None, namespace="collaboration"):
    payload = {"type": "function_call", "name": name, "call_id": call_id,
               "arguments": json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)}
    if namespace is not None:
        payload["namespace"] = namespace
    return codex_line(delta, "response_item", payload)


def collab_output(delta, call_id, output):
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    return codex_line(delta, "response_item",
                      {"type": "function_call_output", "call_id": call_id, "output": output})


def spawn_lines(delta, task, call_id, message="ok", namespace="collaboration"):
    return [collab_call(delta, "spawn_agent", call_id, {"message": message}, namespace),
            collab_output(delta + 1, call_id, {"task_name": task})]


def list_lines(delta, call_id, agents):
    return [collab_call(delta, "list_agents", call_id, {}),
            collab_output(delta + 1, call_id, {"agents": agents})]


def listed(name, status):
    return {"agent_name": name, "agent_status": status}


REPORT = {"completed": "ДЛИННЫЙ ОТЧЁТ SECRET_TOKEN_abc123 sk-TESTSECRET999"}


class CollabTests(TempCase):
    def snap(self, lines, now=NOW, tracker=None, name="codex.jsonl"):
        path = self.write(name, lines)
        tracker = obs.CollabTracker() if tracker is None else tracker
        return obs.build_snapshot(path, None, None, now, tracker)

    def helpers(self, snapshot):
        return [a for a in snapshot["agents"] if a["source"] == "codex_team"]

    def test_without_events_only_core_agents(self):
        snapshot = self.snap([codex_line(-5, "response_item", {"type": "function_call", "name": "exec"})])
        self.assertEqual([a["id"] for a in snapshot["agents"]], ["codex", "claude-cli", "claude-vscode"])

    def test_spawn_creates_helpers_with_safe_labels(self):
        lines = (spawn_lines(-20, "/root/office_review", "c1", message="SECRET_TOKEN_abc123 sk-TESTSECRET999")
                 + spawn_lines(-15, "/root/mystery_task_xyz", "c2")
                 + spawn_lines(-12, "/root/sprint_p2_review", "c3"))
        snapshot = self.snap(lines)
        helpers = self.helpers(snapshot)
        self.assertEqual([h["name"] for h in helpers], ["Помощник office_revie", "Помощник mystery_task", "Помощник sprint_p2_re"])
        self.assertEqual(helpers[0]["id"], "h-office_review")
        for helper in helpers:
            self.assertEqual(helper["role"], "Помощник Codex")
            self.assertEqual(helper["task"], "Изолированное поручение")
            self.assertEqual(helper["status"], "assigned")
            self.assertEqual(helper["action"], "Назначено поручение")
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", helper["id"]))
        self.assertEqual(helpers[0]["event_at"], iso(-19))
        dumped = json.dumps(snapshot, ensure_ascii=False)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, dumped)
        self.assertNotIn("/root", dumped)
        self.assertEqual(len(snapshot["agents"]), 6)

    def test_running_then_completed_without_report_leak(self):
        tracker = obs.CollabTracker()
        first = (spawn_lines(-100, "/root/office_review", "c1")
                 + list_lines(-60, "c2", [listed("/root", "running"), listed("/root/office_review", "running")]))
        snapshot = self.snap(first, tracker=tracker)
        helpers = self.helpers(snapshot)
        self.assertEqual(len(helpers), 1)  # сам /root не дублируется
        self.assertEqual(helpers[0]["status"], "working")
        self.assertEqual(helpers[0]["action"], "Свежая активность")
        second = first + list_lines(-10, "c3", [listed("/root", "running"), listed("/root/office_review", REPORT)])
        snapshot = self.snap(second, tracker=tracker)
        helper = self.helpers(snapshot)[0]
        self.assertEqual(helper["status"], "done")
        self.assertEqual(helper["action"], "Завершено")
        dumped = json.dumps(snapshot, ensure_ascii=False)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, dumped)
        self.assertNotIn("ОТЧЁТ", dumped)

    def test_old_completed_agents_are_not_shown(self):
        unseen = list_lines(-10, "c1", [listed("/root/bridge_review", REPORT), listed("/root/office_review", REPORT)])
        self.assertEqual(self.helpers(self.snap(unseen)), [])
        historic = spawn_lines(-10800, "/root/bridge_review", "c2") + list_lines(-10790, "c3", [
            listed("/root/bridge_review", REPORT)])
        self.assertEqual(self.helpers(self.snap(historic, name="old.jsonl")), [])

    def test_known_completed_rests_then_leaves(self):
        tracker = obs.CollabTracker()
        lines = spawn_lines(-60, "/root/office_review", "c1") + list_lines(-30, "c2", [
            listed("/root/office_review", REPORT)])
        self.assertEqual(self.helpers(self.snap(lines, tracker=tracker))[0]["status"], "done")
        later = NOW + timedelta(seconds=60)
        self.assertEqual(self.helpers(self.snap(lines, now=later, tracker=tracker))[0]["status"], "done")
        gone = NOW + timedelta(seconds=200)
        self.assertEqual(self.helpers(self.snap(lines, now=gone, tracker=tracker)), [])
        self.assertEqual(tracker.agents, {})

    def test_repeated_completed_does_not_extend_rest(self):
        lines = (spawn_lines(-300, "/root/office_review", "c1")
                 + list_lines(-150, "c2", [listed("/root/office_review", REPORT)])
                 + list_lines(-10, "c3", [listed("/root/office_review", REPORT)]))
        self.assertEqual(self.helpers(self.snap(lines)), [])

    def test_wrong_namespace_names_and_unpaired_outputs(self):
        lines = []
        lines += spawn_lines(-30, "/root/office_review", "n1", namespace="other_tool")
        lines += spawn_lines(-30, "/root/office_review", "n2", namespace=None)
        bad_names = ["/root", "/root/", "/root/Bad Name", "/root/../etc", "/etc/passwd", "/root/a//b",
                     "/root/" + "a" * 80, "<script>alert(1)</script>", 123, None]
        for index, name in enumerate(bad_names):
            lines += spawn_lines(-25, name, "b%d" % index)
        lines.append(collab_output(-20, "ghost-call", {"task_name": "/root/ghost"}))
        lines += [collab_call(-20, "other_op", "w1"), collab_output(-19, "w1", {"task_name": "/root/wrong_op"})]
        lines += [collab_call(-20, "spawn_agent", "m1"), collab_output(-19, "m2", {"task_name": "/root/mismatch"})]
        lines += [collab_call(-20, "spawn_agent", "j1"), collab_output(-19, "j1", "not json")]
        lines += list_lines(-10, "l1", [listed("/root/a//b", "running"), listed("<b>x</b>", "running"), "text", 5])
        lines += [collab_call(-9, "list_agents", "l2", {}, namespace="other_tool"),
                  collab_output(-8, "l2", {"agents": [listed("/root/foreign", "running")]})]
        snapshot = self.snap(lines)
        self.assertEqual(self.helpers(snapshot), [])
        self.assertEqual(len(snapshot["agents"]), 3)

    def test_dedup_same_agent(self):
        tracker = obs.CollabTracker()
        lines = (spawn_lines(-30, "/root/office_review", "a1") + spawn_lines(-30, "/root/office_review", "a2")
                 + list_lines(-20, "a3", [listed("/root/office_review", "running"),
                                          listed("/root/office_review", "running")]))
        for _ in range(2):
            snapshot = self.snap(lines, tracker=tracker)
            self.assertEqual(len(self.helpers(snapshot)), 1)
            self.assertEqual(len(tracker.agents), 1)
        self.assertEqual(len([e for e in snapshot["events"] if e["agent_id"] == "h-office_review"]), 1)

    def test_stale_after_90_seconds_then_removed_after_ttl(self):
        tracker = obs.CollabTracker()
        lines = spawn_lines(-200, "/root/office_review", "c1")
        helper = self.helpers(self.snap(lines, tracker=tracker))[0]
        self.assertEqual(helper["status"], "stale")
        self.assertEqual(helper["action"], "Нет свежих событий")
        self.assertEqual(helper["event_at"], iso(-199))
        running = spawn_lines(-300, "/root/sprint_p2_review", "c2") + list_lines(-150, "c3", [
            listed("/root/sprint_p2_review", "running")])
        stale = self.helpers(self.snap(running, name="r.jsonl", tracker=obs.CollabTracker()))[0]
        self.assertEqual(stale["status"], "stale")
        late = NOW + timedelta(seconds=400)
        self.assertEqual(self.helpers(self.snap(lines, now=late, tracker=tracker)), [])
        self.assertEqual(tracker.agents, {})

    def test_followup_reassigns_without_message_leak(self):
        lines = (spawn_lines(-100, "/root/office_review", "c1")
                 + list_lines(-80, "c2", [listed("/root/office_review", REPORT)])
                 + [collab_call(-10, "followup_task", "c3", {"target": "/root/office_review",
                                                             "message": "SECRET_TOKEN_abc123 sk-TESTSECRET999"}),
                    collab_output(-9, "c3", "")])
        snapshot = self.snap(lines)
        helper = self.helpers(snapshot)[0]
        self.assertEqual(helper["status"], "assigned")
        self.assertEqual(helper["event_at"], iso(-9))
        dumped = json.dumps(snapshot, ensure_ascii=False)
        for marker in SECRET_MARKERS:
            self.assertNotIn(marker, dumped)
        bad = [collab_call(-10, "followup_task", "f1", {"target": "../x", "message": "m"}),
               collab_output(-9, "f1", "")]
        self.assertEqual(self.helpers(self.snap(bad, name="bad.jsonl")), [])

    def test_followup_error_or_text_does_not_reassign(self):
        base = spawn_lines(-100, "/root/office_review", "c1") + list_lines(-80, "c2", [
            listed("/root/office_review", REPORT)])
        for index, output in enumerate(["Error: agent is not available", "что угодно", {"ok": True}, " "]):
            call = [collab_call(-10, "followup_task", "f%d" % index, {"target": "/root/office_review"}),
                    collab_output(-9, "f%d" % index, output)]
            helper = self.helpers(self.snap(base + call, name="e%d.jsonl" % index))[0]
            self.assertEqual(helper["status"], "done")
            self.assertEqual(helper["event_at"], iso(-79))
        refused = [collab_call(-10, "followup_task", "g1", {"target": "/root/ghost_agent"}),
                   collab_output(-9, "g1", "Error: agent is not available")]
        self.assertEqual(self.helpers(self.snap(refused, name="g.jsonl")), [])

    def test_ancient_spawn_with_fresh_completed_not_shown(self):
        lines = spawn_lines(-10000, "/root/bridge_review", "c1") + list_lines(-10, "c2", [
            listed("/root/bridge_review", REPORT)])
        self.assertEqual(self.helpers(self.snap(lines)), [])

    def test_stale_spawn_within_limit_still_present(self):
        helper = self.helpers(self.snap(spawn_lines(-200, "/root/office_review", "c1"), name="s.jsonl"))[0]
        self.assertEqual(helper["status"], "stale")

    def test_known_agent_completes_after_later_list(self):
        tracker = obs.CollabTracker()
        self.snap(spawn_lines(-250, "/root/office_review", "c1"), tracker=tracker)
        lines = spawn_lines(-250, "/root/office_review", "c1") + list_lines(-10, "c2", [
            listed("/root/office_review", REPORT)])
        self.assertEqual(self.helpers(self.snap(lines, tracker=tracker))[0]["status"], "done")

    def test_state_is_bounded(self):
        tracker = obs.CollabTracker()
        lines = []
        for index in range(20):
            lines += spawn_lines(-40 + index, "/root/task_%02d" % index, "s%02d" % index)
        snapshot = self.snap(lines, tracker=tracker)
        self.assertLessEqual(len(self.helpers(snapshot)), obs.MAX_HELPERS)
        self.assertLessEqual(len(snapshot["agents"]), 6)
        self.assertLessEqual(len(tracker.agents), obs.MAX_TRACKED_AGENTS)
        self.assertLessEqual(len(tracker.calls), obs.MAX_TRACKED_CALLS)
        ids = [a["id"] for a in snapshot["agents"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_helpers_survive_tail_rotation(self):
        tracker = obs.CollabTracker()
        self.snap(spawn_lines(-20, "/root/office_review", "c1"), tracker=tracker)
        later = self.snap([codex_line(-5, "response_item", {"type": "function_call", "name": "exec"})],
                          tracker=tracker, name="next.jsonl")
        self.assertEqual(len(self.helpers(later)), 1)

    def test_first_read_uses_larger_tail(self):
        tracker = obs.CollabTracker()
        self.assertEqual(tracker.tail_bytes(), obs.COLLAB_START_TAIL_BYTES)
        filler = codex_line(-5, "response_item", {"type": "function_call", "name": "exec"})
        count = (200 * 1024) // (len(filler) + 1) + 1
        lines = spawn_lines(-30, "/root/office_review", "c1") + [filler] * count
        snapshot = self.snap(lines, tracker=tracker)
        self.assertEqual(len(self.helpers(snapshot)), 1)
        self.assertEqual(tracker.tail_bytes(), obs.TAIL_BYTES)


if __name__ == "__main__":
    unittest.main()
