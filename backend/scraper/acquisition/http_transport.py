"""Cancelable HTTP acquisition with one deadline across DNS, headers and body."""
import asyncio
import ipaddress
import json
import time
import zlib
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import UnicodeDammit
from requests.utils import get_environ_proxies, select_proxy

from backend.scraper.http_client import (_shared_permit, _release_permit, validate_public_url,
    retry_after_seconds, FetchAdmissionBusy, proxy_failed_before_send, proxy_required,
    retry_direct_for, remember_proxy_failure)
from backend.scraper.pinned_transport import public_addresses
from backend.services.discovery_policy import get_policy
from .contracts import FetchResult


def _pinned_url(url, address):
    parsed = urlsplit(url)
    if not ipaddress.ip_address(address).is_global:
        raise ValueError('Connection target must be public')
    authority = '[' + address + ']' if ':' in address else address
    if parsed.port:
        authority += ':' + str(parsed.port)
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ''))


async def read_http(request, permit=None, trace=None, *, capture_binary=False, max_bytes=None, extra_headers=None):
    policy = get_policy()
    read_limit = int(max_bytes) if capture_binary else policy['html_max_bytes']
    budget = min(float(request.timeout_seconds), float(policy['http_total_seconds']))
    start = time.monotonic()
    target, visited = request.url, set()
    notes = []
    admission_wait = 0
    trace = trace if trace is not None else {}
    trace['notes'] = notes
    # The enclosing timeout also bounds DNS and the entire redirect chain. A
    # steady trickle of bytes cannot keep a response alive past this deadline.
    async with asyncio.timeout(budget) as deadline:
        async with aiohttp.ClientSession(trust_env=False, auto_decompress=False) as session:
            # aiohttp 3.14 otherwise silently retries idempotent requests on
            # disconnected connections. The durable queue owns all sent retries.
            session._retry_connection = False
            while len(visited) < 6:
                trace['retry_direct_url'] = None
                validate_public_url(target, resolve=False)
                if target in visited:
                    return FetchResult(target, outcome='denied', error_code='redirect_loop', message='官网地址发生循环跳转，稍后自动复查')
                visited.add(target)
                if permit is None:
                    from backend.services.tasks import TaskDeferred
                    while True:
                        try:
                            permit = _shared_permit(target, budget)
                            break
                        except (FetchAdmissionBusy, TaskDeferred) as busy:
                            delay = busy.retry_after if isinstance(busy, FetchAdmissionBusy) else busy.delay
                            if delay > 2 or admission_wait >= budget:
                                raise
                            # A same-host redirect often reaches the next hop before
                            # its one-second start interval. Waiting is not page I/O.
                            wait = max(.01, delay)
                            deadline.reschedule(deadline.when() + wait)
                            await asyncio.sleep(wait)
                            admission_wait += wait
                            trace['admission_wait'] = admission_wait
                try:
                    connection_start = time.monotonic()
                    trace['phase'] = 'dns'
                    try:
                        addresses = await asyncio.wait_for(asyncio.to_thread(public_addresses, target),
                                                           timeout=min(policy['http_connect_seconds'], budget))
                    except TimeoutError:
                        # The enclosing total deadline cancels this await with
                        # CancelledError instead; only wait_for expiry lands here.
                        trace['timeout_kind'] = 'connect'
                        raise
                    trace['phase'] = 'connect_or_headers'
                    parsed = urlsplit(target)
                    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii')
                    headers = {'Host': host if parsed.port in (None, 80 if parsed.scheme == 'http' else 443)
                               else parsed.netloc,
                               'User-Agent': 'SchoolWatcher/3.0 (public university notifications)',
                               'Accept-Encoding': 'gzip, deflate',
                               'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5',
                               'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}
                    if capture_binary and extra_headers:
                        headers.update({name:value for name,value in extra_headers.items() if name in ('Referer','Accept')})
                    remaining = budget - (time.monotonic() - start - admission_wait)
                    connect_remaining = max(.001, policy['http_connect_seconds'] -
                                            (time.monotonic() - connection_start))
                    timeout = aiohttp.ClientTimeout(total=max(.001, remaining),
                        connect=min(connect_remaining, remaining),
                        sock_connect=min(connect_remaining, remaining),
                        sock_read=min(policy['http_read_seconds'], remaining), ceil_threshold=1000)
                    proxy = select_proxy(target, get_environ_proxies(target))
                    required_proxy = proxy_required(request.policy)
                    if not required_proxy and retry_direct_for(target):
                        trace['direct_after_proxy_failure']=bool(proxy)
                        proxy = None
                        notes.append('direct_on_durable_retry')
                    trace['retry_direct_url'] = target if proxy and not required_proxy else None
                    address = next((ip for ip in addresses if ':' not in ip), addresses[0])
                    options = dict(headers=headers, timeout=timeout, allow_redirects=False,
                                   server_hostname=host if parsed.scheme == 'https' else None)
                    try:
                        response = await session.get(_pinned_url(target, address), proxy=proxy, **options)
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                        if not proxy or required_proxy or not proxy_failed_before_send(exc):
                            raise
                        notes.append('direct_after_unsent_proxy_failure:' + type(exc).__name__)
                        response = await session.get(_pinned_url(target, address), **options)
                    async with response:
                        trace['phase'] = 'body'
                        response_headers = dict(response.headers)
                        retry_after = retry_after_seconds(response.headers.get('Retry-After'))
                        if response.status in (403, 407, 502, 503, 504) and proxy and not required_proxy:
                            # An environment proxy can reject a public page the
                            # direct route can read, or fail its pinned-IP route
                            # with a gateway status. Preserve the observed HTTP
                            # status; this does not prove who generated it.
                            # Record an available route check, not
                            # a parser problem; the durable queue owns the wait
                            # and any later direct attempt. Never resend here.
                            notes.append('proxy_route_retry_available')
                        if permit and (response.status == 429 or retry_after is not None):
                            from backend.services.runtime_leases import cool_origin
                            cool_origin(target, retry_after if retry_after is not None else 300)
                        if response.status in (301, 302, 303, 307, 308) and response.headers.get('Location'):
                            location = response.headers['Location']
                            if parsed.scheme == 'http' and parsed.port is None and location == 'https://' + parsed.netloc + target:
                                target = parsed._replace(scheme='https').geturl()
                                notes.append('repaired_same_host_https_redirect:' + location)
                            else:
                                target = urljoin(target, location)
                            trace['redirect_target'] = target
                            continue
                        mime = response.headers.get('Content-Type', '').lower()
                        if capture_binary and response.status >= 400:
                            return FetchResult(target, status=response.status, headers=response_headers,
                                outcome='network_error' if response.status in (408, 429, 500, 502, 503, 504) else 'denied',
                                error_code='http_' + str(response.status), retry_after=retry_after,
                                evidence=tuple(notes),
                                message='官网附件暂时无法读取（HTTP ' + str(response.status) + '）')
                        pdf = request.purpose == 'article' and ('application/pdf' in mime or
                            'application/octet-stream' in mime and urlsplit(target).path.lower().endswith('.pdf'))
                        if not capture_binary and mime and not pdf and not any(kind in mime for kind in ('text/', 'application/xhtml', 'application/json', '+json')):
                            return FetchResult(target, status=response.status, headers=response_headers,
                                outcome='denied', error_code='non_html_resource', message='该官网地址返回了非网页资源',
                                retry_after=retry_after, evidence=tuple(notes))
                        payload = bytearray()
                        encoding = response.headers.get('Content-Encoding', '').lower()
                        decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else
                                   zlib.decompressobj() if encoding == 'deflate' else None)
                        if encoding and encoding not in ('identity', 'gzip', 'deflate'):
                            return FetchResult(target, outcome='denied', error_code='unsupported_content_encoding',
                                               message='官网使用了暂不支持的网页压缩格式')
                        async for chunk in response.content.iter_chunked(65536):
                            if decoder:
                                chunk = decoder.decompress(chunk, read_limit + 1 - len(payload))
                            payload.extend(chunk)
                            if len(payload) > read_limit:
                                return FetchResult(target, status=response.status, headers=response_headers,
                                    outcome='denied', error_code='response_too_large',
                                    message=f"官网响应超过{read_limit / 1048576:g} MiB读取上限，未保存不完整内容")
                        if decoder and not decoder.eof:
                            raise aiohttp.ClientPayloadError('Incomplete compressed response')
                        if capture_binary:
                            return FetchResult(target, status=response.status, headers=dict(response.headers),
                                attachment={'payload': bytes(payload)}, outcome='usable',
                                evidence=tuple(notes) + ('public_dns_ipv4:' + address,))
                        if pdf:
                            from .attachments import pdf_observation
                            attachment = pdf_observation(bytes(payload), target, response_headers)
                            return FetchResult(target, status=response.status, headers=response_headers,
                                attachment=attachment or {}, outcome='usable' if attachment else 'denied',
                                error_code='' if attachment else 'invalid_pdf_response',
                                message='' if attachment else '官网附件未返回完整 PDF 内容',
                                evidence=tuple(notes) + ('public_dns_ipv4:' + address,),
                                timings={'http_ms': round((time.monotonic() - start - admission_wait) * 1000),
                                         'admission_wait_ms': round(admission_wait * 1000)})
                        html = UnicodeDammit(bytes(payload), is_html=True).unicode_markup or ''
                        data = None
                        if 'json' in mime or request.policy.get('api'):
                            try:
                                data = json.loads(html)
                            except ValueError:
                                pass
                        return FetchResult(target, status=response.status, html=html, json_data=data,
                            headers=response_headers, retry_after=retry_after,
                            evidence=tuple(notes) + ('public_dns_ipv4:' + address,),
                            timings={'http_ms': round((time.monotonic() - start - admission_wait) * 1000),
                                     'admission_wait_ms': round(admission_wait * 1000)})
                finally:
                    _release_permit(permit)
                    permit = None
    return FetchResult(target, outcome='denied', error_code='redirect_limit', message='官网跳转次数超出本次读取上限')


def fetch_http(request, *, capture_binary=False, max_bytes=None, extra_headers=None):
    permit = None
    start = time.monotonic()
    trace = {}
    loop = asyncio.new_event_loop()
    try:
        # Admission is outside the page's timeout. Busy durable tasks yield to
        # the shared queue rather than occupying an HTTP worker while waiting.
        permit = _shared_permit(request.url, request.timeout_seconds)
        start = time.monotonic()
        reader = (read_http(request, permit, trace, capture_binary=True, max_bytes=max_bytes, extra_headers=extra_headers)
                  if capture_binary else read_http(request, permit, trace))
        result = loop.run_until_complete(reader)
        if 200 <= result.status < 300 and trace.get('direct_after_proxy_failure'):
            from backend.services.http_route_memory import remember_verified_direct
            remember_verified_direct(result.final_url)
        if 'proxy_route_retry_available' in result.evidence:
            remember_proxy_failure(result.final_url)
        return result
    except FetchAdmissionBusy as exc:
        return FetchResult(request.url, outcome='busy', error_code='origin_busy',
                           retry_after=exc.retry_after, message=str(exc))
    except ValueError as exc:
        if 'No public IPv4 address could be verified' in str(exc):
            return FetchResult(request.url, outcome='network_error', error_code='http_dns_failure',
                               message='官网域名未能解析到可验证的公网地址，系统稍后自动重试')
        return FetchResult(request.url, outcome='denied', error_code='invalid_public_url',
                           message='官网地址必须是可公开访问的 HTTP 或 HTTPS 地址')
    except (TimeoutError, aiohttp.ClientError) as exc:
        if trace.get('retry_direct_url'):
            remember_proxy_failure(trace['retry_direct_url'])
        from backend.scraper.auth_routes import is_sign_in_route, LOGIN_REQUIRED_MESSAGE
        target = trace.get('redirect_target', '')
        if target and is_sign_in_route(target):
            return FetchResult(target, outcome='denied', error_code='source_login_required',
                               message=LOGIN_REQUIRED_MESSAGE)
        if isinstance(exc, aiohttp.SocketTimeoutError):
            code = 'http_idle_timeout'
        elif isinstance(exc, aiohttp.ConnectionTimeoutError) or trace.get('timeout_kind') == 'connect':
            code = 'http_connect_timeout'
        else:
            code = 'http_budget_exhausted' if isinstance(exc, TimeoutError) else 'http_' + type(exc).__name__.lower()
        return FetchResult(request.url, outcome='network_error', error_code=code,
            message='连接或读取官网超时，系统稍后自动重试' if isinstance(exc, TimeoutError) else '官网网络连接失败，系统稍后自动重试',
            evidence=tuple(trace.get('notes', ())),
            timings={'http_ms': round((time.monotonic() - start - trace.get('admission_wait', 0)) * 1000)})
    except Exception as exc:
        return FetchResult(request.url, outcome='network_error', error_code='http_' + type(exc).__name__.lower(),
            message='官网网络连接失败，系统稍后自动重试',
            timings={'http_ms': round((time.monotonic() - start) * 1000)})
    finally:
        _release_permit(permit)
        loop.run_until_complete(loop.shutdown_asyncgens())
        # DNS work can outlive cancellation in an OS resolver. It has no socket
        # to the website and cannot publish a late result; do not wait for it.
        loop.close()
