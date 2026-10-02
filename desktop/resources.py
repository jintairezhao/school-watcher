"""Explicit, testable resource manifest for the frozen application."""
from pathlib import Path
import runpy
import hashlib
import json


def application_data(root, build):
    root, build = Path(root), Path(build)
    data = []
    for folder in ('frontend', 'config', 'migrations', 'licenses', 'desktop/ui', 'backend/ai/skills'):
        for source in (root / folder).rglob('*'):
            if (source.is_file() and '__pycache__' not in source.parts
                    and source.suffix not in ('.pyc', '.pyo')
                    and source.name not in ('desktop-schools.json', 'schools.yaml')):
                data.append((str(source), str(source.parent.relative_to(root))))
    revision = runpy.run_path(str(root / 'backend/scraper/discovery/parser_revision.py'))['PARSER_REVISION']
    build.mkdir(parents=True, exist_ok=True)
    manifest = build / 'parser-revision.txt'
    manifest.write_text(revision + '\n', encoding='ascii')
    data.append((str(manifest), 'config'))
    sources = {}
    for folder in ('backend', 'frontend', 'desktop', 'config', 'migrations', 'scripts'):
        for source in sorted((root / folder).rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix in ('.py', '.js', '.css', '.html', '.json', '.md', '.yaml') and source.name not in ('desktop-schools.json', 'schools.yaml'):
                sources[source.relative_to(root).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    build_info = build / 'build-info.json'
    build_info.write_text(json.dumps({'source_digest': hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
                                     'files': sources}, indent=2), encoding='utf-8')
    data.append((str(build_info), 'config'))
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        data.append((str(root / name), '.'))
    return data
