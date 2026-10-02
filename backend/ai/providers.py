"""Official native HTTP adapters. Exactly one request; never follow redirects."""
from dataclasses import dataclass, field
import codecs
import json
import time
import requests

STREAM_TOTAL_SECONDS = 600

ENDPOINTS = {
    'deepseek': {'default': 'https://api.deepseek.com/chat/completions'},
    'dashscope': {
        'cn-beijing': 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
        'ap-southeast-1': 'https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
        'us-east-1': 'https://dashscope-us.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
    },
    'ark': {'cn-beijing': 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'},
    'bigmodel': {'default': 'https://open.bigmodel.cn/api/paas/v4/chat/completions'},
}
PROVIDER_NAMES = {'deepseek': 'DeepSeek', 'dashscope': '阿里云百炼 / 通义千问',
                  'ark': '火山方舟 / 豆包', 'bigmodel': '智谱'}
DEFAULT_REGIONS = {'deepseek': 'default', 'dashscope': 'cn-beijing', 'ark': 'cn-beijing', 'bigmodel': 'default'}


class ProviderError(Exception):
    def __init__(self, code, *, uncertain=False, retryable=False, request_id=None,
                 content='', usage=None, diagnostics=None):
        super().__init__(code)
        self.code, self.uncertain, self.retryable, self.request_id = code, uncertain, retryable, request_id
        self.content, self.usage, self.diagnostics = content, usage or {'known': False}, diagnostics or {}


@dataclass(frozen=True)
class ProviderResult:
    content: str
    finish_reason: str
    usage: dict
    request_id: str | None
    diagnostics: dict = field(default_factory=dict)


def endpoint_for(provider, region):
    try:
        return ENDPOINTS[provider][region]
    except KeyError:
        raise ValueError('不支持的服务商或官方地域') from None


def _usage(payload):
    usage = payload.get('usage') or {}
    inp = usage.get('prompt_tokens', usage.get('input_tokens'))
    out = usage.get('completion_tokens', usage.get('output_tokens'))
    if not isinstance(inp, int) or not isinstance(out, int) or inp < 0 or out < 0:
        return {'known': False}
    return {'known': True, 'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out}


def complete(profile, api_key, messages, *, max_tokens=None, streaming=False, on_content=None, on_progress=None):
    """A fresh session avoids shared credentials/proxies and SDK retry behavior."""
    url = endpoint_for(profile['provider'], profile['region'])
    budget = min(int(max_tokens or profile.get('max_output_tokens', 4096)), 16384)
    streaming = bool(streaming and profile['provider'] == 'deepseek')
    common = {'model': profile['model'], 'stream': streaming}
    if profile['provider'] == 'dashscope':
        body = {**common, 'input': {'messages': messages}, 'parameters': {
            'result_format': 'message', 'max_tokens': budget, 'temperature': 0.1,
            'response_format': {'type': 'json_object'}, 'enable_thinking': False}}
    else:
        body = {**common, 'messages': messages, 'max_tokens': budget, 'temperature': 0.1,
                'response_format': {'type': 'json_object'}}
        if profile['provider'] == 'deepseek':
            # These bounded classification/summary calls need the JSON answer;
            # default thinking can consume the output budget before it arrives.
            body['thinking'] = {'type': 'disabled'}
    if streaming:
        body['stream_options'] = {'include_usage': True}
    session = requests.Session()
    session.trust_env = False
    started = time.monotonic()
    request_id = None
    content, usage, received = '', {'known': False}, 0
    def diagnostics():
        return {'elapsed_seconds': round(time.monotonic() - started, 3), 'received_bytes': received,
                'streaming': streaming}
    def failure(code):
        return ProviderError(code, uncertain=True, retryable=True, request_id=request_id,
                             content=content, usage=usage, diagnostics=diagnostics())
    try:
        with session.post(url, headers={'Authorization': 'Bearer ' + api_key,
                          'Content-Type': 'application/json'}, json=body,
                          timeout=(8, 120 if streaming else 60), allow_redirects=False, stream=True) as response:
            request_id = response.headers.get('x-request-id') or response.headers.get('x-dashscope-request-id')
            if 300 <= response.status_code < 400:
                raise ProviderError('redirect_rejected', request_id=request_id)
            if response.status_code in (401, 403):
                raise ProviderError('authentication_failed', request_id=request_id)
            if response.status_code == 402:
                raise ProviderError('budget_exhausted', request_id=request_id)
            if response.status_code == 429:
                raise ProviderError('rate_limited', retryable=True, request_id=request_id)
            if response.status_code >= 500:
                raise ProviderError('provider_unavailable', uncertain=True, request_id=request_id)
            if response.status_code != 200:
                raise ProviderError('request_rejected', request_id=request_id)
            if streaming:
                # Bound each socket read by the remaining total allowance. This
                # also bounds a read that begins just before the total deadline.
                raw = getattr(response, 'raw', None)
                stream_socket = getattr(getattr(raw, '_connection', None), 'sock', None)
                if stream_socket is None:
                    try:
                        stream_socket = raw._fp.fp.raw._sock
                    except AttributeError:
                        pass
                def bounded_chunks():
                    chunks = iter(response.iter_content(chunk_size=1))
                    while True:
                        remaining = STREAM_TOTAL_SECONDS - (time.monotonic() - started)
                        if remaining <= 0:
                            raise failure('response_deadline')
                        if stream_socket is not None:
                            stream_socket.settimeout(min(120, remaining))
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            return
                        yield chunk
                decoder = codecs.getincrementaldecoder('utf-8')()
                buffer, lines, finish, ended = '', [], '', False
                for chunk in bounded_chunks():
                    received += len(chunk)
                    if received > 2_000_000:
                        raise failure('response_too_large')
                    if time.monotonic() - started > STREAM_TOTAL_SECONDS:
                        raise failure('response_deadline')
                    if on_progress:
                        on_progress(diagnostics())
                    buffer += decoder.decode(chunk)
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.rstrip('\r')
                        if line:
                            if line.startswith('data:'):
                                lines.append(line[5:].lstrip())
                            continue
                        if not lines:
                            continue
                        raw, lines = '\n'.join(lines), []
                        if raw == '[DONE]':
                            ended = True
                            break
                        try:
                            payload = json.loads(raw)
                            request_id = payload.get('id') or request_id
                            if payload.get('usage'):
                                usage = _usage(payload)
                            for choice in payload.get('choices', []):
                                if choice.get('index', 0) != 0:
                                    continue
                                delta = choice.get('delta') or {}
                                value = delta.get('content') or ''
                                if not isinstance(value, str):
                                    raise ValueError('non-text content')
                                content += value
                                finish = choice.get('finish_reason') or finish
                                if value and on_content:
                                    on_content(content)
                        except (ValueError, KeyError, TypeError):
                            raise failure('invalid_stream_event') from None
                    if ended:
                        break
                if time.monotonic() - started >= STREAM_TOTAL_SECONDS:
                    raise failure('response_deadline')
                if not ended or not finish:
                    raise failure('stream_incomplete')
                return ProviderResult(content, finish, usage, request_id, diagnostics())
            data = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                data.extend(chunk)
                if len(data) > 2_000_000 or time.monotonic() - started > 90:
                    raise ProviderError('response_limit', uncertain=True, request_id=request_id)
            try:
                payload = json.loads(data)
                if profile['provider'] == 'dashscope':
                    if payload.get('code'):
                        raise ProviderError('request_rejected', request_id=payload.get('request_id'))
                    choice = payload['output']['choices'][0]
                else:
                    choice = payload['choices'][0]
                content = choice['message']['content']
                if not isinstance(content, str):
                    raise ValueError('non-text content')
                return ProviderResult(content=content, finish_reason=choice.get('finish_reason') or '',
                                      usage=_usage(payload), request_id=payload.get('request_id') or payload.get('id') or request_id)
            except (KeyError, IndexError, TypeError, ValueError):
                raise ProviderError('invalid_response', uncertain=True, request_id=request_id) from None
    except requests.ConnectTimeout:
        raise ProviderError('connect_timeout', retryable=True) from None
    except requests.ReadTimeout:
        if streaming:
            raise failure('response_deadline' if time.monotonic() - started >= STREAM_TOTAL_SECONDS else 'read_timeout') from None
        raise ProviderError('network_result_unknown', uncertain=True, request_id=request_id) from None
    except (requests.ConnectionError, requests.RequestException) as exc:
        if streaming:
            # requests wraps urllib3's idle-read timeout in ConnectionError.
            code = 'read_timeout' if 'ReadTimeout' in type(exc.__context__).__name__ or 'Read timed out' in str(exc) else 'connection_lost'
            raise failure('response_deadline' if time.monotonic() - started >= STREAM_TOTAL_SECONDS else code) from None
        raise ProviderError('network_result_unknown', uncertain=True, request_id=request_id) from None
    except UnicodeDecodeError:
        raise failure('invalid_stream_encoding') from None
    finally:
        session.close()
