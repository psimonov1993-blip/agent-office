"""Exercise the actual Tk widgets using synthetic data; needs a desktop display."""
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import office
import observer


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def main():
    with tempfile.TemporaryDirectory(prefix='office GUI тест ') as directory:
        root = Path(directory)
        source = root/'custom.json'
        source.write_text(json.dumps({'status': 'editing', 'event_at': observer.iso_z(observer.utc_now())}))
        window = tk.Tk()
        window.withdraw()
        errors = []
        window.report_callback_exception = lambda *args: errors.append(args)
        def button(text):
            return next(w for w in descendants(window) if isinstance(w, ttk.Button) and w.cget('text') == text)
        def exercise():
            try:
                button('Другой агент (JSON)').invoke()
                saved = json.loads((root/'config.json').read_text(encoding='utf-8'))
                assert saved['sources'][0]['name'] == 'Тестовый агент'
                button('Запустить офис').invoke()
                assert (root/'.runtime/agent-office.json').is_file()
                assert str(button('Запустить офис').cget('state')) == 'disabled'
                data = json.loads((root/'.runtime/agent-office.json').read_text(encoding='utf-8'))
                assert data['agents'][0]['status'] == 'editing'
                button('Остановить наблюдение').invoke()
                assert str(button('Запустить офис').cget('state')) == 'normal'
                office.OutputLock(root/'.runtime/agent-office.json').close()
                button('Сначала посмотреть демо — без настройки').invoke()
                browser.assert_called_with(True)
            except BaseException as exc:
                errors.append(exc)
            finally:
                window.destroy()
        with patch.object(tk, 'Tk', return_value=window), \
             patch.object(filedialog, 'askopenfilename', return_value=str(source)), \
             patch.object(simpledialog, 'askstring', return_value='Тестовый агент'), \
             patch.object(messagebox, 'showerror', side_effect=lambda *args: errors.append(args)), \
             patch.object(office, 'open_office') as browser:
            window.after(100, exercise)
            office.graphical(root/'config.json')
        if errors:
            raise AssertionError(errors)
        print('Tk setup: add, save, start, stop, lock release and demo passed.')


if __name__ == '__main__':
    main()
