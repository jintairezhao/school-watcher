"""Async admission client; the main database remains the only rate-limit owner."""
import asyncio
import hashlib
import os
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout


class OriginAdmission:
    def __init__(self, payload, *, ttl=120):
        self.url = os.environ.get('WATCHER_ORIGIN_BROKER_URL', '').rstrip('/')
        self.secret = os.environ.get('WATCHER_BROWSER_TOKEN', '')
        self.initial = (urlsplit(payload['url']).hostname or '').lower().rstrip('.')
        self.identity = payload.get('request_id') or hashlib.sha256(
            (str(payload.get('id')) + ':' + str(payload.get('generation'))).encode()).hexdigest()
        self.ttl = ttl
        self.tokens = set()
        self.lock = asyncio.Lock()
        self.client = None

    async def _request(self, action, data):
        if self.client is None:
            self.client = ClientSession(timeout=ClientTimeout(total=5), trust_env=False)
        async with self.client.post(self.url + '/' + action, json=data,
                headers={'X-Watcher-Token': self.secret}) as response:
            response.raise_for_status()
            return await response.json()

    async def admit(self, url):
        if not self.url:
            return  # Standalone fixture mode; launchers configure the broker.
        host = (urlsplit(url).hostname or '').lower().rstrip('.')
        token = self.identity if host == self.initial else hashlib.sha256((self.identity + ':' + host).encode()).hexdigest()
        async with self.lock:
            if token in self.tokens:
                return
            if len(self.tokens) >= 32:
                raise RuntimeError('Too many document origins')
            result = await self._request('reserve', {'url': url, 'token': token, 'ttl': self.ttl})
            if not result.get('allowed'):
                raise OriginBusy(result.get('retry_after', 2))
            self.tokens.add(token)

    async def close(self):
        if self.url:
            for token in self.tokens:
                try:
                    await self._request('release', {'token': token})
                except Exception:
                    pass  # A failed callback cannot keep an expiring lease forever.
        self.tokens.clear()
        if self.client:
            await self.client.close()


class OriginBusy(Exception):
    def __init__(self, retry_after=2):
        self.retry_after = max(1, min(float(retry_after), 900))
        super().__init__('Origin is cooling down or serving another request')
