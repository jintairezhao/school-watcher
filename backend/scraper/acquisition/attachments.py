"""Small, bounded file observations; never execute or install document contents."""
import hashlib
import re
from email.message import Message
from urllib.parse import unquote, urlsplit


def pdf_observation(payload, url, headers, *, retain=False):
    """A complete PDF response is evidence, not a promise its title matches."""
    if not payload.startswith(b'%PDF-') or b'%%EOF' not in payload[-1024:]:
        return None
    message = Message()
    message['Content-Disposition'] = headers.get('Content-Disposition', '')
    filename = message.get_filename() or unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    filename = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
    observation = {'format': 'pdf', 'verified': True, 'filename': filename[:1000],
            'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
            'verification': 'complete_pdf_signature_and_eof'}
    if retain:
        from flask import has_app_context
        if has_app_context():
            from backend.services.source_governance import _evidence_root
            root = _evidence_root()
            root.mkdir(parents=True, exist_ok=True)
            path = root / (observation['sha256'] + '.pdf')
            if not path.exists():
                try:
                    with path.open('xb') as stream:
                        stream.write(payload)
                except FileExistsError:
                    pass
            read_pdf_material(root, observation['sha256'])
    return observation


def read_pdf_material(root, digest):
    """Recheck retained bytes; a metadata flag cannot replace the document."""
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('PDF材料指纹无效')
    try:
        with (root / (digest + '.pdf')).open('rb') as stream:
            payload = stream.read(8 * 1024 * 1024 + 1)
    except OSError:
        raise ValueError('PDF原文材料缺失') from None
    if (len(payload) > 8 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != digest
            or not payload.startswith(b'%PDF-') or b'%%EOF' not in payload[-1024:]):
        raise ValueError('PDF原文材料与指纹不符')
    return payload


def pdf_sample_identity(attachment, title, requested_url, final_url, *, page_html=''):
    """Keep title agreement distinct from limited identity at a literal URL."""
    normal = lambda value: re.sub(r'[\W_]+', '', value or '').casefold()
    filename = attachment.get('filename', '')
    stem = re.sub(r'\.pdf$', '', filename, flags=re.I)
    # Numeric CMS keys are opaque regardless of their length. They make no
    # title claim; a complete file at the exact listed URL is limited evidence.
    opaque = bool(re.fullmatch(r'\d+|[a-fA-F0-9_-]{6,}', stem))
    valid = (attachment.get('format') == 'pdf' and attachment.get('verified') is True
             and type(attachment.get('bytes')) is int and attachment['bytes'] > 0
             and isinstance(attachment.get('sha256'), str)
             and re.fullmatch('[0-9a-f]{64}', attachment['sha256']))
    embedding = attachment.get('embedding')
    if embedding:
        from .embedded_pdf import valid_binding
        valid = bool(valid and valid_binding(attachment, page_html, final_url))
    resource_url = attachment.get('resource_url') if embedding else final_url
    matched = bool(valid and normal(title) and (normal(stem) == normal(title)
                              or opaque and not embedding and requested_url == final_url))
    cover = None
    if valid and not matched and normal(title):
        from flask import has_app_context
        if has_app_context():
            from backend.services.source_governance import _evidence_root
            from .pdf_identity import cover_title
            try:
                payload = read_pdf_material(_evidence_root(), attachment['sha256'])
                if len(payload) == attachment['bytes']:
                    cover = cover_title(payload, title)
                else:
                    cover = dict(matched=False, reason='pdf_byte_count_mismatch')
            except ValueError:
                cover = dict(matched=False, reason='pdf_original_unavailable')
            matched = bool(cover.get('matched'))
    return dict(matched=matched, attachment=True, attachment_urls=[resource_url] if resource_url else [],
        attachment_evidence=dict(attachment), content_hash=attachment.get('sha256'),
        reason='matched' if matched else 'attachment_identity_unconfirmed',
        body_basis=('verified_pdf_cover_title' if cover and cover.get('matched') else
                    'verified_pdf_and_observed_opaque_url' if opaque else 'verified_pdf_and_matching_filename'),
        limited_identity=bool(opaque and not embedding), **({'cover_identity': cover} if cover is not None else {}))
