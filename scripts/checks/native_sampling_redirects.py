"""An explicit, freshly observed IP redirect is not a general host permission."""
from ipaddress import ip_address
from urllib.parse import urlsplit


def allowed_observed_redirect(scope, ledger, document_id, wire_url, host, method):
    if method != 'GET' or not document_id:
        return False
    target = urlsplit(wire_url)
    try:
        if (not ip_address(host).is_global or target.hostname != host or target.scheme != 'https'
                or target.username or target.password or target.fragment or target.port not in (None, 443)):
            return False
    except ValueError:
        return False
    document = next((d for d in ledger['documents'] if d['sequence'] == document_id), None)
    if not document:
        return False
    # The immediately preceding wire response in this document must prove the
    # exact declared transition again; an older batch or another page cannot.
    previous = next((r for r in reversed(ledger['http_attempts']) if r.get('document') == document_id), None)
    if not previous:
        return False
    for permit in scope['website'].get('observed_redirect_permissions', []):
        if (document['request']['url'] == permit['request_url']
                and wire_url == permit['location'] and previous.get('location') == wire_url
                and previous.get('host') == permit['source_host']
                and previous.get('status') == permit['status']
                and permit['status'] in (301, 302, 303, 307, 308)
                and previous.get('finished_at')):
            return True
    return False
