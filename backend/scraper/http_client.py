"""Small static HTTP client; enhanced TLS is opt-in, redirects stay public."""
import ipaddress
import socket
import time
from urllib.parse import urlsplit, urljoin

import requests as standard_requests


def validate_public_url(url, resolve=True):
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError('请输入有效的公开官网地址')
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if parsed.port not in (None, 80, 443) or '\\' in url or any(c.isspace() for c in url):
            raise ValueError()
        hostname = parsed.hostname.rstrip('.').lower()
        if hostname == 'localhost' or hostname.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if resolve:
            addresses = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                raise ValueError()
    except (ValueError, OSError) as exc:
        raise ValueError('官网地址必须是可公开访问的 HTTP 或 HTTPS 地址') from exc
    return url


def official_domain(url):
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    parts = host.split('.')
    if host.endswith(('.edu.cn', '.ac.cn')) and len(parts) >= 3:
        return '.'.join(parts[-3:])
    return host


def same_school_url(url, school_url):
    host = (urlsplit(url).hostname or '').lower()
    base = official_domain(school_url)
    return bool(base) and (host == base or host.endswith('.' + base))


from threading import BoundedSemaphore, Lock

# Shared by list, body and discovery requests within the single collection process.
_outbound_slots = BoundedSemaphore(2)
_host_guard = Lock()
_host_next = {}


def _pace_host(url):
    """All collection paths, including legacy lists and redirects, share one interval."""
    host = urlsplit(url).hostname.lower().rstrip('.')
    with _host_guard:
        now = time.monotonic()
        start = max(now, _host_next.get(host, now))
        _host_next[host] = start + 1.0
        if len(_host_next) > 512:
            for old in [key for key, due in _host_next.items() if due < now]:
                del _host_next[old]
    if start > now:
        time.sleep(start - now)


def _shared_permit(url, timeout):
    """Runtime requests reserve the same host budget as browser navigations."""
    from flask import has_app_context
    if not has_app_context():
        return None
    from backend.services import tasks
    from backend.services.runtime_leases import reserve_origin
    handle = tasks.current_execution()
    if not handle:
        return None
    tasks.assert_owned(handle)
    seconds = sum(timeout) if isinstance(timeout, (tuple, list)) else timeout or 30
    permit, wait = reserve_origin(url, handle.get('worker_id', 'local'), ttl=max(120, seconds + 30))
    if not permit:
        tasks.defer(phase='origin_wait', delay=min(900, wait), reason='官网访问正在排队', error_code='origin_busy')
    return permit


def _release_permit(permit):
    if permit:
        from backend.services.runtime_leases import release_origin
        release_origin(permit['token'])


def _guard_response(response, permit):
    if not permit:
        return response
    from backend.services.runtime_leases import cool_origin
    if response.status_code == 429:
        retry = response.headers.get('Retry-After', '')
        cool_origin(response.url, float(retry) if str(retry).isdigit() else 300)
    original_close = response.close
    held = [permit]
    def close():
        try:
            return original_close()
        finally:
            if held:
                _release_permit(held.pop())
    response.close = close
    if getattr(response, '_content_consumed', False):
        _release_permit(held.pop())
    return response


class PublicHTTPClient:
    def request(self, method, url, **kwargs):
        with _outbound_slots:
            return self._request(method, url, **kwargs)

    def _request(self, method, url, **kwargs):
        method = method.upper()
        follow = kwargs.pop('allow_redirects', method != 'HEAD')
        # Every collection path uses the same pinned transport. Browser fallback
        # remains available when a site requires a browser TLS/session profile.
        kwargs.pop('impersonate', None)
        from backend.scraper.auth_routes import is_sign_in_route
        from backend.scraper.fetch_errors import SourceLoginRequired
        from backend.scraper.pinned_transport import pinned_request, public_addresses
        transport_notes = []
        sign_in_hop = ''
        for _ in range(6):
            addresses = public_addresses(url)
            permit = _shared_permit(url, kwargs.get('timeout'))
            try:
                try:
                    response = pinned_request(method, url, addresses=addresses, **kwargs)
                except (standard_requests.exceptions.ConnectionError, standard_requests.exceptions.Timeout) as exc:
                    proxies = standard_requests.utils.get_environ_proxies(url)
                    if (method not in ('GET', 'HEAD') or 'proxies' in kwargs or
                            not any(proxies.get(key) for key in ('http', 'https', 'all'))):
                        if sign_in_hop:
                            raise SourceLoginRequired(sign_in_hop) from exc
                        raise
                    try:
                        response = pinned_request(method, url, addresses=addresses, trust_env=False, **kwargs)
                    except (standard_requests.exceptions.ConnectionError, standard_requests.exceptions.Timeout) as direct:
                        if sign_in_hop:
                            raise SourceLoginRequired(sign_in_hop) from direct
                        raise
                    transport_notes.append('direct_after_proxy_error:' + type(exc).__name__)
                _guard_response(response, permit)
            except BaseException:
                _release_permit(permit)
                raise
            if follow and response.status_code in (301, 302, 303, 307, 308) and response.headers.get('Location'):
                location = response.headers['Location']
                original = urlsplit(url)
                # Some official servers append the entire HTTP URL to the
                # HTTPS authority. Repair only this exact same-host pattern.
                if (original.scheme == 'http' and original.port is None and
                        location == 'https://' + original.netloc + url):
                    target = original._replace(scheme='https').geturl()
                    transport_notes.append('repaired_same_host_https_redirect:' + location)
                else:
                    target = urljoin(url, location)
                if is_sign_in_route(target):
                    # Remembered so that if the hand-off itself fails at the
                    # transport layer, the caller is told the site requires a
                    # sign-in rather than being handed a connection error.
                    sign_in_hop = target
                response.close()
                url = target
                continue
            response._watcher_transport = list(dict.fromkeys(transport_notes))
            return response
        raise ValueError('官网重定向次数过多')

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def head(self, url, **kwargs):
        return self.request('HEAD', url, **kwargs)


requests = PublicHTTPClient()
