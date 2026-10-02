"""Is this address the site's own sign-in route?

A sign-on hand-off is observable in two different places, and both must judge it
the same way:

* the classifier, when the identity provider's page is what comes back;
* the redirect hop in the HTTP client, which is the only layer that knows the
  target when the hand-off itself cannot be completed. A university identity
  provider that refuses the TLS connection never produces a page to classify, so
  without this the site's own access rule reaches the reader as "check your
  extraction rules" -- a program fault that does not exist.

Both callers use these predicates rather than their own copies, so the two
channels cannot drift apart.
"""
from urllib.parse import parse_qsl, urlsplit
import re

# A sign-in route is a property of the *document*, so only the last path segment
# is considered, with any extension removed. Matching the whole path instead
# would call a published notice at /news/login.html an identity provider, and
# real notice lists are routinely reached through portal hops whose paths contain
# these words. Anything ending in "login" counts, which covers /cas/login,
# /jalogin and /xtgl/login_slogin.html without naming any one vendor.
AUTH_ROUTE = re.compile(
    r'(?:cas|sso|passport|shibboleth|oauth2?|openid|adfs|signin|sign-?in|auth|authorize'
    r'|(?:[a-z0-9_-]*login)|(?:login[a-z0-9_-]*))', re.I)
# A single sign-on hand-off names the address it will return the visitor to. The
# generic navigation parameters ordinary lists use for filtering -- continue,
# goto, target -- are deliberately absent: they carry no authentication meaning.
HANDOFF = re.compile(r'(?:service|redirect_uri|returnurl|return_url|returl|rurl|backurl|back_url)', re.I)

LOGIN_REQUIRED_MESSAGE = '该通知需要登录校内账号才能查看；列表与其余通知不受影响，已有通知仍保留'


def sign_in_segment(path):
    """The last path segment without its extension: /cas/login.jsp -> login."""
    return path.rstrip('/').rsplit('/', 1)[-1].rsplit('.', 1)[0]


def names_a_handoff(query):
    """True when the address carries a hand-off naming where to return.

    The value must look like the address it promises, which is what separates a
    hand-off from an incidental parameter of the same name.
    """
    for name, value in parse_qsl(query, keep_blank_values=False):
        if HANDOFF.fullmatch(name) and value.strip().startswith(('http://', 'https://', '/')):
            return True
    return False


def is_sign_in_route(url):
    """True when this address is a sign-in route, judged from the address alone."""
    parts = urlsplit(url or '')
    return bool(parts.netloc) and bool(AUTH_ROUTE.fullmatch(sign_in_segment(parts.path))) \
        and names_a_handoff(parts.query)
