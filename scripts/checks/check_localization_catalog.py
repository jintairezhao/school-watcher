"""Check UI copy, including unregistered text and conditional tooltips.

Only application literals are inspected. Values supplied by official pages are
Jinja/JavaScript expressions and are deliberately never translated by this audit.
"""
import ast
import html
import json
from pathlib import Path
import re
from bs4 import BeautifulSoup, Comment

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r'\{(\w+)\}')
CALL = re.compile(r'''\b(?:uiT|tr|t|ui_text|setMessage)\(\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')
MARKER = re.compile(r'''data-i18n(?:-(?:title|placeholder|aria-label|alt))?="([^"\n]+)"''')
HAN = re.compile(r'[\u4e00-\u9fff]')
QUOTED = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''.strip()


def unregistered_template_copy(source):
    masked = re.sub(r'{#.*?#}|{%.*?%}|{{.*?}}', lambda m:' ' * len(m[0]), source, flags=re.S)
    soup = BeautifulSoup(masked, 'html.parser')
    problems = []
    for node in soup.find_all(string=True):
        value = str(node).strip()
        if isinstance(node, Comment) or not HAN.search(value):
            continue
        if node.find_parent(['script','style']) or node.find_parent(attrs={'data-i18n':True}):
            continue
        if node.parent.name == 'option' and node.parent.get('value') == 'zh-CN' and value == '中文':
            continue  # A language's native name stays recognizable in the selector.
        problems.append('Unregistered UI text: ' + value)
    # Parse attributes before HTML parsers merge duplicates across Jinja branches.
    # A marker for a *different* tooltip in an else branch cannot cover this one.
    for tag in re.findall(r'<[a-zA-Z][^>]*>', masked):
        for attribute,value in re.findall(r'''\s(placeholder|aria-label|title|alt)="([^"]*)"''', tag):
            if HAN.search(value):
                markers = re.findall(r'\sdata-i18n-' + attribute + r'="([^"]*)"', tag)
                if value not in markers:
                    problems.append('Unregistered ' + attribute + ': ' + value)
    return problems


def unregistered_script_copy(source, catalog=None):
    problems, stack = [], []
    tokens = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|'+QUOTED+r'|`(?:\\.|[^`\\])*`|[()]')
    for match in tokens.finditer(source):
        raw = match[0]; prefix = source[:match.start()]
        if raw == '(':
            call = re.search(r'(\w+)\s*$',prefix)
            stack.append(call[1] if call else '')
            continue
        if raw == ')':
            if stack: stack.pop()
            continue
        if raw.startswith('/') or not HAN.search(raw):
            continue
        if raw.startswith('`') and not HAN.search(re.sub(r'\$\{(?:uiT|tr|t)\([^}]*\)\}', '', raw)):
            continue
        if any(call in ('uiT','tr','t','setMessage','setText') for call in stack):
            if catalog is not None and raw[0] != '`':
                try: message=ast.literal_eval(raw)
                except (SyntaxError,ValueError): message=''
                if HAN.search(message) and message not in catalog:
                    problems.append('Missing script UI translation: '+message)
            continue
        # Object keys and selectors can be stable protocol identifiers. A ternary
        # value followed by ':' is *not* an object key, including on another line.
        if re.match(r'\s*:',source[match.end():]) and re.search(r'[{,]\s*$',prefix):
            continue
        if raw[1:].startswith('[data-'):
            continue
        problems.append('Unregistered script UI text: '+raw[1:-1])
    return problems


def audit(root=ROOT):
    problems=[]
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:problems.append('Duplicate translation key: '+key)
            result[key]=value
        return result
    catalog=json.loads((root/'frontend/static/i18n/en.json').read_text('utf8'),object_pairs_hook=unique)
    for key,translated in catalog.items():
        variants=translated.values() if isinstance(translated,dict) else [translated]
        if isinstance(translated,dict) and not {'one','other'}<=translated.keys():
            problems.append('Plural variants missing: '+key)
        for value in variants:
            if not isinstance(value,str) or not value.strip():problems.append('Empty translation: '+key)
            elif set(PLACEHOLDER.findall(value))!=set(PLACEHOLDER.findall(key)):
                problems.append('Interpolation mismatch: '+key)
    for folder,extension in [('frontend/templates','*.html'),('frontend/static/js','*.js')]:
        for path in (root/folder).glob(extension):
            source=path.read_text('utf8')
            # Locale formatters and the evidence-only graph model contain
            # canonical date units/navigation names rather than rendered UI copy.
            unregistered = (unregistered_template_copy(source) if extension == '*.html'
                            else [] if path.name in ('i18n.js','discovery-atlas-model.js')
                            else unregistered_script_copy(source,catalog))
            if extension == '*.html':
                for script in BeautifulSoup(source,'html.parser').find_all('script'):
                    if script.get('type') not in ('application/json','importmap'):
                        unregistered.extend(unregistered_script_copy(script.get_text(),catalog))
            problems.extend(str(path.relative_to(root))+': '+item for item in unregistered)
            messages=[]
            for match in CALL.finditer(source):
                try:messages.append(ast.literal_eval(match[1]))
                except (SyntaxError,ValueError):continue
            if extension=='*.html':messages.extend(html.unescape(m[1]) for m in MARKER.finditer(source) if '{{' not in m[1])
            for message in set(messages):
                if re.search('[\u4e00-\u9fff]',message) and message not in catalog:
                    problems.append(str(path.relative_to(root))+': missing '+message)
    return {'catalog_entries':len(catalog),'problems':sorted(set(problems)),
            'scope':'UI messages, literal text and tooltip attributes; original website values are excluded.'}


if __name__=='__main__':
    result=audit();print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(bool(result['problems']))
