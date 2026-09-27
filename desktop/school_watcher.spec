# Only explicit application resources are shipped. Never collect the repository root.
from pathlib import Path
import os
import runpy
import sys

root = Path(SPECPATH).parent
version = runpy.run_path(str(root / 'desktop' / '__init__.py'))['VERSION']
browser_path = Path(os.environ.get('WATCHER_BUNDLE_BROWSERS', root / '.local' / 'desktop-browsers'))
if not any(browser_path.glob('chromium-*')):
    raise RuntimeError('Install Chromium into WATCHER_BUNDLE_BROWSERS before building.')

datas = []
for folder in ('frontend', 'config', 'migrations', 'licenses', 'desktop/ui'):
    for source in (root / folder).rglob('*'):
        if source.is_file() and '__pycache__' not in source.parts and source.suffix not in ('.pyc', '.pyo'):
            datas.append((str(source), str(source.parent.relative_to(root))))
for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
    datas.append((str(root / name), '.'))
datas.append((str(browser_path), 'browser-runtime'))
hidden = ['desktop.window', 'desktop.updater', 'scripts.maintenance.migrate_safely']
for source in (root / 'backend').rglob('*.py'):
    name = '.'.join(source.relative_to(root).with_suffix('').parts)
    hidden.append(name.removesuffix('.__init__'))

a = Analysis([str(root / 'desktop' / 'entry.py')], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=hidden, hookspath=[], runtime_hooks=[],
    excludes=['PyQt5','PyQt6','PySide2','PySide6','tkinter','matplotlib','numpy','scipy','pandas','pytest'],
    noarchive=False)
pyz = PYZ(a.pure)
icon = root / 'frontend' / 'static' / 'img' / 'icon.ico'
if sys.platform == 'darwin':
    icon = root / '.local' / 'desktop-build' / 'icon.icns'
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SchoolWatcher', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    icon=str(icon), codesign_identity=os.environ.get('WATCHER_CODESIGN_IDENTITY') or None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SchoolWatcher')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='School Watcher.app', icon=str(icon),
        bundle_identifier='io.github.jintairezhao.schoolwatcher',
        info_plist={'CFBundleShortVersionString': version, 'CFBundleVersion': version,
                    'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '15.0',
                    'NSRequiresAquaSystemAppearance': False})
