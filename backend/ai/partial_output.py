"""Read only complete JSON array objects, including from a truncated stream."""
import io
import ijson


def validated_partial(skill, content, evidence):
    from backend.ai.skill_loader import validate_output
    key = 'proposals' if skill.mode == 'extraction' else 'results'
    rows, duplicates = {}, set()
    try:
        for row in ijson.items(io.StringIO(content), key + '.item'):
            if not isinstance(row, dict):
                continue
            ident = row.get('candidate_id')
            if ident in rows:
                duplicates.add(ident)
            rows[ident] = row
    except (ValueError, ijson.JSONError):
        pass
    for ident in duplicates:
        rows.pop(ident, None)
    # A contradictory school identity invalidates every partial classification.
    if key == 'results':
        try:
            school = next(ijson.items(io.StringIO(content), 'school_id'), evidence['school_id'])
            if school != evidence['school_id']:
                return None
        except (ValueError, ijson.JSONError):
            # Candidate IDs and cited material are bound to this request. The
            # envelope can arrive later; a contradictory identity retracts it.
            pass
    combined = {key: list(rows.values())}
    if key == 'results':
        combined.update(school_id=evidence['school_id'], coverage_gaps=[])
    try:
        validate_output(skill, combined, evidence, allow_partial=True)
        if rows:
            return combined
    except (ValueError, TypeError, KeyError):
        pass
    accepted = []
    for row in rows.values():
        output = {key: [row]}
        if key == 'results':
            output.update(school_id=evidence['school_id'], coverage_gaps=[])
        try:
            validate_output(skill, output, evidence, allow_partial=True)
        except (ValueError, TypeError, KeyError):
            continue
        accepted.append(row)
    if not accepted:
        return None
    output = {key: accepted}
    if key == 'results':
        output.update(school_id=evidence['school_id'], coverage_gaps=[])
    return output
