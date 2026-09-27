"""Stable acquisition API, usable independently of the web application."""
from .contracts import FetchRequest, FetchResult, FetchedHTML
from .classifier import classify_result
from .profiles import profile_fingerprint
from .coordinator import FetchFailure, FetchDeferred, execution_context, fetch, fetch_or_raise

__all__ = ['FetchRequest', 'FetchResult', 'FetchedHTML', 'FetchFailure', 'FetchDeferred',
           'classify_result', 'profile_fingerprint', 'execution_context', 'fetch', 'fetch_or_raise']
