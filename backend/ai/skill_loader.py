"""Load reviewed resources deterministically and enforce their narrow JSON contract."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).parent / 'skills'
SKILLS = {'university-source-onboarding', 'summarize-university-notice'}
VALIDATOR_VERSION = '1'


class SkillValidationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_schema(value, schema, path='$'):
    """Validate the JSON Schema subset used by our bundled, trusted contracts.

    No remote refs or model-supplied schemas are interpreted. Unknown keywords fail
    at load time rather than silently relaxing an output contract.
    """
    supported = {'type', 'properties', 'required', 'additionalProperties', 'items',
                 'enum', 'const', 'minLength', 'maxLength', 'minItems', 'maxItems'}
    if set(schema) - supported:
        raise SkillValidationError('unsupported_schema_keyword')
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise SkillValidationError(path + ': const')
    if 'enum' in schema and value not in schema['enum']:
        raise SkillValidationError(path + ': enum')
    if 'type' in schema:
        types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
                 'string': isinstance(value, str), 'integer': type(value) is int,
                 'boolean': type(value) is bool, 'null': value is None}
        if not any(valid.get(t, False) for t in types):
            raise SkillValidationError(path + ': type')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 1_000_000):
            raise SkillValidationError(path + ': string_length')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 10_000):
            raise SkillValidationError(path + ': array_length')
        for index, item in enumerate(value):
            validate_schema(item, schema.get('items', {}), path + '[' + str(index) + ']')
    if isinstance(value, dict):
        if set(schema.get('required', [])) - set(value):
            raise SkillValidationError(path + ': missing_field')
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(props):
            raise SkillValidationError(path + ': unknown_field')
        for key, sub in props.items():
            if key in value:
                validate_schema(value[key], sub, path + '.' + key)


@dataclass(frozen=True)
class LoadedSkill:
    id: str
    version: str
    contract_version: str
    mode: str
    resource_digest: str
    prompt: str
    input_schema: dict
    output_schema: dict


def load_skill(skill_id, mode, version=None):
    if skill_id not in SKILLS:
        raise SkillValidationError('unknown_skill')
    folder = ROOT / skill_id
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if version is not None and version != manifest['version']:
        raise SkillValidationError('skill_version_unavailable')
    if mode not in manifest['modes']:
        raise SkillValidationError('unknown_mode')
    spec = manifest['modes'][mode]
    paths = ['SKILL.md', manifest['input_schema'], spec['schema'], *spec['references']]
    resources = {'manifest.json': canonical(manifest)}
    for name in paths:
        file = (folder / name).resolve()
        if not file.is_relative_to(folder.resolve()):
            raise SkillValidationError('resource_path_outside_skill')
        resources[name] = file.read_text(encoding='utf-8')
    schema = json.loads(resources[spec['schema']])
    prompt = '\n\n'.join([*([] if spec.get('standalone') else [resources['SKILL.md']]), *[resources[n] for n in spec['references']],
                            '只输出符合下列 JSON Schema 的 JSON 对象：', canonical(schema)])
    return LoadedSkill(skill_id, manifest['version'], manifest['contract_version'], mode,
                       digest(resources), prompt, json.loads(resources[manifest['input_schema']]), schema)


def validate_input(skill, evidence):
    validate_schema(evidence, skill.input_schema)
    if len(canonical(evidence)) > 250_000:
        raise SkillValidationError('evidence_too_large')
    if skill.id == 'summarize-university-notice':
        ids = [item['id'] for item in evidence['paragraphs']]
    else:
        ids = [item['candidate_id'] for item in evidence['candidates']]
        evidence_ids = [item['evidence_id'] for item in evidence['evidence']]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise SkillValidationError('duplicate_evidence_id')
    if not ids or len(ids) != len(set(ids)):
        raise SkillValidationError('empty_or_duplicate_candidate_ids')


def _refs(refs, known):
    if len(refs) != len(set(refs)) or set(refs) - set(known):
        raise SkillValidationError('unknown_or_duplicate_evidence')


def _tokens_present(text, corpus):
    # This check prevents fabricated numeric facts and links, not all semantic errors.
    numbers = set(re.findall(r'\d+(?:[.:/-]\d+)*', text))
    known = set(re.findall(r'\d+(?:[.:/-]\d+)*', corpus))
    if numbers - known:
        raise SkillValidationError('unsupported_number_or_date')
    for url in re.findall(r'https?://[^\s<>"，。；）)]+', text):
        if url not in corpus:
            raise SkillValidationError('unsupported_link')


def validate_output(skill, output, evidence, *, allow_partial=False):
    validate_schema(output, skill.output_schema)
    if skill.id == 'university-source-onboarding' and skill.mode == 'column':
        if [output['candidate_id']] != [row['candidate_id'] for row in evidence['candidates']]:
            raise SkillValidationError('candidate_coverage_mismatch')
        if (output['status'] == 'ready') != bool(output['columns']):
            raise SkillValidationError('column_status_mismatch')
        import soupsieve
        for column in output['columns']:
            for field, value in column.items():
                if field.endswith('_selector') and value:
                    try:
                        soupsieve.compile(value)
                    except Exception:
                        raise SkillValidationError('invalid_css_selector') from None
        return output
    if skill.id == 'summarize-university-notice':
        paragraphs = {item['id']: item['text'] for item in evidence['paragraphs']}
        coverage = output['coverage']['paragraph_ids']
        if len(coverage) != len(set(coverage)) or set(coverage) != set(paragraphs):
            raise SkillValidationError('incomplete_paragraph_coverage')
        if skill.mode != 'extract' and not output['summary'].strip():
            raise SkillValidationError('empty_summary')
        corpus = '\n'.join(paragraphs.values()) + '\n' + evidence['title']
        _tokens_present(output['summary'], corpus)
        for fact in output['facts']:
            _refs(fact['evidence_ids'], paragraphs)
            cited = '\n'.join(paragraphs[x] for x in fact['evidence_ids'])
            if not cited.strip():
                raise SkillValidationError('empty_fact_evidence')
            _tokens_present(fact['text'], cited)
        return output
    candidates = {row['candidate_id']: row for row in evidence['candidates']}
    known = {row['evidence_id']: row for row in evidence['evidence']}
    rows = output['proposals'] if skill.mode == 'extraction' else output['results']
    for row in rows:
        for action in row.get('actions', []):
            if action['type'] == 'read_page':
                if action.get('url') not in evidence.get('observed_urls', []) or action.get('purpose') not in ('directory', 'list', 'article'):
                    raise SkillValidationError('unobserved_or_invalid_operation')
            elif action.get('reference') not in ('identity', 'page-types', 'extraction-patterns'):
                raise SkillValidationError('unknown_skill_reference')
        if row.get('decision') == 'explore' and not row.get('actions'):
            raise SkillValidationError('explore_without_operation')
        if row.get('decision') == 'choose' and not row.get('question'):
            raise SkillValidationError('choice_without_question')
        if row.get('decision') == 'not_column' and not row.get('evidence_ids'):
            raise SkillValidationError('classification_without_evidence')
        proof = row.get('publisher_evidence')
        if proof:
            _refs([proof['directory_evidence_id'], proof['homepage_evidence_id']], known)
    ids = [row['candidate_id'] for row in rows]
    if len(ids) != len(set(ids)) or set(ids) - set(candidates) or (not allow_partial and set(ids) != set(candidates)):
        raise SkillValidationError('candidate_coverage_mismatch: expected=' + canonical(sorted(candidates)) + '; received=' + canonical(ids))
    if skill.mode == 'extraction':
        observed = set(evidence.get('observed_urls', []))
        def urls(value):
            if isinstance(value, str) and value.startswith(('https://', 'http://')):
                observed.add(value)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key.endswith(('url', 'urls')):
                        urls(item)
            elif isinstance(value, list):
                for item in value:
                    urls(item)
        for item in [*evidence['evidence'], *evidence['candidates']]:
            urls(item)
        import soupsieve
        for row in rows:
            _refs(row['evidence_ids'], known)
            config = row['config']
            for name, value in config.items():
                if name.endswith('_url') and value and value not in observed:
                    raise SkillValidationError('unobserved_url')
                if name.endswith('_selector') and value:
                    if len(value) > 500:
                        raise SkillValidationError('selector_too_long')
                    try:
                        soupsieve.compile(value)
                    except Exception:
                        raise SkillValidationError('invalid_css_selector') from None
            if row['decision'] == 'propose' and (not row['evidence_ids'] or not all(config.get(x) for x in ('name','list_url','list_selector'))):
                raise SkillValidationError('missing_source_evidence')
        return output
    if output['school_id'] != evidence['school_id']:
        raise SkillValidationError('school_mismatch')
    entities = {str(row['id']) for row in evidence.get('entities', [])}
    unit_rows = {row['candidate_id']: row for row in rows if row['kind'] == 'unit'}
    allowed_entities = entities | set(unit_rows)
    parents = {}
    for row in rows:
        for refs in row['evidence'].values():
            _refs(refs, known)
        for topic in row['topics']:
            _refs(topic['evidence_ids'], known)
            if not topic['evidence_ids']:
                raise SkillValidationError('topic_without_evidence')
        for field, ekey in [('parent_entity_id', 'parent'), ('publisher_entity_id', 'publisher')]:
            ident = row[field]
            if ident is not None:
                if ident not in allowed_entities or not row['evidence'][ekey]:
                    raise SkillValidationError('unsupported_entity_relationship')
                if row['decision'] == 'propose' and ident in unit_rows and unit_rows[ident]['decision'] != 'propose':
                    raise SkillValidationError('unresolved_parent')
        if row['name'] and not row['evidence']['name']:
            raise SkillValidationError('name_without_evidence')
        if row['kind'] == 'unit' and row['parent_entity_id']:
            parents[row['candidate_id']] = row['parent_entity_id']
        if row['kind'] == 'channel' and row['parent_entity_id'] is not None:
            raise SkillValidationError('channel_parent_instead_of_publisher')
        if row['decision'] == 'propose':
            if not row['name'] or not row['evidence']['identity']:
                raise SkillValidationError('identity_missing')
            if row['kind'] == 'channel' and not row['publisher_entity_id']:
                raise SkillValidationError('publisher_missing')
        if row['decision'] == 'review' and not row['needed_evidence']:
            raise SkillValidationError('review_without_next_step')
        if row['name'] and row['name'].strip().lower() in {'read','更多','了解','查看详情','点击进入'}:
            raise SkillValidationError('action_label_as_name')
    for node in parents:
        seen = set()
        while node in parents:
            if node in seen:
                raise SkillValidationError('relationship_cycle')
            seen.add(node)
            node = parents[node]
    return output
