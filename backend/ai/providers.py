"""Official native HTTP adapters. Exactly one request; never follow redirects."""
from dataclasses import dataclass
import json
import time
import requests

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
    def __init__(self, code, *, uncertain=False, retryable=False, request_id=None):
        super().__init__(code)
        self.code, self.uncertain, self.retryable, self.request_id = code, uncertain, retryable, request_id


@dataclass(frozen=True)
class ProviderResult:
    content: str
    finish_reason: str
    usage: dict
    request_id: str | None


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


def complete(profile, api_key, messages, *, max_tokens=None):
    """A fresh session avoids shared credentials/proxies and SDK retry behavior."""
    url = endpoint_for(profile['provider'], profile['region'])
    budget = min(int(max_tokens or profile.get('max_output_tokens', 4096)), 16384)
    common = {'model': profile['model'], 'stream': False}
    if profile['provider'] == 'dashscope':
        body = {**common, 'input': {'messages': messages}, 'parameters': {
            'result_format': 'message', 'max_tokens': budget, 'temperature': 0.1,
            'response_format': {'type': 'json_object'}, 'enable_thinking': False}}
    else:
        body = {**common, 'messages': messages, 'max_tokens': budget, 'temperature': 0.1,
                'response_format': {'type': 'json_object'}}
    session = requests.Session()
    session.trust_env = False
    started = time.monotonic()
    request_id = None
    try:
        with session.post(url, headers={'Authorization': 'Bearer ' + api_key,
                          'Content-Type': 'application/json'}, json=body,
                          timeout=(8, 60), allow_redirects=False, stream=True) as response:
            request_id = response.headers.get('x-request-id') or response.headers.get('x-dashscope-request-id')
            if 300 <= response.status_code < 400:
                raise ProviderError('redirect_rejected', request_id=request_id)
            if response.status_code in (401, 403):
                raise ProviderError('authentication_failed', request_id=request_id)
            if response.status_code == 429:
                raise ProviderError('rate_limited', retryable=True, request_id=request_id)
            if response.status_code >= 500:
                raise ProviderError('provider_unavailable', uncertain=True, request_id=request_id)
            if response.status_code != 200:
                raise ProviderError('request_rejected', request_id=request_id)
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
    except (requests.ReadTimeout, requests.ConnectionError, requests.RequestException):
        raise ProviderError('network_result_unknown', uncertain=True, request_id=request_id) from None
    finally:
        session.close()
