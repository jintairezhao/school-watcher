"""Export the public school directory without importing or opening the runtime database."""
import ast
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]


def literal_assignments(path):
    result = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return result


def export(check=False):
    urls = literal_assignments(ROOT / 'backend/scraper/discovery/university_urls.py')
    meta = literal_assignments(ROOT / 'backend/services/catalog.py')
    provinces = {name: province for group in ('MEMBERS_BY_PROVINCE', 'OTHER_PROVINCES')
                 for province, names in meta[group].items() for name in names.split()}
    entries = {}
    for name, url in {**urls['UNIVERSITY_URL_MAP'], **meta['EXTRA_URLS']}.items():
        name = meta['NAME_ALIASES'].get(name, urls['ALIAS_MAP'].get(name, name))
        entries[name] = dict(name=name, url=url, province=provinces.get(name, ''))
    for alias, name in {**urls['ALIAS_MAP'], **meta['NAME_ALIASES']}.items():
        name = meta['NAME_ALIASES'].get(name, name)
        if name in entries and alias != name:
            entries[name].setdefault('aliases', []).append(alias)
    # Independently named campuses must retain their own official website.
    supplements = json.loads((ROOT / 'config/android-school-supplements.json').read_text(encoding='utf-8'))
    for entry in supplements:
        name = entry['name']
        if not name or not entry['url'].startswith('https://') or not entry['province']:
            raise ValueError('Supplement requires a name, official HTTPS URL and province')
        existing = entries.setdefault(name, dict(name=name, url=entry['url'], province=entry['province']))
        if existing['url'].rstrip('/') != entry['url'].rstrip('/'):
            raise ValueError(f'Conflicting official URLs for {name}')
        existing['province'] = entry['province']
        existing['aliases'] = sorted(set(existing.get('aliases', []) + entry.get('aliases', [])))
        presets = entry.get('presets', [])
        seen = set()
        for preset in presets:
            address = urlparse(preset['url'])
            if (not preset['name'].strip() or address.scheme != 'https'
                    or address.hostname != urlparse(entry['url']).hostname
                    or address.username or address.password or preset['url'] in seen):
                raise ValueError(f'Invalid or duplicate official preset for {name}: {preset}')
            seen.add(preset['url'])
        if presets:
            existing['presets'] = presets
    target = ROOT / 'android/app/src/main/assets/schools.json'
    content = json.dumps(list(entries.values()), ensure_ascii=False, indent=2) + '\n'
    if check:
        if not target.exists() or target.read_text(encoding='utf-8') != content:
            raise SystemExit('Android school directory is stale; run python scripts/assets/android_catalog.py')
        print(f'Verified {len(entries)} public school/campus entries')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
    print(f'Exported {len(entries)} public school entries to {target}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify the packaged directory matches its public sources')
    export(check=parser.parse_args().check)
