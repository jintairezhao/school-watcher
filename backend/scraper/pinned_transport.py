"""Requests transport whose TCP/CONNECT target is a validated numeric address.

Keep the original URL for cookies, Host, certificate checks and SNI. DNS is never
delegated a second time to the HTTP client, including with environment proxies.
"""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy, prepend_scheme_if_needed


def public_addresses(url):
    from backend.scraper.http_client import validate_public_url
    from backend.scraper.public_dns import public_ipv4
    validate_public_url(url, resolve=False)
    parsed = urlsplit(url)
    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii')
    try:
        rows = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                  type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(row[4][0] for row in rows))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError('DNS returned a non-public address')
        return addresses
    except (OSError, ValueError):
        # The existing split-DNS fallback is restricted to public campus names.
        return public_ipv4(host)


class PinnedAdapter(HTTPAdapter):
    def __init__(self, url, address):
        super().__init__(max_retries=0)
        if not ipaddress.ip_address(address).is_global:
            raise ValueError('Connection target must be public')
        self.address = address
        self.hostname = urlsplit(url).hostname.rstrip('.').encode('idna').decode('ascii').lower()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if verify is False:
            raise ValueError('Public collection requires TLS certificate verification')
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        if host_params['host'].rstrip('.').lower() != self.hostname:
            raise ValueError('Connection hostname changed after validation')
        host_params['host'] = self.address
        if host_params['scheme'] == 'https':
            pool_kwargs.update(server_hostname=self.hostname, assert_hostname=self.hostname)
        proxy = select_proxy(request.url, proxies)
        manager = self.proxy_manager_for(prepend_scheme_if_needed(proxy, 'http')) if proxy else self.poolmanager
        return manager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)

    def request_url(self, request, proxies):
        url = super().request_url(request, proxies)
        if urlsplit(url).scheme:
            # HTTP proxies receive an absolute URI; pin that URI as well.
            parsed = urlsplit(url)
            authority = '[' + self.address + ']' if ':' in self.address else self.address
            if parsed.port:
                authority += ':' + str(parsed.port)
            return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ''))
        return url

    def add_headers(self, request, **kwargs):
        parsed = urlsplit(request.url)
        host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
        # Match normal browser/urllib3 Host headers. Some campus frontends route
        # an explicit default port to a different (404) virtual host after HTTPS
        # redirects. Non-default ports remain part of the origin identity.
        if parsed.port and parsed.port != (443 if parsed.scheme == 'https' else 80):
            host += ':' + str(parsed.port)
        request.headers['Host'] = host


def pinned_request(method, url, *, addresses=None, trust_env=True, **kwargs):
    from backend.scraper.http_client import _pace_host
    kwargs.setdefault('timeout', (6, 16))
    addresses = addresses or public_addresses(url)
    if not addresses:
        raise ValueError('No public destination')
    last_error = None
    for address in addresses:
        session = requests.Session()
        session.trust_env = trust_env
        adapter = PinnedAdapter(url, address)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        try:
            _pace_host(url)
            response = session.request(method, url, allow_redirects=False, **kwargs)
            original_close = response.close
            def close(original_close=original_close, session=session):
                try:
                    original_close()
                finally:
                    session.close()
            response.close = close
            response._watcher_dns = {'host': adapter.hostname, 'public_ip': address}
            if response._content_consumed:
                session.close()
            return response
        except (requests.ConnectionError, requests.Timeout) as exc:
            session.close()
            last_error = exc
            if method not in ('GET', 'HEAD'):
                raise
        except BaseException:
            session.close()
            raise
    raise last_error
