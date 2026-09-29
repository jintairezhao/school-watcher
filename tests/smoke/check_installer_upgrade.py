"""Compile tiny Windows fixtures and upgrade them under a unique test-only AppId."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def check():
    import winreg
    from desktop.runtime import stop_process
    compiler = os.environ.get('WATCHER_ISCC') or shutil.which('iscc') or str(ROOT / '.local/inno/ISCC.exe')
    csc = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    output = (ROOT / '.local/onboarding-checks/installer-upgrade' / uuid.uuid4().hex).resolve()
    assert output.is_relative_to(ROOT / '.local/onboarding-checks')
    output.mkdir(parents=True)
    source = output / 'source'; source.mkdir()
    program = output / '自选程序位置'
    token = str(uuid.uuid4()).upper()
    app_id = '{{' + token + '}'
    location_key = 'Software\\SchoolWatcherUpgradeTest-' + token
    uninstall_key = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{' + token + '}_is1'
    calls = output / 'calls.txt'
    env = {**os.environ, 'WATCHER_INSTALL_FIXTURE_LOG': str(calls)}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    values = {field: str(output / name) for field, name in (
        ('DataDirectory', '自选数据'), ('CacheDirectory', '自选缓存'),
        ('BackupDirectory', '自选备份'), ('DownloadDirectory', '自选下载'))}
    for path in values.values():
        Path(path).mkdir()
        (Path(path) / 'keep.txt').write_bytes(b'personal fixture must survive')
    before = {str(p): p.read_bytes() for p in output.glob('自选*/keep.txt')}
    fixture = output / 'Fixture.cs'
    fixture.write_text('''using System; using System.IO;
class Fixture { static void Main(string[] args) {
  string marker = Environment.GetEnvironmentVariable("WATCHER_INSTALL_PARENT_DONE");
  if (args.Length == 0 && !String.IsNullOrEmpty(marker) && !File.Exists(marker)) {
    File.AppendAllText(Environment.GetEnvironmentVariable("WATCHER_INSTALL_FIXTURE_LOG"), "EARLY LAUNCH\\n");
  }
  File.AppendAllText(Environment.GetEnvironmentVariable("WATCHER_INSTALL_FIXTURE_LOG"),
    args.Length == 0 ? "LAUNCH\\n" : String.Join("\\t", args) + "\\n");
}}''', encoding='utf-8')
    subprocess.run([str(csc), '/nologo', '/target:winexe', '/out:' + str(source / 'SchoolWatcher.exe'),
                    str(fixture)], check=True, capture_output=True)

    def compile_installer(script, version):
        script = script.replace('LicenseFile=..\\LICENSE', 'LicenseFile=' + str(ROOT / 'LICENSE'))
        script = script.replace('languages\\ChineseSimplified.isl', str(ROOT / 'desktop/languages/ChineseSimplified.isl'))
        script = script.replace("'Software\\SchoolWatcher'", "'" + location_key + "'")
        script = script.replace('DefaultGroupName=School Watcher', 'DefaultGroupName=School Watcher Upgrade Test ' + token)
        script = script.replace('Name: "{autodesktop}\\School Watcher"', 'Name: "{autodesktop}\\School Watcher Upgrade Test ' + token + '"')
        path = output / (version + '.iss')
        path.write_text(script, encoding='utf-8')
        with (output / (version + '-compile.log')).open('w') as log:
            subprocess.run([compiler, '/DAppVersion=' + version, '/DAppIdentifier=' + app_id,
                '/DLocationRegistry=' + location_key, '/DAppSource=' + str(source),
                '/DOutputPath=' + str(output), str(path)], stdout=log, stderr=log, check=True, timeout=60)
        return output / ('School-Watcher-' + version + '-windows-x64-setup.exe')

    def run(command, timeout=60, success=True):
        proc = subprocess.Popen(list(map(str, command)), startupinfo=startup, env=env)
        try:
            code = proc.wait(timeout=timeout)
            assert (code == 0) == success, (code, command)
        finally:
            if proc.poll() is None:
                stop_process(proc)

    def saved_paths():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, location_key) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in values}

    def previous_tasks():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, 'Inno Setup: Selected Tasks')[0]

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, location_key) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        # Seed with the previous release's installer script, not the new update behavior.
        legacy = (ROOT / 'tests/fixtures/windows_installer_v012.iss').read_text(encoding='utf-8')
        (source / 'version.txt').write_text('before')
        old = compile_installer(legacy, '9.0.0')
        run([old, '/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS=desktopicon',
             '/DIR=' + str(program), '/LOG=' + str(output / 'first-install.log')])
        assert calls.read_text().startswith('--configure-locations')
        tasks = previous_tasks()
        assert tasks == 'desktopicon', tasks
        calls.write_text('')
        (source / 'version.txt').write_text('after')
        new = compile_installer((ROOT / 'desktop/windows.iss').read_text(encoding='utf-8'), '9.0.1')
        parent_done = output / 'previous-app-exited'
        env['WATCHER_INSTALL_PARENT_DONE'] = str(parent_done)
        parent = subprocess.Popen([sys.executable, '-c',
            'import pathlib,sys,time; time.sleep(3); pathlib.Path(sys.argv[1]).touch()', str(parent_done)],
            creationflags=subprocess.CREATE_NO_WINDOW)
        # No /DIR: the installer itself must find the old custom program directory.
        try:
            run([new, '/UPDATE', '/SILENT', '/SP-', '/NORESTART', '/WATCHERPID=' + str(parent.pid),
                 '/LOG=' + str(output / 'upgrade.log')])
            assert parent.poll() == 0
        finally:
            if parent.poll() is None:
                parent.terminate(); parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while 'LAUNCH' not in calls.read_text() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (program / 'version.txt').read_text() == 'after'
        assert calls.read_text().splitlines() == ['LAUNCH'], calls.read_text()
        assert saved_paths() == values
        calls.write_text('')
        missing = output / 'not-an-installed-program'
        run([new, '/UPDATE', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART',
             '/DIR=' + str(missing), '/LOG=' + str(output / 'refused-update.log')], success=False)
        assert not (missing / 'SchoolWatcher.exe').exists()
        assert calls.read_text() == ''
        assert saved_paths() == values
        assert previous_tasks() == tasks
        assert all(Path(path).read_bytes() == content for path, content in before.items())
        calls.write_text('')
        # Double-clicking the installer should also bypass the setup pages on an existing install.
        run([new, '/SP-', '/NORESTART', '/LOG=' + str(output / 'manual-upgrade.log')])
        assert calls.read_text().splitlines() == ['LAUNCH'], calls.read_text()
        assert saved_paths() == values
        result = {'program_path_inherited': True, 'all_data_paths_retained': True,
                  'personal_files_unchanged': True, 'shortcut_selection_retained': True,
                  'no_location_configuration_on_update': True, 'automatic_restart': True,
                  'manual_download_skips_wizard': True, 'waits_for_previous_process': True,
                  'missing_original_installation_refused': True}
        (output / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        print(json.dumps(result), flush=True)
    finally:
        uninstaller = (program / 'unins000.exe').resolve()
        assert uninstaller.is_relative_to(output)
        if uninstaller.exists():
            run([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'])
        assert location_key.startswith('Software\\SchoolWatcherUpgradeTest-')
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, location_key)
        except FileNotFoundError:
            pass


if __name__ == '__main__':
    check()
