"""One message catalog for HTTP pages and independent native windows."""
from functools import lru_cache
import json
from pathlib import Path
import re


@lru_cache(maxsize=1)
def english_catalog():
    return json.loads((Path(__file__).resolve().parents[1] / 'frontend/static/i18n/en.json').read_text('utf8'))


@lru_cache(maxsize=1)
def message_patterns():
    result = []
    for key in english_catalog():
        names = re.findall(r'\{(\w+)\}', key)
        if names:
            fragments = re.split(r'\{\w+\}', key)
            result.append((key, names, re.compile('(.+?)'.join(map(re.escape, fragments)))))
    return result


def translate_for_locale(message, locale, **variables):
    text = english_catalog().get(message, message) if locale == 'en' else message
    if locale == 'en' and message not in english_catalog() and isinstance(message, str) and len(message) < 2000:
        for key, names, pattern in message_patterns():
            match = pattern.fullmatch(message)
            if match:
                values = dict(zip(names, match.groups()))
                if 'count' in values and re.fullmatch(r'[\d,]+', values['count']):
                    values['count'] = int(values['count'].replace(',', ''))
                return translate_for_locale(key, locale, **values)
    if isinstance(text, dict):
        text = text.get('one' if variables.get('count') == 1 else 'other', text.get('other', message))
    for key, value in variables.items():
        if locale == 'en' and key == 'audience':
            value = ', '.join(translate_for_locale(role.strip(), locale) for role in re.split(r'[、＋,]', str(value)))
        text = text.replace('{' + key + '}', str(value))
    return text
