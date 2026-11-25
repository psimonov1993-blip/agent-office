import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import monitor
import observer
import office


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent office тест ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = observer.utc_now()

    def source(self, sid="alice", kind="state", **extra):
        return dict(id=sid, name="Алиса", role="Проверяющий", task="Мой сайт", kind=kind,
                    path=str(self.root / (sid + (".json" if kind == "state" else ".jsonl"))), **extra)

    def config(self, sources):
        return monitor.validate_config({"version": 1, "sources": sources}, self.root)

    def write_state(self, source, status="editing", **extra):
        data = dict(status=status, event_at=observer.iso_z(self.now), **extra)
        Path(source["path"]).write_text(json.dumps(data), encoding="utf-8")

    def test_any_directory_and_configured_names(self):
        source = self.source()
        self.write_state(source)
        conf = self.config([source])
        output = office.make_output(self.root / "config.json", conf)
        snapshot = monitor.Monitor(conf).write(output)
        self.assertEqual(output.parent, self.root / ".runtime")
        self.assertEqual(snapshot["agents"][0]["name"], "Алиса")
        self.assertEqual(snapshot["agents"][0]["status"], "editing")
        self.assertTrue(output.is_file())

    def test_state_does_not_copy_private_fields(self):
        source = self.source()
        self.write_state(source, prompt="SECRET_PROMPT", task="SECRET_TASK", name="SECRET_NAME", token="SECRET_TOKEN", output="SECRET_OUTPUT")
        before = Path(source["path"]).read_bytes()
        snapshot = monitor.Monitor(self.config([source])).snapshot(self.now)
        encoded = json.dumps(snapshot)
        self.assertNotIn("SECRET_", encoded)
        self.assertNotIn(source["path"], encoded)
        self.assertEqual(before, Path(source["path"]).read_bytes())

    def test_log_adapters_with_synthetic_sources(self):
        codex, claude = self.source("code", "codex"), self.source("claude", "claude")
        at = observer.iso_z(self.now)
        Path(codex["path"]).write_text(json.dumps({"type": "response_item", "timestamp": at, "payload": {"type": "function_call", "name": "apply_patch", "arguments": "SECRET"}}) + "\n", encoding="utf-8")
        Path(claude["path"]).write_text(json.dumps({"type": "assistant", "timestamp": at, "message": {"content": [{"type": "tool_use", "name": "Read", "input": "SECRET"}]}}) + "\n", encoding="utf-8")
        data = monitor.Monitor(self.config([codex, claude])).snapshot(self.now)
        self.assertEqual([a["status"] for a in data["agents"]], ["editing", "reading"])
        self.assertNotIn("SECRET", json.dumps(data))

    def test_stale_and_future_events(self):
        source = self.source()
        for delta, expected in ((-100, "stale"), (400, "unknown")):
            Path(source["path"]).write_text(json.dumps({"status": "checking", "event_at": observer.iso_z(self.now+timedelta(seconds=delta))}))
            data = monitor.Monitor(self.config([source])).snapshot(self.now)
            self.assertEqual(data["agents"][0]["status"], expected)

    def test_missing_corrupt_and_oversized_sources_do_not_hide_others(self):
        good, bad = self.source(), self.source("bad")
        self.write_state(good, "done")
        reader = monitor.Monitor(self.config([good, bad]))
        for content in (None, "{broken", "x" * 65537, '{"status": []}'):
            if content is not None:
                Path(bad["path"]).write_text(content)
            data = reader.snapshot(self.now)
            self.assertEqual(data["agents"][0]["status"], "done")
            self.assertEqual(data["agents"][1]["status"], "unknown")

    def test_config_relative_paths_and_duplicate_rejection(self):
        source = self.source()
        source["path"] = "alice.json"
        conf = self.config([source])
        self.assertEqual(conf["sources"][0]["path"], str(self.root / "alice.json"))
        with self.assertRaises(ValueError):
            self.config([source, dict(source, id="other")])

    def test_config_validation(self):
        for changes in ({"id": "../bad"}, {"id": "a" * 21}, {"kind": "shell"}, {"path": "secret.env"}, {"helpers": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.config([dict(self.source(), **changes)])
        with self.assertRaises(ValueError):
            self.config([self.source(str(i)) for i in range(7)])
        with self.assertRaises(ValueError):
            monitor.validate_config({"version": 2, "sources": []}, self.root)

    def test_config_save_and_reload(self):
        path = self.root / "config.json"
        result = monitor.save_config(path, {"version": 1, "sources": [self.source()]})
        self.assertEqual(monitor.load_config(path), result)

    def test_config_and_output_cannot_overwrite_input(self):
        source = self.source()
        self.write_state(source)
        before = Path(source["path"]).read_bytes()
        with self.assertRaises(ValueError):
            monitor.save_config(Path(source["path"]), self.config([source]))
        source["path"] = str(self.root / "agent-office.json")
        self.write_state(source)
        with self.assertRaises(observer.OutputError):
            monitor.Monitor(self.config([source])).write(Path(source["path"]))
        self.assertEqual(before, (self.root / "alice.json").read_bytes())

    def test_no_sources_does_not_invent_agents(self):
        self.assertEqual(monitor.Monitor(self.config([])).snapshot()["agents"], [])

    def test_headless_roundtrip_from_non_ascii_path(self):
        source = self.source()
        self.write_state(source)
        config_path = self.root / "config.json"
        monitor.save_config(config_path, self.config([source]))
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        result = subprocess.run([sys.executable, str(ROOT / "office.py"), "--config", str(config_path), "--once"], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((self.root / ".runtime" / "agent-office.json").read_text(encoding="utf-8"))
        self.assertEqual(data["agents"][0]["id"], "alice")

    def test_config_not_implicitly_read_from_other_workspace(self):
        self.assertEqual(office.ROOT, ROOT)

    def test_recent_logs_only_metadata(self):
        path = self.root / "session.jsonl"
        path.write_text("PRIVATE_DO_NOT_PARSE")
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected read")):
            logs = office.recent_logs(self.root)
        self.assertEqual(logs[0][1], path)

    def test_sources_unchanged_by_observer(self):
        source = self.source()
        self.write_state(source)
        path = Path(source["path"])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        output = self.root / "agent-office.json"
        monitor.Monitor(self.config([source])).write(output)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_one_writer_per_output_and_lock_released(self):
        output = self.root / 'agent-office.json'
        lock = office.OutputLock(output)
        try:
            with self.assertRaises(ValueError):
                office.OutputLock(output)
        finally:
            lock.close()
        office.OutputLock(output).close()


if __name__ == "__main__":
    unittest.main()
