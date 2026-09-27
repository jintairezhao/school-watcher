"""Preserve actionable causes and avoid immediate retries for access/format failures."""
import requests


class SourceAccessError(RuntimeError):
    retryable = False


class SourceNetworkError(RuntimeError):
    retryable = True


def http_failure(status):
    if status == 429:
        return SourceAccessError('官网提示请求过于频繁（HTTP 429），已停止本轮重试')
    if status in (401, 403):
        return SourceAccessError(f'官网拒绝本次访问（HTTP {status}），已停止本轮重试')
    if status in (404, 410):
        return SourceAccessError(f'官网栏目地址不存在或已撤下（HTTP {status}），需要核对来源地址')
    if status >= 500:
        return SourceNetworkError(f'官网服务暂时不可用（HTTP {status}），稍后重试')
    return SourceAccessError(f'官网返回异常状态（HTTP {status}），请核对来源状态')


def describe_fetch_error(error):
    if isinstance(error, (SourceAccessError, SourceNetworkError)):
        return error
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return http_failure(error.response.status_code)
    if isinstance(error, requests.Timeout):
        return SourceNetworkError('连接或读取官网超时，请稍后重试；已有消息仍保留')
    if 'No public IPv4 address could be verified' in str(error):
        return SourceNetworkError('官网域名未能解析到可验证的公网地址，请检查网络解析；已有消息仍保留')
    if isinstance(error, requests.ConnectionError) or 'Public campus endpoint did not respond' in str(error):
        return SourceNetworkError(f'官网网络连接失败：{error}')
    return SourceNetworkError(f'官网请求失败（{type(error).__name__}）：{error}')
