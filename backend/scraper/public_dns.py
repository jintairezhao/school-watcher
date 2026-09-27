"""Resolve a public campus endpoint when local split DNS returns private addresses."""
import ipaddress
import json
import secrets
import socket
import struct
import time
from urllib.parse import urlsplit

import requests
import urllib3

_cache = {}


def _https_ipv4(host):
    """UDP/53 may be unavailable; query fixed TLS resolvers, never arbitrary URLs."""
    endpoints = ('https://dns.alidns.com/resolve', 'https://cloudflare-dns.com/dns-query')
    for endpoint in endpoints:
        try:
            with requests.get(endpoint, params={'name': host, 'type': 'A'},
                              headers={'Accept': 'application/dns-json'}, timeout=(4, 6),
                              stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    continue
                payload = bytearray()
                for chunk in response.iter_content(8192):
                    payload.extend(chunk)
                    if len(payload) > 65536:
                        raise ValueError('DNS answer exceeds limit')
                data = json.loads(payload)
            if not isinstance(data, dict) or data.get('Status') != 0 or data.get('TC'):
                continue
            questions = data.get('Question', [])
            if isinstance(questions, dict):
                questions = [questions]
            if (not isinstance(questions, list) or len(questions) != 1 or
                    not isinstance(questions[0], dict) or questions[0].get('type') != 1 or
                    str(questions[0].get('name', '')).rstrip('.').lower() != host.lower()):
                continue
            answers = data.get('Answer', [])
            if not isinstance(answers, list) or len(answers) > 128:
                continue
            # Only A records for this question or its explicit CNAME chain count.
            names, ttl = {host.lower()}, 300
            for _ in range(16):
                old_size = len(names)
                for record in answers:
                    if (isinstance(record, dict) and record.get('type') == 5 and
                            str(record.get('name', '')).rstrip('.').lower() in names):
                        names.add(str(record.get('data', '')).rstrip('.').lower())
                        ttl = min(ttl, max(1, int(record.get('TTL', 60))))
                if len(names) == old_size:
                    break
            addresses = []
            for record in answers:
                if (not isinstance(record, dict) or record.get('type') != 1 or
                        str(record.get('name', '')).rstrip('.').lower() not in names):
                    continue
                address = ipaddress.ip_address(record.get('data', ''))
                if address.version != 4 or not address.is_global:
                    raise ValueError('DNS answer is not a public IPv4 address')
                addresses.append(str(address))
                ttl = min(ttl, max(1, int(record.get('TTL', 60))))
            if addresses:
                return list(dict.fromkeys(addresses)), ttl
        except (requests.RequestException, OSError, ValueError, TypeError):
            continue
    return None


class PinnedResponse(requests.Response):
    def close(self):
        try:
            super().close()
        finally:
            pool = getattr(self, '_watcher_pool', None)
            if pool:
                pool.close()


def _skip_name(packet, position):
    while position < len(packet):
        length = packet[position]
        if length & 0xC0 == 0xC0:
            if position + 1 >= len(packet):
                raise ValueError('Truncated DNS name')
            return position + 2
        if length > 63:
            raise ValueError('Invalid DNS label')
        position += 1
        if length == 0:
            return position
        position += length
    raise ValueError('Truncated DNS name')


def public_ipv4(host):
    host = host.rstrip('.').encode('idna').decode()
    # This fallback is for publicly named university sites, never literal/private destinations.
    if not host.endswith(('.edu.cn', '.ac.cn')):
        raise ValueError('Public university DNS fallback is unavailable for this host')
    cached = _cache.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    labels = host.encode().split(b'.')
    if any(not label or len(label) > 63 for label in labels):
        raise ValueError('Invalid DNS name')
    question = b''.join(bytes([len(label)]) + label for label in labels) + b'\0\0\1\0\1'
    txid = secrets.randbits(16)
    query = struct.pack('!HHHHHH', txid, 0x0100, 1, 0, 0, 0) + question
    for server in ('1.1.1.1', '8.8.8.8'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(3)
                sock.connect((server, 53))
                sock.send(query)
                data = sock.recv(4096)
            ident, flags, questions, answers, _, _ = struct.unpack('!6H', data[:12])
            if ident != txid or not flags & 0x8000 or flags & 0x020F or questions != 1:
                continue
            if data[12:12 + len(question)] != question:
                continue
            position = 12 + len(question)
            addresses, ttl = [], 300
            for _ in range(answers):
                position = _skip_name(data, position)
                kind, dnsclass, seconds, length = struct.unpack('!HHIH', data[position:position + 10])
                position += 10
                payload = data[position:position + length]
                if len(payload) != length:
                    raise ValueError('Truncated DNS answer')
                position += length
                if kind == 1 and dnsclass == 1 and length == 4:
                    address = str(ipaddress.ip_address(payload))
                    if ipaddress.ip_address(address).is_global:
                        addresses.append(address)
                        ttl = min(ttl, seconds)
            if addresses:
                addresses = list(dict.fromkeys(addresses))
                _cache[host] = (time.monotonic() + max(1, ttl), addresses)
                return addresses
        except (OSError, ValueError, struct.error):
            continue
    resolved = _https_ipv4(host)
    if resolved:
        addresses, ttl = resolved
        _cache[host] = (time.monotonic() + ttl, addresses)
        return addresses
    raise ValueError('No public IPv4 address could be verified')


def pinned_request(method, url, **kwargs):
    """Keep TLS certificate and SNI checks on the original hostname while pinning the peer IP."""
    from backend.scraper.http_client import _pace_host
    if method not in ('GET', 'HEAD'):
        raise ValueError('Public DNS fallback supports read-only requests')
    parsed = urlsplit(url)
    host = parsed.hostname
    headers = dict(kwargs.get('headers') or {})
    headers['Host'] = parsed.netloc
    timeout = kwargs.get('timeout', 15)
    connect, read = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    last_error = None
    for address in public_ipv4(host):
        pool = None
        try:
            if parsed.scheme == 'https':
                pool = urllib3.HTTPSConnectionPool(address, port=parsed.port or 443,
                    server_hostname=host, assert_hostname=host, cert_reqs='CERT_REQUIRED',
                    ca_certs=requests.certs.where())
            else:
                pool = urllib3.HTTPConnectionPool(address, port=parsed.port or 80)
            # Reserve the request start after resolution, which can take seconds.
            _pace_host(url)
            raw = pool.urlopen(method, path, headers=headers, preload_content=False,
                               redirect=False, retries=False, timeout=urllib3.Timeout(connect=connect, read=read))
            response = PinnedResponse()
            response.status_code = raw.status
            response.headers = requests.structures.CaseInsensitiveDict(raw.headers)
            response.raw = raw
            response.url = url
            response.encoding = requests.utils.get_encoding_from_headers(response.headers)
            response._watcher_dns = {'host': host, 'public_ip': address}
            response._watcher_pool = pool
            if not kwargs.get('stream'):
                response.content
                pool.close()
            return response
        except Exception as exc:
            last_error = exc
            if pool:
                pool.close()
    raise ValueError(f'Public campus endpoint did not respond: {last_error}')
