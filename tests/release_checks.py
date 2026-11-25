"""Verify the Windows archive in a new folder with no installed-app dependencies."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    archive = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT/'dist/Agent-Office-Windows-x64.zip'
    with tempfile.TemporaryDirectory(prefix='office portable тест ') as directory:
        root = Path(directory).resolve()
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            assert not any(part in {'.git', '.runtime', '.venv', 'config.json'} for name in names for part in Path(name).parts)
            for name in names:
                assert (root/name).resolve().is_relative_to(root), name
            package.extractall(root)
        app = root/'Agent Office'
        assert (app/'vendor/three.min.js').is_file()
        assert (app/'licenses/PYTHON-LICENSE.txt').is_file()
        status = app/'synthetic-state.json'
        status.write_text(json.dumps({'status': 'done', 'event_at': '2026-11-25T12:00:00Z', 'prompt': 'PRIVATE_SENTINEL'}))
        config = app/'config.json'
        config.write_text(json.dumps({'version': 1, 'sources': [{'id': 'test', 'name': 'Portable test', 'kind': 'state', 'path': str(status)}]}))
        executable = app/'Agent Office.exe'
        result = subprocess.run([str(executable), '--config', str(config), '--once'], cwd=root, timeout=30)
        assert result.returncode == 0, result.returncode
        snapshot = (app/'.runtime/agent-office.json').read_text(encoding='utf-8')
        assert 'PRIVATE_SENTINEL' not in snapshot
        assert json.loads(snapshot)['agents'][0]['status'] == 'done'
        # Exercise frozen Tk import/startup without using the user's actual logs.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen([str(executable), '--config', str(config)], cwd=root, startupinfo=startup)
        try:
            time.sleep(3)
            assert process.poll() is None, 'Frozen setup window exited unexpectedly'
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'), headless=True)
            context = browser.new_context(offline=True)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto((app/'index.html').as_uri()+'#demo')
            page.wait_for_function('window.agentOffice && window.agentOffice.getState().three && window.agentOffice.getState().connection === "demo"')
            assert not errors, errors
            browser.close()
    print('Portable ZIP: frozen observer, Tk startup, privacy and offline 3D passed in a separate folder.')


if __name__ == '__main__':
    main()
