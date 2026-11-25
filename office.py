#!/usr/bin/env python3
"""Agent Office launcher: local files, no server, no agent execution."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import webbrowser

import monitor
import observer

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


class OutputLock:
    """One writer per output folder; the OS releases the lock after a crash."""
    def __init__(self, output):
        path = Path(output).with_suffix(".lock")
        if path.is_symlink():
            raise ValueError("Файл блокировки не должен быть ссылкой.")
        self.stream = path.open("a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ValueError("Наблюдение уже запущено для этой папки. Используйте прежнее окно.") from exc

    def close(self):
        self.stream.close()


def open_office(demo=False):
    uri = (ROOT / "index.html").as_uri() + ("#demo" if demo else "")
    # Prefer browsers that support a user-selected live file handle.
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.extend(Path(base) / rel for rel in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"))
    elif sys.platform == "darwin":
        candidates.extend(Path(p) for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    else:
        candidates.extend(Path(p) for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge") if (p := shutil.which(name)))
    for executable in candidates:
        if executable.is_file():
            try:
                subprocess.Popen([str(executable), uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    if not webbrowser.open(uri):
        raise OSError("Не удалось открыть браузер. Откройте index.html вручную в Chrome или Edge.")


def source_home(kind):
    if kind == "codex":
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"


def recent_logs(directory):
    """List filenames and mtimes only. Never read session contents here."""
    files = []
    for index, path in enumerate(Path(directory).rglob("*.jsonl")):
        if index >= 10000:
            break
        try:
            if path.is_file():
                files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return sorted(files, key=lambda pair: pair[0], reverse=True)[:30]


def make_output(config_path, config):
    directory = Path(config_path).parent / ".runtime"
    directory.mkdir(exist_ok=True)
    return observer.validate_output(directory / "agent-office.json", [s["path"] for s in config["sources"]])


def headless(args):
    config = monitor.load_config(args.config)
    if not config["sources"]:
        raise ValueError("Добавьте хотя бы один источник в конфигурацию.")
    output = make_output(args.config, config)
    lock = OutputLock(output)
    reader = monitor.Monitor(config)
    duration = max(1, min(args.duration, 28800))
    deadline = time.monotonic() + duration
    print("Agent Office — только чтение. Ctrl+C для остановки.", flush=True)
    print("Подключите этот файл в index.html: " + str(output), flush=True)
    try:
        while True:
            snapshot = reader.write(output)
            if args.once:
                print("Снимок записан. Источники: " + str(snapshot["observer"]["sources"]), flush=True)
                return 0
            if time.monotonic() + 2 >= deadline:
                return 0
            time.sleep(2)
    finally:
        lock.close()


def graphical(config_path):
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except ImportError as exc:
        raise RuntimeError("Нужен Python с Tkinter. На Linux установите python3-tk или используйте --headless.") from exc

    class App:
        def __init__(self, window):
            self.window, self.reader, self.timer = window, None, None
            self.output = None
            self.lock = None
            self.config = {"version": 1, "sources": []}
            self.config_error = None
            if config_path.exists():
                try:
                    self.config = monitor.load_config(config_path)
                except (OSError, ValueError) as exc:
                    self.config_error = str(exc)
            window.title("Agent Office · подключение агентов")
            window.geometry("900x690")
            window.minsize(700, 610)
            window.protocol("WM_DELETE_WINDOW", self.close)
            frame = ttk.Frame(window, padding=22)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="Ваш офис агентов", font=("Segoe UI", 22, "bold")).pack(anchor="w")
            ttk.Label(frame, text="1. Добавьте журнал агента.  2. Запустите наблюдение.  3. Подключите файл в браузере.", wraplength=820).pack(anchor="w", pady=(8, 14))
            ttk.Button(frame, text="Сначала посмотреть демо — без настройки", command=lambda: self.open(True)).pack(anchor="w", pady=(0, 14))
            self.tree = ttk.Treeview(frame, columns=("name", "kind", "task"), show="headings", height=6, selectmode="browse")
            for key, title, width in (("name", "Агент", 180), ("kind", "Источник", 100), ("task", "Задача (ваша подпись)", 410)):
                self.tree.heading(key, text=title)
                self.tree.column(key, width=width)
            self.tree.pack(fill="x")
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=10)
            self.edit_buttons = []
            for title, kind in (("Добавить Codex", "codex"), ("Добавить Claude", "claude"), ("Другой агент (JSON)", "state")):
                button = ttk.Button(row, text=title, command=lambda k=kind: self.add(k))
                button.pack(side="left", padx=(0, 6))
                self.edit_buttons.append(button)
            for title, callback in (("Изменить", self.edit), ("Удалить", self.remove)):
                button = ttk.Button(row, text=title, command=callback)
                button.pack(side="left", padx=(0, 6))
                self.edit_buttons.append(button)
            self.tree.bind("<<TreeviewSelect>>", self.show_path)
            self.path_label = ttk.Label(frame, text="Поддерживаются до 6 агентов. Журналы выбираете только вы.", wraplength=820)
            self.path_label.pack(anchor="w", pady=(0, 12))
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=8)
            self.start_button = ttk.Button(row, text="Запустить офис", command=self.start)
            self.start_button.pack(side="left", padx=(0, 8))
            self.stop_button = ttk.Button(row, text="Остановить наблюдение", command=self.stop, state="disabled")
            self.stop_button.pack(side="left")
            self.status = tk.StringVar(value="Готово к настройке. Задания агентам давайте в их обычных приложениях.")
            ttk.Label(frame, textvariable=self.status, wraplength=820).pack(anchor="w", pady=10)
            ttk.Separator(frame).pack(fill="x", pady=8)
            ttk.Label(frame, text="В браузере: «Подключить живые данные» → выберите файл ниже.", wraplength=820).pack(anchor="w")
            self.output_var = tk.StringVar(value=str(config_path.parent / ".runtime" / "agent-office.json"))
            ttk.Entry(frame, textvariable=self.output_var, state="readonly").pack(fill="x", pady=8)
            ttk.Button(frame, text="Скопировать путь к файлу", command=self.copy_path).pack(anchor="w")
            ttk.Label(frame, text="Оставьте это окно открытым: оно обновляет снимок каждые 2 секунды.\nНаблюдение остановится через 2 часа; его можно запустить снова.\nВ новую сессию агента выберите новый журнал. Пути сохраняются только в config.json.", wraplength=820).pack(anchor="w", pady=(14, 0))
            self.render()
            if self.config_error:
                messagebox.showerror("Конфигурация не прочитана", self.config_error + "\nИсправьте config.json или сохраните новую настройку через мастер.")

        def render(self):
            for item in self.tree.get_children():
                self.tree.delete(item)
            for s in self.config["sources"]:
                self.tree.insert("", "end", iid=s["id"], values=(s["name"], s["kind"], s["task"]))

        def selected(self):
            ids = self.tree.selection()
            return next((s for s in self.config["sources"] if ids and s["id"] == ids[0]), None)

        def show_path(self, _event=None):
            source = self.selected()
            if source:
                self.path_label.config(text=source["path"])

        def pick(self, kind):
            initial = source_home(kind) if kind != "state" else ROOT
            files = recent_logs(initial) if kind != "state" and initial.is_dir() else []
            if not files:
                return filedialog.askopenfilename(parent=self.window, title="Выберите журнал .jsonl" if kind != "state" else "Выберите JSON состояния", initialdir=str(initial if initial.exists() else Path.home()), filetypes=[("JSON", "*.json" if kind == "state" else "*.jsonl")])
            dialog = tk.Toplevel(self.window)
            dialog.title("Недавние журналы — выберите нужную сессию")
            dialog.geometry("940x420")
            ttk.Label(dialog, text="Показаны имена 30 недавно изменённых файлов; содержимое здесь не читается.", padding=12).pack(anchor="w")
            listing = ttk.Treeview(dialog, columns=("time", "file"), show="headings", selectmode="browse")
            listing.heading("time", text="Изменён")
            listing.heading("file", text="Путь внутри " + str(initial))
            listing.column("time", width=145, stretch=False)
            listing.column("file", width=740)
            listing.pack(fill="both", expand=True, padx=12)
            for i, (mtime, path) in enumerate(files):
                listing.insert("", "end", iid=str(i), values=(time.strftime("%d.%m %H:%M:%S", time.localtime(mtime)), str(path.relative_to(initial))))
            result = []
            def choose():
                ids = listing.selection()
                if ids:
                    result.append(str(files[int(ids[0])][1]))
                    dialog.destroy()
            def browse():
                path = filedialog.askopenfilename(parent=dialog, initialdir=str(initial), filetypes=[("Session log", "*.jsonl")])
                if path:
                    result.append(path)
                    dialog.destroy()
            row = ttk.Frame(dialog, padding=12)
            row.pack(fill="x")
            ttk.Button(row, text="Подключить выбранный", command=choose).pack(side="left", padx=(0, 8))
            ttk.Button(row, text="Выбрать другой файл", command=browse).pack(side="left")
            dialog.transient(self.window)
            dialog.grab_set()
            self.window.wait_window(dialog)
            return result[0] if result else None

        def persist(self, candidate):
            try:
                self.config = monitor.save_config(config_path, candidate)
                self.render()
                return True
            except (OSError, ValueError) as exc:
                messagebox.showerror("Не удалось сохранить", str(exc))
                return False

        def add(self, kind):
            if len(self.config["sources"]) >= 6:
                messagebox.showinfo("Офис заполнен", "В офисе до 6 мест. Удалите один источник, чтобы добавить новый.")
                return
            path = self.pick(kind)
            if not path:
                return
            name = simpledialog.askstring("Имя в офисе", "Как назвать агента?", initialvalue={"codex": "Codex", "claude": "Claude", "state": "Мой агент"}[kind], parent=self.window)
            if name is None:
                return
            ids = {s["id"] for s in self.config["sources"]}
            sid = next("agent-" + str(i) for i in range(1, 100) if "agent-" + str(i) not in ids)
            source = {"id": sid, "name": name, "kind": kind, "path": path, "helpers": True}
            self.persist({"version": 1, "sources": self.config["sources"] + [source]})

        def edit(self):
            source = self.selected()
            if not source:
                return
            name = simpledialog.askstring("Имя", "Имя агента в офисе:", initialvalue=source["name"], parent=self.window)
            if name is None:
                return
            task = simpledialog.askstring("Задача", "Ваша подпись к задаче (она не отправляется агенту):", initialvalue=source["task"], parent=self.window)
            if task is None:
                return
            self.persist({"version": 1, "sources": [dict(s, name=name, task=task) if s["id"] == source["id"] else s for s in self.config["sources"]]})

        def remove(self):
            source = self.selected()
            if source:
                self.persist({"version": 1, "sources": [s for s in self.config["sources"] if s["id"] != source["id"]]})

        def copy_path(self):
            self.window.clipboard_clear()
            self.window.clipboard_append(self.output_var.get())

        def open(self, demo=False):
            try:
                open_office(demo)
            except OSError as exc:
                messagebox.showerror("Открытие браузера", str(exc))

        def start(self):
            if self.reader:
                return
            if not self.config["sources"]:
                messagebox.showinfo("Добавьте агента", "Сначала выберите хотя бы один журнал или откройте демо.")
                return
            try:
                self.output = make_output(config_path, self.config)
                self.lock = OutputLock(self.output)
                self.reader = monitor.Monitor(self.config)
                self.deadline = time.monotonic() + 7200
                self.reader.write(self.output)
            except (OSError, ValueError, observer.OutputError) as exc:
                self.reader = None
                if self.lock:
                    self.lock.close()
                    self.lock = None
                messagebox.showerror("Не удалось запустить", str(exc))
                return
            self.start_button.config(state="disabled")
            self.stop_button.config(state="normal")
            for button in self.edit_buttons:
                button.config(state="disabled")
            self.tick()
            self.open()

        def tick(self):
            self.timer = None
            if not self.reader:
                return
            if time.monotonic() >= self.deadline:
                self.stop()
                return
            try:
                snapshot = self.reader.write(self.output)
                states = snapshot["observer"]["sources"]
                issues = [s["name"] + ": " + {"missing": "файл не найден", "empty": "нет распознанных событий", "unreadable": "файл не прочитан"}.get(states[s["id"]], states[s["id"]]) for s in self.config["sources"] if states[s["id"]] != "ok"]
                self.status.set("Наблюдение работает. " + ("; ".join(issues) if issues else "Снимок обновлён; источники читаются."))
            except (OSError, ValueError, observer.OutputError) as exc:
                self.status.set("Снимок не записан: " + str(exc))
            self.timer = self.window.after(2000, self.tick)

        def stop(self):
            if self.timer:
                self.window.after_cancel(self.timer)
                self.timer = None
            self.reader = None
            if self.lock:
                self.lock.close()
                self.lock = None
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            for button in self.edit_buttons:
                button.config(state="normal")
            self.status.set("Наблюдение остановлено. В браузере данные устареют через 15 секунд.")

        def close(self):
            self.stop()
            self.window.destroy()

    window = tk.Tk()
    App(window)
    window.mainloop()
    return 0


def main(argv=None):
    observer._configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--headless", action="store_true", help="Наблюдение без окна настройки")
    parser.add_argument("--once", action="store_true", help="Записать один снимок и завершиться")
    parser.add_argument("--duration", type=int, default=7200, help="Длительность headless-наблюдения, до 28800 секунд")
    parser.add_argument("--demo", action="store_true", help="Открыть демо без настройки")
    args = parser.parse_args(argv)
    args.config = args.config.expanduser().resolve()
    try:
        if args.demo:
            open_office(True)
            return 0
        return headless(args) if args.headless or args.once else graphical(args.config)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError, observer.OutputError) as exc:
        print("Agent Office: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
