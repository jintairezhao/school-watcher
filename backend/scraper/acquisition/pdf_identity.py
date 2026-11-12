"""Re-execute a conservative title witness on hash-checked PDF bytes."""
from io import BytesIO
import hashlib
import re

VERSION = 'pdf-cover-title-1'
MAX_CONTENT_BYTES = 4 * 1024 * 1024


def cover_title(payload, title):
    """Only a full title at the start of page one can supply this witness."""
    normal = lambda text: re.sub(r'[\W_]+', '', text or '').casefold()
    proof = dict(matched=False, validator_version=VERSION, page=1,
                 content_hash=hashlib.sha256(payload).hexdigest())
    if not title or not normal(title) or len(title) > 500:
        return dict(proof, reason='pdf_title_unusable')
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return dict(proof, reason='pdf_text_extractor_unavailable')
    try:
        reader = PdfReader(BytesIO(payload), strict=True)
        if reader.is_encrypted:
            return dict(proof, reason='pdf_encrypted')
        if not len(reader.pages):
            return dict(proof, reason='pdf_page_missing')
        page = reader.pages[0]
        contents = page.get_contents()
        if contents is None:
            return dict(proof, reason='pdf_cover_text_missing')
        if len(contents.get_data()) > MAX_CONTENT_BYTES:
            return dict(proof, reason='pdf_cover_content_limit')
        text = page.extract_text() or ''
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return dict(proof, reason='pdf_cover_unreadable')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return dict(proof, reason='pdf_cover_text_missing')
    observed = []
    expected = normal(title)
    for line in lines[:6]:
        observed.append(line)
        value = normal(''.join(observed))
        if value == expected:
            return dict(proof, matched=True, reason='matched', observed_title='\n'.join(observed),
                        locator=f'pdf:page:1:nonempty-lines:1-{len(observed)}')
        # A title mentioned later in prose/contents cannot identify the file.
        if len(value) > 500 or not expected.startswith(value):
            break
    return dict(proof, reason='pdf_cover_title_unconfirmed')
