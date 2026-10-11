"""Small, bounded file observations; never execute or install document contents."""
import hashlib
from email.message import Message
from urllib.parse import unquote, urlsplit


def pdf_observation(payload, url, headers):
    """A complete PDF response is evidence, not a promise its title matches."""
    if not payload.startswith(b'%PDF-') or b'%%EOF' not in payload[-1024:]:
        return None
    message = Message()
    message['Content-Disposition'] = headers.get('Content-Disposition', '')
    filename = message.get_filename() or unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    filename = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
    return {'format': 'pdf', 'verified': True, 'filename': filename[:1000],
            'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
            'verification': 'complete_pdf_signature_and_eof'}
