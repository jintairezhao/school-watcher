"""Transport-neutral acquisition values. No Flask or database dependencies."""
from dataclasses import asdict, dataclass, field, fields
from typing import Any


OUTCOMES = frozenset({'usable', 'empty', 'requires_render', 'needs_manual', 'denied',
                      'network_error', 'needs_adapter', 'unavailable', 'busy'})


@dataclass(frozen=True)
class FetchRequest:
    url: str
    purpose: str = 'directory'
    source_id: str = ''
    policy_version: str = '1'
    timeout_seconds: float = 45
    readiness_selector: str = ''
    expected_response_url: str = ''
    request_id: str = ''
    session_id: str = ''
    browser_allowed: bool = True
    policy: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.purpose not in ('directory', 'list', 'article'):
            raise ValueError('Unknown acquisition purpose')
        if not 0 < self.timeout_seconds <= 90:
            raise ValueError('Acquisition budget must be between 0 and 90 seconds')
        if not isinstance(self.policy, dict):
            raise ValueError('Acquisition policy must be an object')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        allowed = {f.name for f in fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in allowed})


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    status: int = 0
    html: str = ''
    json_data: Any = None
    transport: str = 'http'
    outcome: str = 'needs_adapter'
    error_code: str = ''
    message: str = ''
    evidence: tuple = ()
    timings: dict = field(default_factory=dict)
    retry_after: float | None = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.outcome in ('usable', 'empty')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value.setdefault('final_url', value.get('url', ''))
        value.setdefault('status', value.get('status_code', 0))
        value['evidence'] = tuple(value.get('evidence') or ())
        allowed = {f.name for f in fields(cls)}
        return cls(**{key: item for key, item in value.items() if key in allowed})


class FetchedHTML(str):
    """Compatibility text that retains final URL and typed metadata for old parsers."""
    def __new__(cls, result):
        value = super().__new__(cls, result.html)
        value.result = result
        value.final_url = result.final_url
        return value
