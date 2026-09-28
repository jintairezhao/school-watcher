"""One HTML5 allowlist for collected, imported and previously cached article HTML."""
from bs4 import BeautifulSoup
import nh3


_CLEANER = nh3.Cleaner(
    tags={'a', 'abbr', 'b', 'blockquote', 'br', 'caption', 'code', 'col', 'colgroup',
          'dd', 'del', 'div', 'dl', 'dt', 'em', 'figcaption', 'figure', 'h1', 'h2',
          'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'li', 'ol', 'p', 'pre', 's',
          'small', 'span', 'strong', 'sub', 'sup', 'table', 'tbody', 'td', 'th',
          'thead', 'tfoot', 'tr', 'u', 'ul'},
    clean_content_tags={'script', 'style', 'iframe', 'object', 'embed', 'form',
                        'svg', 'math', 'template', 'noscript'},
    attributes={'*': {'title', 'lang', 'dir', 'style'},
                'a': {'href'}, 'img': {'src', 'alt', 'width', 'height'},
                'td': {'colspan', 'rowspan'}, 'th': {'colspan', 'rowspan', 'scope'},
                'col': {'span'}, 'colgroup': {'span'}, 'ol': {'start', 'reversed'},
                'li': {'value'}},
    url_schemes={'http', 'https', 'mailto', 'tel'},
    link_rel='noopener noreferrer',
    set_tag_attribute_values={'a': {'target': '_blank'}},
    filter_style_properties={'text-align', 'vertical-align', 'font-weight',
                             'font-style', 'text-decoration', 'white-space',
                             'border-collapse', 'border-spacing', 'border-style',
                             'border-width', 'padding', 'padding-left', 'padding-right',
                             'padding-top', 'padding-bottom'},
)


def sanitize_html(html_str: str) -> str:
    """Remove executable markup, including in previously cached and imported HTML."""
    if not html_str or not html_str.strip():
        return html_str or ''
    # Presentation cleanup precedes the final HTML5 security parser. Do not
    # serialize the sanitized fragment through another HTML parser afterward.
    soup = BeautifulSoup(html_str, 'lxml')
    for text in soup.find_all(string=lambda t: t and t.strip().startswith(('当前位置：', '当前位置:'))):
        block = text.find_parent(['div', 'p', 'nav'])
        if block:
            block.decompose()
    return _CLEANER.clean(str(soup))
