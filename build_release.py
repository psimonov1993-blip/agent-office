"""Create an allowlisted portable Windows release, without local configuration."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
PUBLIC_FILES = [
    'index.html', 'guide.html', 'README.md', 'README.en.md', 'LICENSE',
    'THIRD_PARTY_NOTICES.md', 'config.example.json',
    'docs/INTEGRATIONS.md', 'docs/office-demo.png', 'docs/BUILD.md', 'CONTRIBUTING.md',
    'vendor/three.min.js', 'vendor/THREE-LICENSE.txt', 'vendor/TCL-LICENSE.txt', 'examples/agent-state.json',
]


def main():
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build this Windows x64 archive on Windows x64.')
    destination = ROOT / 'dist' / 'Agent Office'
    archive = ROOT / 'dist' / 'Agent-Office-Windows-x64.zip'
    if destination.exists() or archive.exists():
        raise SystemExit('Build destination already exists. Use a fresh checkout or move the previous build.')
    for relative in PUBLIC_FILES:
        if not (ROOT / relative).is_file():
            raise SystemExit('Missing release input: ' + relative)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--onedir', '--windowed', '--noupx',
                    '--name', 'Agent Office', '--distpath', str(ROOT/'dist'),
                    '--workpath', str(ROOT/'build'), '--specpath', str(ROOT/'build'),
                    str(ROOT/'office.py')], cwd=ROOT, check=True)
    for relative in PUBLIC_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    licenses = destination / 'licenses'
    licenses.mkdir()
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise SystemExit('Python license missing; refusing to package.')
    shutil.copy2(python_license, licenses / 'PYTHON-LICENSE.txt')
    dist = importlib.metadata.distribution('pyinstaller')
    copied = 0
    for item in dist.files or []:
        if 'licenses' in item.parts and item.name.lower().startswith(('copying', 'license')):
            shutil.copy2(dist.locate_file(item), licenses / ('PYINSTALLER-' + item.name))
            copied += 1
    if not copied:
        raise SystemExit('PyInstaller licenses missing; refusing to package.')
    tcl = Path(sys.base_prefix) / 'tcl'
    for name in ('tcl8.6', 'tk8.6'):
        candidates = [tcl/name/'license.terms', tcl/name/'licenseTerms']
        if name == 'tcl8.6':
            candidates.append(ROOT/'vendor/TCL-LICENSE.txt')
        license_file = next((p for p in candidates if p.is_file()), None)
        if not license_file:
            raise SystemExit('Tcl/Tk license missing: ' + name)
        shutil.copy2(license_file, licenses/(name+'-LICENSE.txt'))
    metadata = {'python': platform.python_version(), 'pyinstaller': importlib.metadata.version('pyinstaller'),
                'platform': platform.system(), 'architecture': platform.machine()}
    (destination/'BUILD-INFO.json').write_text(json.dumps(metadata, indent=2)+'\n', encoding='utf-8')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                out.write(path, 'Agent Office/' + path.relative_to(destination).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n', encoding='ascii')
    print(archive)


if __name__ == '__main__':
    main()
