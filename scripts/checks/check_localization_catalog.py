"""Check explicitly registered UI copy, plural variants and interpolation contracts."""
import ast
import html
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r'\{(\w+)\}')
CALL = re.compile(r'''\b(?:uiT|tr|t|ui_text|setMessage)\(\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')
MARKER = re.compile(r'''data-i18n(?:-(?:title|placeholder|aria-label|alt))?="([^"\n]+)"''')


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
            messages=[]
            for match in CALL.finditer(source):
                try:messages.append(ast.literal_eval(match[1]))
                except (SyntaxError,ValueError):continue
            if extension=='*.html':messages.extend(html.unescape(m[1]) for m in MARKER.finditer(source) if '{{' not in m[1])
            for message in set(messages):
                if re.search('[\u4e00-\u9fff]',message) and message not in catalog:
                    problems.append(str(path.relative_to(root))+': missing '+message)
    return {'catalog_entries':len(catalog),'problems':sorted(set(problems)),
            'scope':'Explicit UI messages only; original website content is excluded.'}


if __name__=='__main__':
    result=audit();print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(bool(result['problems']))
