#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ручной наблюдатель для страницы «Agent Office».

Только чтение. Без сети, без subprocess, без зависимостей.

Что читает:
  * хвост (до 128 КБ, при старте журнала Codex до 512 КБ) явно заданных журналов .jsonl Codex и Claude;
  * один небольшой JSON безопасного состояния прямого CLI.
Что делает с прочитанным:
  * берёт только type / subtype / timestamp и название инструмента из белого списка;
  * для помощников Codex дополнительно берёт только подтверждённую пару call_id -> операция
    (namespace collaboration: spawn_agent, followup_task, list_agents), валидное имя задачи
    /root/..., и тип agent_status (running, completed, errored, иное unknown);
  * хвост файла физически читается целиком, но тексты сообщений, мышление, аргументы (включая
    message), отчёты completed, команды и учётные данные не извлекаются и не сохраняются;
  * держит небольшую память процесса (до 16 вызовов и 16 помощников), без файлов очередей;
  * пишет один собственный JSON в выбранный существующий каталог.

Наблюдатель показывает события из журналов, а не состояние процессов.
Он ничего не знает о том, жив ли процесс агента.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
TAIL_BYTES = 128 * 1024
STATE_MAX_BYTES = 64 * 1024
OUTPUT_MAX_BYTES = 64 * 1024
FRESH_EVENT_SECONDS = 90
MAX_FUTURE_SKEW_SECONDS = 300
MAX_EVENTS_PER_SOURCE = 8
MAX_EVENTS_TOTAL = 24
MIN_INTERVAL = 0.5
MAX_INTERVAL = 60.0
MAX_DURATION = 8 * 3600.0
DEFAULT_INTERVAL = 2.0
DEFAULT_DURATION = 7200.0

COLLAB_START_TAIL_BYTES = 512 * 1024
MAX_TRACKED_CALLS = 16
MAX_TRACKED_AGENTS = 16
MAX_HELPERS = 3
MAX_LIST_ITEMS = 32
MAX_JSON_TEXT = 256 * 1024
HELPER_REST_SECONDS = 120
HELPER_STALE_SECONDS = 300

ACTIVE_STATUSES = frozenset({"reading", "editing", "running", "working"})

ACTION_LABELS = {
    "assigned": "Назначено поручение",
    "reading": "Читает",
    "editing": "Правит файл",
    "running": "Выполняет команду",
    "working": "Свежая активность",
    "done": "Завершено",
    "error": "Ошибка",
    "stale": "Нет свежих событий",
    "unknown": "Нет данных",
}

# Имена и роли задаёт код, а не поля журналов.
AGENTS = (
    {"id": "codex", "name": "Codex", "role": "Руководитель сессии",
     "task": "Руководство сессией", "source": "codex_log"},
    {"id": "claude-cli", "name": "Claude CLI", "role": "Исполнитель в терминале",
     "task": "Поручение Codex", "source": "cli_state"},
    {"id": "claude-vscode", "name": "Claude VS Code", "role": "Исполнитель в редакторе",
     "task": "Работа в VS Code", "source": "claude_log"},
)

# Белые списки инструментов. Название инструмента никогда не попадает в снимок:
# оно превращается в одно из русских действий выше.
CODEX_TOOLS = {
    "read_file": "reading", "view_image": "reading", "list_dir": "reading",
    "grep_files": "reading", "read": "reading", "search": "reading",
    "apply_patch": "editing", "edit": "editing", "write": "editing", "write_file": "editing",
    "exec": "running", "exec_command": "running", "shell": "running",
    "shell_command": "running", "local_shell": "running", "bash": "running",
    "container.exec": "running", "write_stdin": "running",
    "update_plan": "working", "web_search": "working",
}
CLAUDE_TOOLS = {
    "Read": "reading", "Grep": "reading", "Glob": "reading", "LS": "reading",
    "NotebookRead": "reading",
    "Edit": "editing", "MultiEdit": "editing", "Write": "editing", "NotebookEdit": "editing",
    "Bash": "running", "BashOutput": "running", "KillShell": "running",
    "Task": "working", "TodoWrite": "working", "WebFetch": "working", "WebSearch": "working",
}
CLI_STATUSES = frozenset({"working", "done", "error"})
CLI_TASK_LABELS = frozenset({"Офис агентов", "Поручение Codex"})

Event = namedtuple("Event", ["at", "status", "seq"])


class OutputError(Exception):
    """Недопустимый путь вывода."""


# ---------------------------------------------------------------- время

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$"
)


def utc_now():
    return datetime.now(timezone.utc)


def iso_z(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value, now=None):
    """Разбирает ISO-время (любая зона, без зоны считается UTC) или epoch.

    Возвращает aware datetime в UTC либо None. Время дальше чем на 5 минут
    в будущем считается недостоверным.
    """
    moment = None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e11:
                seconds /= 1000.0
            moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 40:
            return None
        match = _TS_RE.match(text)
        if not match:
            return None
        year, month, day, hour, minute, second, frac, zone = match.groups()
        try:
            micro = int(((frac or "") + "000000")[:6])
            tzinfo = timezone.utc
            if zone and zone not in ("Z", "z"):
                sign = -1 if zone[0] == "-" else 1
                digits = zone[1:].replace(":", "")
                off_h = int(digits[:2])
                off_m = int(digits[2:4]) if len(digits) >= 4 else 0
                if off_h > 14 or off_m > 59:
                    return None
                tzinfo = timezone(sign * timedelta(hours=off_h, minutes=off_m))
            moment = datetime(int(year), int(month), int(day), int(hour), int(minute),
                              int(second or 0), micro, tzinfo=tzinfo)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    try:
        moment = moment.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    if moment.year < 2000 or moment.year > 2100:
        return None
    if now is not None and (moment - now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
        return None
    return moment


# ---------------------------------------------------------------- чтение источников

def source_state(path, suffix):
    """Проверяет источник без чтения содержимого."""
    if path is None:
        return "not_configured"
    try:
        if "\x00" in str(path):
            return "unreadable"
        candidate = Path(path)
        if candidate.suffix.lower() != suffix:
            return "unreadable"
        if not candidate.exists():
            return "missing"
        if not candidate.is_file():
            return "unreadable"
    except (OSError, ValueError):
        return "unreadable"
    return "ok"


def read_tail(path, max_bytes=TAIL_BYTES):
    """Возвращает строки (bytes) из хвоста файла не больше max_bytes.

    Если чтение началось не с начала файла, первая (возможно обрезанная) строка отбрасывается.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read(max_bytes)
    if start > 0:
        newline = data.find(b"\n")
        data = b"" if newline < 0 else data[newline + 1:]
    return data.split(b"\n")


def _short_str(value, limit=64):
    if isinstance(value, str) and 0 < len(value) <= limit:
        return value
    return ""


def extract_codex_event(obj, seq, now):
    if not isinstance(obj, dict):
        return None
    moment = parse_timestamp(obj.get("timestamp"), now)
    payload = obj.get("payload")
    if moment is None or not isinstance(payload, dict):
        return None
    top = obj.get("type")
    kind = payload.get("type")
    status = None
    if top == "response_item":
        if kind in ("function_call", "custom_tool_call"):
            name = _short_str(payload.get("name")).lower()
            if name not in CODEX_TOOLS:
                for prefix in ("functions.", "tools."):
                    if name.startswith(prefix):
                        name = name[len(prefix):]
                        break
            status = CODEX_TOOLS.get(name, "working")
        elif kind == "message":
            if payload.get("role") == "assistant":
                status = "working"
        elif kind in ("function_call_output", "custom_tool_call_output", "reasoning"):
            # Вывод инструмента не означает успешное задание, это только свежая активность.
            status = "working"
    elif top == "event_msg":
        if kind == "task_complete":
            status = "done"
        elif kind == "error":
            status = "error"
        elif kind in ("task_started", "item_completed", "agent_message", "agent_reasoning"):
            # item_completed завершает один элемент, а не всё задание.
            status = "working"
    if status is None:
        return None
    return Event(moment, status, seq)


def extract_claude_event(obj, seq, now):
    if not isinstance(obj, dict):
        return None
    moment = parse_timestamp(obj.get("timestamp"), now)
    if moment is None:
        return None
    top = obj.get("type")
    status = None
    if top == "assistant":
        status = "working"
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for item in content[:64]:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    name = item.get("name")
                    if isinstance(name, str):
                        status = CLAUDE_TOOLS.get(name, "working")
    elif top == "system":
        if obj.get("subtype") == "turn_duration":
            status = "done"
    if status is None:
        return None
    return Event(moment, status, seq)


def collect_log(path, extractor, now, max_bytes=TAIL_BYTES, observer=None):
    state = source_state(path, ".jsonl")
    if state != "ok":
        return state, []
    try:
        lines = read_tail(path, max_bytes)
    except (OSError, ValueError):
        return "unreadable", []
    events = []
    for seq, raw in enumerate(lines):
        raw = raw.strip()
        if not raw or len(raw) >= max_bytes:
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue  # частичная или битая строка
        if observer is not None:
            try:
                observer(obj, now)
            except Exception:  # помощники не должны ломать основной снимок
                pass
        event = extractor(obj, seq, now)
        if event is not None:
            events.append(event)
    return ("ok" if events else "empty"), events


def collect_cli_state(path, now):
    """Читает безопасное состояние прямого CLI. Содержимое считается недоверенным."""
    state = source_state(path, ".json")
    if state != "ok":
        return state, [], None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(STATE_MAX_BYTES + 1)
    except (OSError, ValueError):
        return "unreadable", [], None
    if len(raw) > STATE_MAX_BYTES:
        return "unreadable", [], None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return "unreadable", [], None
    if not isinstance(data, dict):
        return "unreadable", [], None
    agent_id = data.get("agent_id")
    if agent_id is not None and agent_id != "claude-cli":
        return "unreadable", [], None
    raw_status = data.get("status")
    if not isinstance(raw_status, str) or raw_status not in CLI_STATUSES:
        return "unreadable", [], None
    moment = parse_timestamp(data.get("event_at"), now)
    if moment is None:
        return "empty", [], None
    if raw_status == "working":
        tool = data.get("tool")
        status = CLAUDE_TOOLS.get(tool, "working") if isinstance(tool, str) else "working"
    else:
        status = raw_status
    task = data.get("task")
    label = task if isinstance(task, str) and task in CLI_TASK_LABELS else None
    return "ok", [Event(moment, status, 0)], label


# ---------------------------------------------------------------- помощники Codex

COLLAB_NAMESPACE = "collaboration"
COLLAB_OPS = frozenset({"spawn_agent", "followup_task", "list_agents"})
TERMINAL_STATES = frozenset({"completed", "errored"})
HELPER_STATUS = {"assigned": "assigned", "running": "working", "completed": "done",
                 "errored": "error", "unknown": "unknown"}
HELPER_STALEABLE = frozenset({"assigned", "working"})
HELPER_TITLES = {}
_TASK_NAME_RE = re.compile(r"/root/[a-z0-9_/-]{1,48}")
_CALL_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


def safe_task_name(value):
    """Возвращает имя вида /root/xxx либо None. Сам /root не принимается."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    if not _TASK_NAME_RE.fullmatch(value):
        return None
    slug = value[len("/root/"):]
    if "//" in slug or slug.startswith("/") or slug.endswith("/"):
        return None
    return value


def _fnv(text):
    value = 2166136261
    for char in text:
        value = ((value ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return "%06x" % (value & 0xFFFFFF)


def helper_identity(task_name):
    """Безопасные id и имя помощника. Всё задаёт код, а не журнал."""
    slug = task_name[len("/root/"):]
    flat = "h-" + slug.replace("/", "-")
    if "/" in slug or len(flat) > 32:
        flat = flat[:25] + "-" + _fnv(slug)
    if slug in HELPER_TITLES:
        title = HELPER_TITLES[slug]
    else:
        title = "Помощник " + (slug.rsplit("/", 1)[-1][:12] or "агент")
    return flat, title


def _loads_small(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_JSON_TEXT:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _helper_state(value):
    """Только тип статуса. Содержимое completed/errored никогда не читается."""
    if isinstance(value, str):
        key = value
    elif isinstance(value, dict) and len(value) == 1:
        key = next(iter(value))
    else:
        return "unknown"
    if key in ("running", "completed", "errored"):
        return key
    return "unknown"


class CollabTracker(object):
    """Небольшая память процесса о помощниках Codex. Файлов и очередей нет."""

    def __init__(self):
        self.calls = {}
        self.agents = {}
        self.started = False

    def tail_bytes(self):
        return TAIL_BYTES if self.started else COLLAB_START_TAIL_BYTES

    def feed(self, obj, now):
        if not isinstance(obj, dict) or obj.get("type") != "response_item":
            return
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "function_call":
            self._feed_call(obj, payload, now)
        elif kind == "function_call_output":
            self._feed_output(obj, payload, now)

    def _feed_call(self, obj, payload, now):
        if payload.get("namespace") != COLLAB_NAMESPACE:
            return
        op = payload.get("name")
        call_id = payload.get("call_id")
        if op not in COLLAB_OPS or not isinstance(call_id, str) or not _CALL_ID_RE.fullmatch(call_id):
            return
        if parse_timestamp(obj.get("timestamp"), now) is None:
            return
        target = None
        if op == "followup_task":
            data = _loads_small(payload.get("arguments"))
            if isinstance(data, dict):
                target = safe_task_name(data.get("target"))  # message не читается
            if target is None:
                return
        self.calls.pop(call_id, None)
        self.calls[call_id] = (op, target)
        while len(self.calls) > MAX_TRACKED_CALLS:
            self.calls.pop(next(iter(self.calls)))

    def _feed_output(self, obj, payload, now):
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            return
        pair = self.calls.get(call_id)
        moment = parse_timestamp(obj.get("timestamp"), now)
        if pair is None or moment is None:
            return
        op, target = pair
        if op == "spawn_agent":
            data = _loads_small(payload.get("output"))
            name = safe_task_name(data.get("task_name")) if isinstance(data, dict) else None
            if name:
                self._observe(name, "assigned", moment, now, True)
        elif op == "followup_task":
            # Штатный успех followup_task — пустая строка; ошибки и любой текст не назначение.
            if payload.get("output") == "":
                self._observe(target, "assigned", moment, now, False)
        elif op == "list_agents":
            data = _loads_small(payload.get("output"))
            items = data.get("agents") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return
            for item in items[:MAX_LIST_ITEMS]:
                if not isinstance(item, dict):
                    continue
                name = safe_task_name(item.get("agent_name"))
                if name:
                    self._observe(name, _helper_state(item.get("agent_status")), moment, now, False)

    def _observe(self, name, status, at, now, may_create):
        entry = self.agents.get(name)
        if entry is None:
            fresh = (now - at).total_seconds() <= FRESH_EVENT_SECONDS
            # Завершённых и неизвестных агентов, которых раньше не видели, не показываем.
            young = (now - at).total_seconds() <= HELPER_STALE_SECONDS
            if not ((may_create and young) or (fresh and status in ("assigned", "running"))):
                return
            self.agents[name] = {"status": status, "at": at}
            while len(self.agents) > MAX_TRACKED_AGENTS:
                oldest = min(self.agents, key=lambda key: self.agents[key]["at"])
                del self.agents[oldest]
            return
        if at < entry["at"]:
            return
        if entry["status"] in TERMINAL_STATES:
            if status == entry["status"] or status == "unknown" or at == entry["at"]:
                return  # повтор того же итога не продлевает отдых
        entry["status"] = status
        entry["at"] = at

    def _prune(self, now):
        for name in list(self.agents):
            entry = self.agents[name]
            limit = HELPER_REST_SECONDS if entry["status"] in TERMINAL_STATES else HELPER_STALE_SECONDS
            if (now - entry["at"]).total_seconds() > limit:
                del self.agents[name]

    def snapshot_parts(self, now):
        self._prune(now)
        recent = sorted(self.agents, key=lambda key: self.agents[key]["at"], reverse=True)[:MAX_HELPERS]
        keep = set(recent)
        agents, events = [], []
        for name, entry in self.agents.items():
            if name not in keep:
                continue
            agent_id, title = helper_identity(name)
            status = HELPER_STATUS[entry["status"]]
            shown = status
            age = max(0.0, (now - entry["at"]).total_seconds())
            if status in HELPER_STALEABLE and age > FRESH_EVENT_SECONDS:
                shown = "stale"
            agents.append({
                "id": agent_id,
                "name": title,
                "role": "Помощник Codex",
                "task": "Изолированное поручение",
                "status": shown,
                "action": ACTION_LABELS[shown],
                "event_at": iso_z(entry["at"]),
                "source": "codex_team",
            })
            events.append((entry["at"], -1, {
                "agent_id": agent_id,
                "at": iso_z(entry["at"]),
                "action": ACTION_LABELS[status],
            }))
        return agents, events


# ---------------------------------------------------------------- снимок

def _collapse(events):
    """Оставляет по одному последнему событию из подряд идущих одинаковых статусов."""
    ordered = sorted(events, key=lambda item: (item.at, item.seq))
    result = []
    for event in ordered:
        if result and result[-1].status == event.status:
            result[-1] = event
        else:
            result.append(event)
    return result


def _agent_record(definition, events, now, task_override=None):
    latest = max(events, key=lambda item: (item.at, item.seq)) if events else None
    if latest is None:
        status, event_at = "unknown", None
    else:
        status = latest.status
        event_at = iso_z(latest.at)
        if status in ACTIVE_STATUSES and (now - latest.at).total_seconds() > FRESH_EVENT_SECONDS:
            status = "stale"
    return {
        "id": definition["id"],
        "name": definition["name"],
        "role": definition["role"],
        "task": task_override or definition["task"],
        "status": status,
        "action": ACTION_LABELS[status],
        "event_at": event_at,
        "source": definition["source"],
    }


def build_snapshot(codex_log=None, claude_log=None, cli_state=None, now=None, tracker=None):
    now = now or utc_now()
    tracker = tracker if tracker is not None else CollabTracker()
    results = {}
    for key, path, extractor in (("codex_log", codex_log, extract_codex_event),
                                 ("claude_log", claude_log, extract_claude_event)):
        try:
            if key == "codex_log":
                found = collect_log(path, extractor, now, tracker.tail_bytes(), tracker.feed)
                if found[0] in ("ok", "empty"):
                    tracker.started = True
            else:
                found = collect_log(path, extractor, now)
            results[key] = found + (None,)
        except Exception:  # один источник не должен скрывать остальные
            results[key] = ("unreadable", [], None)
    try:
        results["cli_state"] = collect_cli_state(cli_state, now)
    except Exception:
        results["cli_state"] = ("unreadable", [], None)

    agents, events, sources = [], [], {}
    for definition in AGENTS:
        key = definition["source"]
        state, source_events, task_label = results[key]
        sources[key] = state
        agents.append(_agent_record(definition, source_events, now, task_label))
        for event in _collapse(source_events)[-MAX_EVENTS_PER_SOURCE:]:
            events.append((event.at, event.seq, {
                "agent_id": definition["id"],
                "at": iso_z(event.at),
                "action": ACTION_LABELS[event.status],
            }))
    try:
        helper_agents, helper_events = tracker.snapshot_parts(now)
    except Exception:
        helper_agents, helper_events = [], []
    agents.extend(helper_agents)
    events.extend(helper_events)
    events.sort(key=lambda item: (item[0], item[1]))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_z(now),
        "agents": agents,
        "events": [item[2] for item in events[-MAX_EVENTS_TOTAL:]],
        "observer": {"mode": "read_only", "sources": sources},
    }


# ---------------------------------------------------------------- запись

_OUTPUT_NAME_RE = re.compile(r"^agent-office[A-Za-z0-9._-]*\.json$")


def _same_file(first, second):
    try:
        if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
            return True
        if os.path.exists(first) and os.path.exists(second):
            return os.path.samefile(first, second)
    except (OSError, ValueError):
        return True  # при сомнении считаем совпадением
    return False


def validate_output(output, sources):
    """Проверяет --output и возвращает безопасный Path либо бросает OutputError."""
    if not output:
        raise OutputError("не задан путь вывода")
    try:
        candidate = Path(output)
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise OutputError("каталог вывода не существует")
    if not parent.is_dir():
        raise OutputError("каталог вывода не существует")
    if not _OUTPUT_NAME_RE.match(candidate.name):
        raise OutputError("имя файла вывода должно начинаться с agent-office и заканчиваться на .json")
    target = parent / candidate.name
    try:
        if target.is_symlink():
            raise OutputError("файл вывода не должен быть ссылкой")
        if target.exists() and not target.is_file():
            raise OutputError("путь вывода не является файлом")
    except OSError:
        raise OutputError("путь вывода недоступен")
    for source in sources:
        if source and _same_file(target, source):
            raise OutputError("файл вывода совпадает с источником")
    return target


def write_atomic(target, payload):
    """Атомарно записывает байты рядом с целевым файлом."""
    if len(payload) > OUTPUT_MAX_BYTES:
        raise ValueError("снимок слишком большой")
    fd, temp_name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temp, target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))  # браузер может держать файл открытым
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def encode_snapshot(snapshot):
    return (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def run_cycle(args, target, tracker=None):
    snapshot = build_snapshot(args.codex_log, args.claude_log, args.cli_state, tracker=tracker)
    write_atomic(target, encode_snapshot(snapshot))
    return snapshot


# ---------------------------------------------------------------- CLI

def clamp(value, low, high):
    return max(low, min(high, value))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Ручной наблюдатель за событиями агентов (только чтение).")
    parser.add_argument("--codex-log", metavar="PATH", help="журнал Codex .jsonl")
    parser.add_argument("--claude-log", metavar="PATH", help="журнал Claude .jsonl")
    parser.add_argument("--cli-state", metavar="PATH", help="JSON состояния прямого CLI")
    parser.add_argument("--output", metavar="PATH", required=True,
                        help="agent-office*.json в существующем каталоге")
    parser.add_argument("--once", action="store_true", help="один снимок и выход")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="период в секундах (0.5 до 60)")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION,
                        help="максимальная длительность в секундах (до 28800)")
    return parser.parse_args(argv)


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _configure_stdio()
    args = parse_args(argv)
    interval = args.interval if args.interval == args.interval else DEFAULT_INTERVAL
    duration = args.duration if args.duration == args.duration else DEFAULT_DURATION
    interval = clamp(interval, MIN_INTERVAL, MAX_INTERVAL)
    duration = clamp(duration, 1.0, MAX_DURATION)
    try:
        target = validate_output(args.output, [args.codex_log, args.claude_log, args.cli_state])
    except OutputError as error:
        print("Отказ: " + str(error) + ".", file=sys.stderr)
        return 2

    print("Наблюдатель запущен в режиме только чтения. Остановка: Ctrl+C.")
    deadline = time.monotonic() + duration
    failures = 0
    tracker = CollabTracker()
    try:
        while True:
            try:
                snapshot = run_cycle(args, target, tracker)
                failures = 0
                print("Снимок записан: агентов {0}, событий {1}.".format(
                    len(snapshot["agents"]), len(snapshot["events"])))
            except (OSError, ValueError) as error:
                failures += 1
                print("Снимок не записан ({0}). Повтор на следующем шаге.".format(
                    error.__class__.__name__), file=sys.stderr)
            if args.once:
                return 0 if failures == 0 else 1
            if time.monotonic() + interval >= deadline:
                print("Длительность наблюдения исчерпана. Наблюдатель остановлен.")
                return 0
            slept = 0.0
            while slept < interval:
                step = min(0.25, interval - slept)
                time.sleep(step)
                slept += step
    except KeyboardInterrupt:
        print("Наблюдатель остановлен. Агенты не затронуты.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
