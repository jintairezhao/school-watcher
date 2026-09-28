# Only explicit application resources are shipped. Never collect the repository root.
from pathlib import Path
from importlib import metadata
import os
import runpy
import sys

root = Path(SPECPATH).parent
version = runpy.run_path(str(root / 'desktop' / '__init__.py'))['VERSION']

application_data = runpy.run_path(str(root / 'desktop/resources.py'))['application_data']
datas = application_data(root, root / '.local/desktop-build')
hidden = ['desktop.window', 'desktop.updater', 'desktop.browser', 'scripts.maintenance.migrate_safely', 'logging.config', 'ijson.backends.yajl2_c', 'ijson.backends.python']
for source in (root / 'backend').rglob('*.py'):
    name = '.'.join(source.relative_to(root).with_suffix('').parts)
    hidden.append(name.removesuffix('.__init__'))

a = Analysis([str(root / 'desktop' / 'entry.py')], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=hidden, hookspath=[], runtime_hooks=[],
    excludes=['PyQt5','PyQt6','PySide2','PySide6','tkinter','matplotlib','numpy','scipy','pandas','pytest'],
    noarchive=False)
# Keep the actual distributions' copyright and license files with the executable.
modules = {entry[0].split('.')[0] for entry in a.pure}
modules.update(entry[0].replace('\\', '/').split('/')[0].split('.')[0] for entry in a.binaries)
mapping = metadata.packages_distributions()
distributions = {name for module in modules for name in mapping.get(module, [])}
for name in sorted(distributions):
    distribution = metadata.distribution(name)
    for entry in distribution.files or []:
        if any(word in entry.name.lower() for word in ('license', 'licence', 'copying', 'notice', 'copyright')):
            source = Path(distribution.locate_file(entry))
            if source.is_file():
                destination = 'third-party-licenses/' + name + '/' + str(entry).replace('../', '').replace('\\', '/')
                a.datas.append((destination, str(source), 'DATA'))
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
