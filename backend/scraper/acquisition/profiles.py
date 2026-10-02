"""Versioned, explicitly verified public API profiles; never guessed endpoints."""
from dataclasses import replace
from html import escape
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urljoin

from .contracts import FetchResult

DEFAULT_PROFILES = Path(__file__).resolve().parents[3] / 'config' / 'source_profiles.json'


def _profile_path():
    return os.environ.get('WATCHER_SOURCE_PROFILES') or str(DEFAULT_PROFILES)


def profile_fingerprint():
    """Read-only revision check used by the worker's stale-execution fence."""
    path = _profile_path()
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return 'unreadable:' + hashlib.sha256(path.encode()).hexdigest()
    return hashlib.sha256(b'acquisition-profiles:1\0' + payload).hexdigest()


def configured_request(request):
    """Load explicit source policies; ambiguity is an error, never a first-match guess."""
    path = _profile_path()
    try:
        payload = Path(path).read_bytes()
        if len(payload) > 1024 * 1024:
            raise ValueError('Source profiles exceed 1 MiB')
        catalog = json.loads(payload)
        if catalog.get('version') != 1 or not isinstance(catalog.get('profiles'), list):
            raise ValueError('Source profiles require version 1 and a profiles array')
        matches = []
        for profile in catalog['profiles']:
            if not isinstance(profile, dict) or not (profile.get('url_prefix') or profile.get('url')) or not profile.get('version'):
                raise ValueError('Every source profile requires URL scope and version')
            if profile.get('source_id') and str(profile['source_id']) != request.source_id:
                continue
            if profile.get('purpose') and profile['purpose'] != request.purpose:
                continue
            # Require an URL boundary, not a raw hostname prefix that would also
            # match example.edu.cn.attacker.example.
            from urllib.parse import urlsplit
            scope = profile.get('url') or profile['url_prefix']
            prefix, candidate = urlsplit(scope), urlsplit(request.url)
            if (prefix.scheme, prefix.netloc) != (candidate.scheme, candidate.netloc):
                continue
            if profile.get('url') and request.url.rstrip('/') != scope.rstrip('/'):
                continue
            if not profile.get('url') and not request.url.startswith(scope):
                continue
            matches.append(profile)
        if len(matches) > 1:
            raise ValueError('Source policy scopes overlap; narrow their URL or purpose')
        if not matches:
            return request
        profile = matches[0]
        policy = dict(profile.get('policy', {}), **request.policy)
        revision = str(profile['version']) + ':' + hashlib.sha256(payload).hexdigest()[:16]
        return replace(request, policy=policy, policy_version=revision,
                       readiness_selector=request.readiness_selector or (
                           '' if request.policy.get('exploration') else profile.get('readiness_selector', '')),
                       expected_response_url=profile.get('expected_response_url') or request.expected_response_url)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid source profile configuration: ' + str(exc)) from exc


def _field(value, path):
    for part in str(path or '').split('.'):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            raise ValueError('API response does not match verified mapping: ' + str(path))
        value = value[part]
    return value


def api_request(request):
    profile = request.policy.get('api')
    if not profile:
        return None
    if not isinstance(profile, dict) or profile.get('verified') is not True:
        raise ValueError('Public API profile requires explicit verification')
    if not profile.get('evidence_url') or not profile.get('version') or not profile.get('url'):
        raise ValueError('Public API profile requires evidence, version and exact endpoint')
    if profile.get('purpose', request.purpose) != request.purpose:
        raise ValueError('Public API profile purpose mismatch')
    return replace(request, url=profile['url'], browser_allowed=False)


def map_api_response(request, response):
    """A small fixed field mapper produces HTML accepted by the existing parsers."""
    profile = request.policy['api']
    if response.status >= 400 or response.json_data is None:
        return replace(response, outcome='needs_adapter', error_code='api_response_changed',
                       message='已验证的官网接口未返回预期数据，需要核对适配规则')
    try:
        value = _field(response.json_data, profile.get('items_path', ''))
        mapping = profile.get('fields', {})
        if request.purpose == 'article':
            content = _field(value, mapping.get('content', 'content'))
            if not isinstance(content, str) or not content.strip():
                raise ValueError('Missing article content')
            html = '<article>' + content + '</article>'
            count = 1
        else:
            if not isinstance(value, list):
                raise ValueError('Expected an array')
            rows = []
            for item in value:
                title = _field(item, mapping.get('title', 'title'))
                href = _field(item, mapping.get('url', 'url'))
                date = _field(item, mapping['date']) if mapping.get('date') else ''
                if not isinstance(title, str) or not title.strip() or not isinstance(href, str) or not href.strip():
                    raise ValueError('Missing title or original article address')
                original = urljoin(profile.get('link_base_url', request.url), href)
                from backend.scraper.http_client import validate_public_url
                validate_public_url(original, resolve=False)
                rows.append('<li><a href="' + escape(original, quote=True) + '">' + escape(title) +
                            '</a><time>' + escape(str(date)) + '</time></li>')
            count = len(rows)
            html = '<section><h2>通知公告</h2><ul class="watcher-api-list">' + ''.join(rows) + '</ul></section>'
        if not count and profile.get('empty_is_valid') is not True:
            raise ValueError('Empty response has not been verified')
        return replace(response, final_url=request.url, html=html, transport='api',
                       outcome='usable' if count else 'empty', error_code='', message='',
                       evidence=tuple(response.evidence) + ('verified_api:' + str(profile['version']),))
    except (ValueError, TypeError, KeyError) as exc:
        return replace(response, outcome='needs_adapter', error_code='api_mapping_changed',
                       message='官网接口内容与已验证规则不一致，需要核对适配', evidence=(str(exc),))
