"""Build native artifacts; all generated files stay under the ignored .local tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installer', action='store_true')
    parser.add_argument('--skip-freeze', action='store_true')
    args = parser.parse_args()
    build = ROOT / '.local' / 'desktop-build'
    dist = ROOT / '.local' / 'desktop-dist'
    release = ROOT / '.local' / 'desktop-release'
    for path in (build, dist, release):
        path.mkdir(parents=True, exist_ok=True)
    os.environ['PYINSTALLER_CONFIG_DIR'] = str(build / 'cache')
    if sys.platform == 'darwin':
        from PIL import Image
        with Image.open(ROOT / 'frontend' / 'static' / 'img' / 'icon.ico') as icon:
            icon.convert('RGBA').resize((1024, 1024)).save(build / 'icon.icns')
    if not args.skip_freeze:
        subprocess.run([sys.executable, '-m', 'PyInstaller', str(ROOT / 'desktop' / 'school_watcher.spec'),
            '--noconfirm', '--distpath', str(dist), '--workpath', str(build / 'work')], cwd=ROOT, check=True)
    bundle = dist / ('School Watcher.app' if sys.platform == 'darwin' else 'SchoolWatcher')
    if sys.platform == 'darwin' and not args.skip_freeze:
        # Preserve Chrome's signed nested bundles and framework symlinks intact.
        # PyInstaller's individual Mach-O rewriting breaks those bundle signatures.
        browser_source = Path(os.environ.get('WATCHER_BUNDLE_BROWSERS', ROOT / '.local' / 'desktop-browsers'))
        shutil.copytree(browser_source, bundle / 'Contents' / 'Resources' / 'browser-runtime', symlinks=True)
        subprocess.run(['codesign', '--force', '--sign', os.environ.get('WATCHER_CODESIGN_IDENTITY') or '-',
                        '--timestamp=none', str(bundle)], check=True)
    # Also run this against the finished tree, since dependencies have their own hooks.
    forbidden = [p for p in bundle.rglob('*') if p.is_file() and (
        '.local' in p.relative_to(bundle).parts or 'promo-web' in p.parts or p.name in ('.env', '.field-key', 'browser-service.token')
        or p.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.mp4', '.webm', '.wav', '.mp3'))]
    if forbidden:
        raise RuntimeError(f'Private or unrelated files found in bundle: {forbidden[:5]}')
    if sys.platform == 'win32':
        name = f'School-Watcher-{VERSION}-windows-x64-portable'
        archive = shutil.make_archive(str(release / name), 'zip', root_dir=dist, base_dir='SchoolWatcher')
        if args.installer:
            iscc = shutil.which('iscc') or str(Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Inno Setup 6' / 'ISCC.exe')
            if not Path(iscc).is_file():
                raise RuntimeError('Inno Setup 6 is required for --installer. Portable ZIP is already built.')
            command = [iscc, f'/DAppVersion={VERSION}', f'/DAppSource={bundle}', f'/DOutputPath={release}']
            bootstrapper = os.environ.get('WATCHER_WEBVIEW_BOOTSTRAPPER')
            if not bootstrapper or not Path(bootstrapper).is_file():
                raise RuntimeError('Provide the official Microsoft WebView2 bootstrapper for the Windows installer.')
            command.append(f'/DWebViewBootstrapper={bootstrapper}')
            subprocess.run([*command, str(ROOT / 'desktop' / 'windows.iss')], check=True)
    elif sys.platform == 'darwin':
        arch = 'arm64' if platform.machine() == 'arm64' else 'x64'
        # hdiutil accepts the staging directory and preserves the .app symlinks.
        stage = build / f'dmg-{arch}'
        stage.mkdir(exist_ok=True)
        destination = stage / 'School Watcher.app'
        if destination.exists():
            raise RuntimeError('Use a fresh build directory for DMG staging.')
        shutil.copytree(bundle, destination, symlinks=True)
        (stage / 'Applications').symlink_to('/Applications', target_is_directory=True)
        subprocess.run(['hdiutil', 'create', '-volname', 'School Watcher', '-srcfolder', str(stage),
            '-ov', '-format', 'UDZO', str(release / f'School-Watcher-{VERSION}-macos-{arch}.dmg')], check=True)
    else:
        raise RuntimeError('Build this application on Windows or macOS.')
    entries = []
    for artifact in sorted(release.iterdir()):
        if artifact.suffix in ('.exe', '.zip', '.dmg'):
            with artifact.open('rb') as source:
                entries.append(f'{hashlib.file_digest(source, "sha256").hexdigest()}  {artifact.name}')
    (release / 'SHA256SUMS.txt').write_text('\n'.join(entries) + '\n', encoding='utf-8')
    print(json.dumps({'version': VERSION, 'release': str(release), 'assets': len(entries)}), flush=True)


if __name__ == '__main__':
    main()
