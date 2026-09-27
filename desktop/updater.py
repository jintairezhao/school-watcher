"""Explicit, platform-aware updates from this repository's public GitHub releases."""
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import urllib.error
import urllib.parse
import urllib.request

from desktop import REPOSITORY, VERSION

MAX_PACKAGE = 2 * 1024 ** 3


class UpdateError(Exception):
    pass


def version_tuple(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(value))
    if not match:
        raise UpdateError('版本号格式不受支持，请前往项目 Release 页面查看。')
    return tuple(map(int, match.groups()))


def platform_key(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == 'Windows' and machine in ('amd64', 'x86_64'):
        return 'windows-x64'
    if system == 'Darwin' and machine in ('arm64', 'aarch64'):
        return 'macos-arm64'
    if system == 'Darwin' and machine in ('amd64', 'x86_64'):
        return 'macos-x64'
    raise UpdateError('当前系统暂无桌面安装包，请前往项目 Release 页面查看。')


def asset_name(version, target=None):
    version_tuple(version)
    version = version.removeprefix('v')
    target = target or platform_key()
    suffix = '-setup.exe' if target == 'windows-x64' else '.dmg'
    return f'School-Watcher-{version}-{target}{suffix}'


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {
            'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
            'objects.githubusercontent.com'} or parsed.username or parsed.password:
            raise UpdateError('更新下载地址不受信任，已停止下载。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': f'SchoolWatcher/{VERSION}',
        'Accept': 'application/vnd.github+json' if url.startswith('https://api.github.com/') else 'application/octet-stream'})
    try:
        return urllib.request.build_opener(SafeRedirect()).open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError('暂时还没有正式发布的更新。') from exc
        if exc.code in (403, 429):
            raise UpdateError('GitHub 暂时限制了请求频率，请稍后再试。') from exc
        raise UpdateError('暂时无法连接 GitHub，请稍后再试。') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError('暂时无法连接 GitHub，请检查网络后重试。') from exc


def read_bytes(url, limit, opener=open_url):
    with opener(url) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise UpdateError('更新信息超出预期大小，已停止处理。')
    return value


def release_asset(release, name):
    matches = [a for a in release.get('assets', []) if a.get('name') == name]
    if len(matches) != 1:
        raise UpdateError('这个版本尚未提供当前系统的完整安装包，请稍后重试。')
    item = matches[0]
    expected = f'https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(release["tag_name"], safe="")}/{name}'
    if item.get('browser_download_url') != expected:
        raise UpdateError('安装包来源与项目 Release 不一致，已停止处理。')
    return item


@dataclass(frozen=True)
class Update:
    version: str
    name: str
    url: str
    size: int
    sha256: str
    notes: str

    def public(self):
        return asdict(self)


def check_update(current=VERSION, target=None, opener=open_url):
    try:
        release = json.loads(read_bytes(f'https://api.github.com/repos/{REPOSITORY}/releases/latest', 1024 * 1024, opener))
        if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
            raise UpdateError('暂时还没有正式发布的更新。')
        tag = release.get('tag_name', '')
        if version_tuple(tag) <= version_tuple(current):
            return None
        version = tag.removeprefix('v')
        name = asset_name(version, target)
        package = release_asset(release, name)
        size = package.get('size')
        if not isinstance(size, int) or not 0 < size <= MAX_PACKAGE:
            raise UpdateError('安装包大小不符合预期，已停止处理。')
        manifest = release_asset(release, 'SHA256SUMS.txt')
        checksums = read_bytes(manifest['browser_download_url'], 65536, opener).decode('utf-8')
        hashes = [m.group(1).lower() for line in checksums.splitlines()
                  if (m := re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?' + re.escape(name), line.strip()))]
        if len(hashes) != 1:
            raise UpdateError('缺少安装包校验信息，暂时不能安全下载。')
        return Update(version, name, package['browser_download_url'], size, hashes[0], str(release.get('body') or '')[:6000])
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise UpdateError('GitHub 返回的更新信息不完整，请稍后重试。') from exc


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def download_update(update, data_dir, progress=None, opener=open_url):
    # Names are derived locally, never accepted as arbitrary release-provided paths.
    if update.name not in [asset_name(update.version, p) for p in ('windows-x64', 'macos-x64', 'macos-arm64')]:
        raise UpdateError('安装包名称不合法。')
    folder = Path(data_dir) / 'updates' / update.version
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / update.name
    if target.exists() and target.stat().st_size == update.size and file_hash(target) == update.sha256:
        return target
    partial = target.with_suffix(target.suffix + '.part')
    digest, count = hashlib.sha256(), 0
    try:
        with opener(update.url) as source, partial.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                count += len(chunk)
                if count > update.size:
                    raise UpdateError('下载大小与 Release 不一致，已停止下载。')
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(count, update.size)
            output.flush()
            os.fsync(output.fileno())
        if count != update.size or digest.hexdigest() != update.sha256:
            raise UpdateError('安装包校验失败，请重新下载。')
        os.replace(partial, target)
        return target
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
