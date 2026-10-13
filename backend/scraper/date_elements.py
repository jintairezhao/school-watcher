"""Read explicit date elements, including a renderer's date-only attribute."""
import re
import soupsieve


COMPLETE_DATE = re.compile(r'20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}日?')


def _date_text(element):
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
    # Calendar tiles can split the year from month-day across child elements.
    forward = re.fullmatch(r'\s*(20\d{2})\s+(\d{1,2})[-/.](\d{1,2})\s*', text)
    if forward:
        year, month, day = forward.groups()
        return f'{year}-{month}-{day}'
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


NON_PUBLICATION = re.compile(r'讲座(?:时间|日期)|活动(?:时间|日期)|会议(?:时间|日期)|举行(?:时间|日期)|'
    r'报名(?:时间|日期|截止)|截止(?:时间|日期)|截至|更新时间|更新日期|修改时间|'
    r'(?:event|lecture)[-_ ]?(?:date|time)|deadline|datemodified|startdate|enddate', re.I)
TIME_RANGE = re.compile(r'\d{1,2}[:：]\d{2}\s*[—–~～至-]+\s*\d{1,2}[:：]\d{2}')


def listing_date_observation(element):
    raw = _date_text(element)
    if not raw:
        return {'role':'missing','text':'','raw':''}
    markers = ' '.join(element.get('class', [])) + ' ' + (element.get('itemprop') or '')
    parent = element.parent
    context = raw
    if parent is not None and not parent.select('a[href]'):
        nearby = parent.get_text(' ',strip=True)
        if len(nearby) < 160 and len(COMPLETE_DATE.findall(nearby)) == 1 and re.match(
                r'^(?:发布日期|发布时间|发布于|发文日期|讲座时间|讲座日期|活动时间|活动日期|报名截止|截止时间|更新时间|更新日期)',nearby):
            context = nearby
        if len(nearby) < 160 and len(COMPLETE_DATE.findall(nearby)) == 1:
            markers += ' ' + ' '.join(parent.get('class',[])) + ' ' + (parent.get('itemprop') or '')
    if NON_PUBLICATION.search(context+' '+markers) or TIME_RANGE.search(raw):
        return {'role':'non_publication','text':'','raw':raw[:300]}
    # A selector spanning titles or prose cannot promote a quoted date to a
    # publication field. Date-only tiles and explicit publication labels remain valid.
    visible = re.sub(r'^(?:发布日期|发布时间|发布于|发文日期)\s*[：:]?\s*','',raw)
    complete = re.match(r'^\s*\d{4}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}',visible)
    english = re.fullmatch(r'\s*(?:[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*',visible)
    if not complete and not english:
        return {'role':'ambiguous','text':'','raw':raw[:300]}
    return {'role':'publication' if re.search(r'发布日期|发布时间|发布于|发文日期',context) else 'listing',
        'text':raw,'raw':raw[:300]}


def publication_date_text(element):
    return listing_date_observation(element)['text']


def infer_publication_date_selector(items):
    """Infer a repeatable date-only element, never a date quoted in prose/title."""
    from collections import Counter
    candidates = Counter()
    for item in items:
        dated = []
        for node in item.find_all(['time', 'span', 'em', 'i', 'div', 'p', 'td']):
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
            # Table-based lists often use an unlabelled date-only cell beside
            # the title. Bind its column position, not a generic div shared by
            # the title and date. A date embedded in prose is not this layout.
            cell = node if node.name == 'td' else node.find_parent('td')
            table_date = (item.name == 'tr' and cell is not None and cell.parent is item
                          and not cell.find('a') and COMPLETE_DATE.fullmatch(publication_date_text(cell)))
            if not explicit and not adjacent and not table_date:
                continue
            selector = node.name + ''.join('.' + soupsieve.escape(c) for c in classes)
            if not classes:
                position = 1 + len(node.find_previous_siblings(node.name))
                selector += f':nth-of-type({position})'
            if table_date:
                cell_selector = f'td:nth-of-type({1 + len(cell.find_previous_siblings("td"))})'
                selector = cell_selector if node is cell else cell_selector + ' ' + selector
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
