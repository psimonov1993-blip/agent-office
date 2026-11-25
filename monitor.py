"""Portable configuration and read-only monitoring. Python standard library only."""
from __future__ import annotations

import json
import re
from pathlib import Path

import observer as core

KINDS = {"codex", "claude", "state"}
STATUSES = frozenset(core.ACTION_LABELS) | {"waiting", "checking"}
LABELS = dict(core.ACTION_LABELS, waiting="Ожидает", checking="Проверяет")
MAX_SOURCES = 6


def label(value, default, limit):
    if not isinstance(value, str):
        return default
    value = re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", value)
    return " ".join(value.split())[:limit] or default


def validate_config(data, directory):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Ожидается конфигурация version: 1.")
    sources = data.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ValueError("В sources должен быть список: до 6 источников.")
    result, ids, paths = [], set(), set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Каждый источник должен быть объектом.")
        sid, kind, path = source.get("id"), source.get("kind"), source.get("path")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", sid) or sid in ids:
            raise ValueError("ID источника: 1–20 латинских букв, цифр, _ или -; без повторов.")
        if not isinstance(kind, str) or kind not in KINDS or not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("Укажите kind (codex, claude, state) и путь к файлу.")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(directory) / candidate
        candidate = candidate.resolve()
        if candidate.suffix.lower() != (".json" if kind == "state" else ".jsonl"):
            raise ValueError("Выберите .jsonl для журнала или .json для состояния.")
        key = str(candidate).casefold() if core.os.name == "nt" else str(candidate)
        if key in paths:
            raise ValueError("Этот файл уже подключён.")
        ids.add(sid)
        paths.add(key)
        if "helpers" in source and not isinstance(source["helpers"], bool):
            raise ValueError("helpers должен быть true или false.")
        result.append({"id": sid, "kind": kind, "path": str(candidate),
                       "name": label(source.get("name"), kind.title(), 24),
                       "role": label(source.get("role"), "Подключённый агент", 40),
                       "task": label(source.get("task"), "Задача не указана", 48),
                       "helpers": source.get("helpers", True)})
    return {"version": 1, "sources": result}


def load_config(path):
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Конфигурация больше 64 КБ.")
    return validate_config(json.loads(raw.decode("utf-8-sig")), path.parent)


def save_config(path, data):
    """Validate first; keep the user's local paths only in this ignored file."""
    path = Path(path)
    normalized = validate_config(data, path.parent)
    if path.is_symlink():
        raise ValueError("Конфигурация не должна быть ссылкой.")
    for source in normalized["sources"]:
        if core._same_file(path, source["path"]):
            raise ValueError("Конфигурация совпадает с источником.")
    core.write_atomic(path, core.encode_snapshot(normalized))
    return normalized


def read_state(path, now):
    state = core.source_state(path, ".json")
    if state != "ok":
        return state, []
    try:
        with open(path, "rb") as stream:
            raw = stream.read(core.STATE_MAX_BYTES + 1)
        if len(raw) > core.STATE_MAX_BYTES:
            return "unreadable", []
        obj = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(obj, dict) or not isinstance(obj.get("status"), str) or obj["status"] not in STATUSES:
            return "unreadable", []
        moment = core.parse_timestamp(obj.get("event_at"), now)
        return ("ok", [core.Event(moment, obj["status"], 0)]) if moment else ("empty", [])
    except (OSError, ValueError, RecursionError):
        return "unreadable", []


class Monitor:
    def __init__(self, config):
        self.sources = config["sources"]
        self.trackers = {s["id"]: core.CollabTracker() for s in self.sources if s["kind"] == "codex"}

    def snapshot(self, now=None):
        now = now or core.utc_now()
        agents, events, health, helpers = [], [], {}, []
        for source in self.sources:
            sid, kind = source["id"], source["kind"]
            tracker = self.trackers.get(sid)
            if kind == "state":
                state, found = read_state(source["path"], now)
            elif tracker and source["helpers"]:
                state, found = core.collect_log(source["path"], core.extract_codex_event, now,
                                                tracker.tail_bytes(), tracker.feed)
                if state in ("ok", "empty"):
                    tracker.started = True
                records, _ = tracker.snapshot_parts(now)
                for record in records:
                    # Names from logs are not displayed. IDs are namespaced hashes.
                    record["id"] = "h-" + sid + "-" + core._fnv(record["id"])
                    record["name"] = "Помощник " + source["name"][:14]
                    record["task"] = source["task"]
                    helpers.append(record)
            else:
                extractor = core.extract_codex_event if kind == "codex" else core.extract_claude_event
                state, found = core.collect_log(source["path"], extractor, now)
            health[sid] = state
            definition = dict(source, source={"codex": "codex_log", "claude": "claude_log", "state": "custom_state"}[kind])
            latest = max(found, key=lambda e: (e.at, e.seq)) if found else None
            status = latest.status if latest else "unknown"
            if latest and status in (core.ACTIVE_STATUSES | {"assigned", "checking"}) and (now-latest.at).total_seconds() > core.FRESH_EVENT_SECONDS:
                status = "stale"
            agents.append({k: definition[k] for k in ("id", "name", "role", "task", "source")})
            agents[-1].update(status=status, action=LABELS[status], event_at=core.iso_z(latest.at) if latest else None)
            for event in core._collapse(found)[-core.MAX_EVENTS_PER_SOURCE:]:
                events.append({"agent_id": sid, "at": core.iso_z(event.at), "action": LABELS[event.status]})
        capacity = max(0, MAX_SOURCES - len(agents))
        agents.extend(helpers[:capacity])
        return {"schema_version": 1, "generated_at": core.iso_z(now), "agents": agents,
                "events": sorted(events, key=lambda e: e["at"])[-24:],
                "observer": {"mode": "read_only", "sources": health, "hidden_helpers": max(0, len(helpers)-capacity)}}

    def write(self, output):
        target = core.validate_output(output, [s["path"] for s in self.sources])
        snapshot = self.snapshot()
        core.write_atomic(target, core.encode_snapshot(snapshot))
        return snapshot
