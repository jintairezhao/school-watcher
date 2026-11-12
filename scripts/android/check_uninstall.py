"""Destructive acceptance check on a dedicated, rootable Android emulator only.

Seeds labelled test data, fetches one public university page to create an actual
notification, uninstalls normally (never -k), and verifies a fresh reinstall.
Never point this at an emulator whose School Watcher data you need to keep.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET

PACKAGE = "cn.schoolwatcher.app"
MARKER = "uninstall-acceptance-only"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-reset", action="store_true", help="Allow replacing this test emulator's app data")
    args = parser.parse_args()

    def adb(*parts, binary=False, check=True):
        result = subprocess.run([args.adb, "-s", args.serial, *parts], capture_output=True, check=check, timeout=45)
        if not check:
            return result
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

    if not args.allow_reset or not args.serial.startswith("emulator-"):
        parser.error("Requires --allow-reset and an explicit emulator serial; physical devices are refused")
    if adb("shell", "getprop", "ro.kernel.qemu").strip() != "1":
        parser.error("Target is not an Android emulator")
    adb("root")
    adb("wait-for-device")
    assert adb("shell", "id", "-u").strip() == "0", "A rootable test image is required to inspect removed private directories"
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []

    def record(message):
        checks.append(message)
        print(message, flush=True)

    def launch():
        adb("shell", "am", "start", "-W", "-n", PACKAGE + "/.MainActivity")

    def stop():
        adb("shell", "am", "force-stop", PACKAGE)

    def exists(path):
        return adb("shell", "test", "-e", path, check=False).returncode == 0

    def jobs():
        dump = adb("shell", "dumpsys", "jobscheduler")
        # Completed-job diagnostics are OS history, not registered/runnable work.
        assert "Recently completed jobs:" in dump, "Unrecognized JobScheduler dump; update this check for the target OS"
        dump = dump.split("Recently completed jobs:", 1)[0]
        return re.findall(r"^\s+Service: " + re.escape(PACKAGE) + r"/[^\r\n]+", dump, re.M)

    def notifications():
        return [line for line in adb("shell", "cmd", "notification", "list").splitlines() if "|" + PACKAGE + "|" in line]

    def private_file(relative, data):
        # adb's Windows stdin can truncate binary data. Use the binary sync protocol,
        # then copy as the app UID to preserve its SELinux storage label.
        assert re.fullmatch(r"[a-zA-Z0-9_./-]+", relative) and ".." not in relative
        fixture = args.output / "fixture.bin"
        fixture.write_bytes(data)
        staging = "/data/local/tmp/" + PACKAGE + "-uninstall-test-fixture"
        try:
            adb("push", str(fixture), staging)
            adb("shell", "chmod", "644", staging)
            adb("shell", "run-as", PACKAGE, "cp", staging, relative)
        finally:
            # This temporary test-tool fixture is not produced by the application.
            adb("shell", "rm", "-f", staging)
        assert adb("exec-out", "run-as", PACKAGE, "cat", relative, binary=True) == data, "Fixture transfer changed data"

    adb("uninstall", PACKAGE, check=False)
    adb("install", str(args.apk.resolve()))
    launch()
    time.sleep(1)
    stop()
    database = args.output / "before.db"
    database.write_bytes(adb("exec-out", "run-as", PACKAGE, "cat", "databases/watcher.db", binary=True))
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM refresh_items")
        db.execute("DELETE FROM refresh_round")
        db.execute("DELETE FROM notices")
        db.execute("DELETE FROM sources")
        db.execute("INSERT INTO sources(id,school,name,url,enabled) VALUES(1,'卸载验收（测试数据）','教务通知','https://www.cupk.edu.cn/jwb/tzgg/',1)")
        db.execute("INSERT INTO notices(id,source_id,title,url,date,body,is_read,starred,created) VALUES(1,1,'【验收数据】卸载后应删除','https://example.invalid/uninstall','2026-10-03',?,1,1,1)", (MARKER,))
    private_file("databases/watcher.db", database.read_bytes())
    preferences = ('<?xml version="1.0" encoding="utf-8"?><map>'
                   '<boolean name="background" value="true"/>'
                   '<boolean name="wifi" value="false"/>'
                   '<boolean name="notify" value="true"/>'
                   '<string name="uninstall_marker">' + MARKER + '</string></map>')
    adb("shell", "run-as", PACKAGE, "mkdir", "-p", "shared_prefs", "cache", "files", "no_backup")
    private_file("shared_prefs/settings.xml", preferences.encode())
    for directory in ("cache", "files", "no_backup"):
        private_file(directory + "/" + MARKER, MARKER.encode())
    adb("shell", "pm", "grant", PACKAGE, "android.permission.POST_NOTIFICATIONS")
    adb("shell", "dumpsys", "deviceidle", "whitelist", "+" + PACKAGE)
    launch()
    for _ in range(25):
        if notifications() and jobs():
            break
        time.sleep(1)
    assert notifications(), "No actual app notification; check public website connectivity before retrying"
    assert jobs(), "Background job was not registered"
    # Open a real collected article so the platform's private WebView storage is
    # present in the uninstall check, rather than only testing SQLite and files.
    archive = args.output / "reader-before.db"
    archive.write_bytes(adb("exec-out", "run-as", PACKAGE, "cat", "databases/watcher.db", binary=True))
    with sqlite3.connect(archive) as db:
        title = db.execute("SELECT title FROM notices WHERE url LIKE 'https://www.cupk.edu.cn/%' ORDER BY date DESC,created DESC,id DESC LIMIT 1").fetchone()[0]
    adb("shell", "uiautomator", "dump", "/data/local/tmp/watcher-uninstall-ui.xml")
    tree = ET.fromstring(adb("exec-out", "cat", "/data/local/tmp/watcher-uninstall-ui.xml"))
    node = next(n for n in tree.iter("node") if n.get("text") == title)
    x1,y1,x2,y2 = map(int,re.findall(r"\d+",node.get("bounds")))
    adb("shell", "input", "tap", str((x1+x2)//2), str((y1+y2)//2))
    for _ in range(15):
        if exists("/data/user/0/" + PACKAGE + "/app_webview"): break
        time.sleep(.3)
    assert exists("/data/user/0/" + PACKAGE + "/app_webview"), "Reader did not create its private WebView profile"
    adb("shell", "rm", "-f", "/data/local/tmp/watcher-uninstall-ui.xml")
    record("Actual article reader creates WebView storage only inside the app private directory")
    record("Before uninstall: actual notification and background job exist; database, preferences, cached files and checkpoints are private")

    apk_paths = [line.removeprefix("package:") for line in adb("shell", "pm", "path", PACKAGE).splitlines() if line.startswith("package:")]
    install_dirs = {str(PurePosixPath(path).parent) for path in apk_paths}
    assert install_dirs and all(path.startswith("/data/app/") and PACKAGE in path for path in install_dirs)
    private_dirs = ["/data/user/0/" + PACKAGE, "/data/user_de/0/" + PACKAGE]
    external_dirs = ["/sdcard/Android/" + directory + "/" + PACKAGE for directory in ("data", "media", "obb")]
    assert exists(private_dirs[0])
    assert not any(exists(path) for path in external_dirs), "Unexpected external app directory needs investigation"
    # Normal uninstall only. Do not manually clean directories or scheduler state.
    assert "Success" in adb("uninstall", PACKAGE)
    for _ in range(10):
        if not any(exists(path) for path in private_dirs + external_dirs + list(install_dirs)) and not jobs() and not notifications():
            break
        time.sleep(.5)
    assert not any(exists(path) for path in private_dirs + external_dirs + list(install_dirs))
    assert not jobs() and not notifications()
    assert not adb("shell", "pidof", PACKAGE, check=False).stdout.strip()
    packages = adb("shell", "pm", "list", "packages", "-u", PACKAGE).splitlines()
    assert "package:" + PACKAGE not in packages, "Package/data was retained"
    record("Normal uninstall removes installation, private directories, active jobs, notifications and process; no retained package")

    adb("install", str(args.apk.resolve()))
    launch()
    time.sleep(1)
    stop()
    fresh = args.output / "after.db"
    fresh.write_bytes(adb("exec-out", "run-as", PACKAGE, "cat", "databases/watcher.db", binary=True))
    with sqlite3.connect(fresh) as db:
        for table in ("sources", "notices", "refresh_round", "refresh_items", "history_sources", "history_pages"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0, table
    assert not exists(private_dirs[0] + "/shared_prefs/settings.xml")
    for directory in ("cache", "files", "no_backup"):
        assert not exists(private_dirs[0] + "/" + directory + "/" + MARKER)
    record("Reinstall starts with empty subscriptions, archive and checkpoints, default preferences, and no old cache")
    report = {"device": adb("shell", "getprop", "ro.build.version.release").strip(), "serial": args.serial,
              "apkSha256": hashlib.sha256(args.apk.read_bytes()).hexdigest(), "checks": checks,
              "limitations": ["AOSP emulator only; OEM physical devices and cloud/D2D transports need separate verification",
                              "Android retains completed-job diagnostic history after uninstall; it is not runnable app work",
                              "OS history, downloaded APKs and user-shared external copies are outside app-private cleanup"]}
    (args.output / "uninstall-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
