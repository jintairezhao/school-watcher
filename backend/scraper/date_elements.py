"""Read explicit date elements, including a renderer's date-only attribute."""
import re
import soupsieve


COMPLETE_DATE = re.compile(r'20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?')


def publication_date_text(element):
    if element is None:
        return ''
    if element.get('datetime'):
        return element['datetime']
    if element.has_attr('data-title'):
        # jFlex copies this markup into its visible slide control. Read only
        # the reviewed date-only format; never render or execute the attribute.
        match = re.fullmatch(r'\s*<strong>\s*(\d{1,2})\s*</strong>\s*'
                             r'<i>\s*(20\d{2})[./-](\d{1,2})\s*</i>\s*',
                             element['data-title'])
        if not match:
            return ''
        day, year, month = match.groups()
        return f'{year}-{month}-{day}'
    text = element.get_text(' ', strip=True)
    split_calendar = re.fullmatch(r'\s*(\d{1,2})\s+(20\d{2})[-/.](\d{1,2})\s*', text)
    if split_calendar:
        day, year, month = split_calendar.groups()
        return f'{year}-{month}-{day}'
    # Some templates display month-day first and the complete year underneath.
    reverse = re.fullmatch(r'\s*(\d{1,2})[-/.](\d{1,2})\s+(20\d{2})\s*', text)
    if reverse:
        month, day, year = reverse.groups()
        return f'{year}-{month}-{day}'
    return text


def infer_publication_date_selector(items):
    """Infer a repeatable date-only element, never a date quoted in prose/title."""
    from collections import Counter
    candidates = Counter()
    for item in items:
        dated = []
        for node in item.find_all(['time', 'span', 'em', 'i', 'div', 'p']):
            ancestors = []
            for ancestor in node.parents:
                if ancestor is item:
                    break
                ancestors.append(ancestor)
            if any(a.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') or any(
                    re.search(r'title|summary|abstract|description', c, re.I) for c in a.get('class', []))
                   for a in ancestors):
                continue
            text = publication_date_text(node)
            if not COMPLETE_DATE.fullmatch(text):
                continue
            classes = node.get('class', [])
            explicit = node.name == 'time' or any(re.search(r'date|time', c, re.I) for c in classes)
            # A bare span/em immediately beside an article link is also explicit
            # layout evidence. Dates nested in summaries or headings are not.
            adjacent = node.parent is item and node.name in ('span', 'em', 'i')
            if not explicit and not adjacent:
                continue
            selector = node.name + ''.join('.' + soupsieve.escape(c) for c in classes)
            if not classes:
                position = 1 + len(node.find_previous_siblings(node.name))
                selector += f':nth-of-type({position})'
            dated.append((selector, text))
        # Multiple distinct dates may be event start/end times. Leave unresolved.
        if len({text for _, text in dated}) != 1:
            continue
        for selector in set(selector for selector, _ in dated):
            if len(item.select(selector)) == 1:
                candidates[selector] += 1
    if not candidates:
        return ''
    selector, count = max(candidates.items(), key=lambda p: (p[1], len(p[0]), p[0]))
    return selector if count >= max(2, len(items) * 0.6) else ''
