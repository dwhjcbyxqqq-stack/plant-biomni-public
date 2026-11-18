#!/usr/bin/env python3
"""Create and check source-pinned planning packets; never execute or certify science."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sys
import uuid

from literature_audit import canonical_doi

ROOT = Path(__file__).resolve().parent.parent
VERSION = '1.0.0'
FIELDS = ('objective_and_estimand', 'source_records', 'context', 'workflow_ids_versions',
          'strongest_alternative', 'controls_and_comparisons', 'required_outputs',
          'continue_revise_stop', 'execution_and_writeback')
CONTEXT = ('species', 'genotype', 'tissue', 'stage', 'leaf_rank_surface',
           'stimulus_route_dose_time', 'experimental_unit')
BLOCKED = {'retracted', 'expression_of_concern'}
UNKNOWN = {'', 'unknown', 'unresolved', 'not_checked', 'not supplied', 'pending', 'tbd'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def object_hash(obj):
    return digest(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def read(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    return json.loads(raw), {'path': str(path), 'sha256': digest(raw)}


def present(value):
    if isinstance(value, str):
        return value.strip().casefold() not in UNKNOWN
    if isinstance(value, list):
        return bool(value) and all(present(v) for v in value)
    if isinstance(value, dict):
        # A reasoned not-applicable value is explicit, not an empty placeholder.
        if value.get('status') == 'not_applicable':
            return present(value.get('reason'))
        return bool(value) and all(present(v) for v in value.values())
    return value is not None and value is not False


def select(index, selectors):
    rows = index.get('records')
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('Index must contain complete record objects')
    keys = [r.get('record_key') for r in rows]
    if any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
        raise ValueError('Index record_key values must be nonempty and unique')
    if not isinstance(selectors, list) or not selectors:
        raise ValueError('Select at least one exact record or DOI')
    selected = set()
    for selector in selectors:
        if not isinstance(selector, dict) or set(selector) != {'kind', 'value'}:
            raise ValueError('Each selector requires exactly kind and value')
        kind, val = selector['kind'], selector['value']
        if not isinstance(val, str) or not val.strip():
            raise ValueError('Selector value must be nonempty')
        if kind == 'record':
            matches = [r for r in rows if val in (r['record_key'], r.get('source_record_id'))]
            if len(matches) > 1:
                raise ValueError('Ambiguous source_record_id; use an exact record_key: ' + val)
        elif kind == 'doi':
            doi = canonical_doi(val)
            if doi is None:
                raise ValueError('Invalid DOI selector: ' + val)
            matches = [r for r in rows if canonical_doi(r.get('doi_key')) == doi]
        else:
            raise ValueError('Unknown selector kind')
        if not matches:
            raise ValueError('Source selector has no exact match: ' + val)
        selected.update(r['record_key'] for r in matches)
    return [r for r in rows if r['record_key'] in selected]


def publication_role(row):
    original = row.get('original') or {}
    fields = ('publication_type', 'publicationType', 'paper_type', 'paperType',
              'document_type', 'documentType', 'type', 'kind', 'evidence_role', 'evidenceRole')
    declarations = [{'location': location, 'field': field, 'value': obj[field]}
                    for location, obj in (('record', row), ('original', original))
                    for field in fields if obj.get(field) not in (None, '')]
    normalized = {re.sub(r'[\s-]+', '_', str(d['value']).strip().casefold()) for d in declarations}
    review_values = {'review', 'review_article', 'review_synthesis', 'historical_review',
                     'systematic_review', 'meta_analysis', 'mini_review', 'minireview'}
    primary_values = {'primary', 'primary_research', 'research', 'research_article',
                      'original_research', 'original_article', 'primary_report',
                      'primary_evidence', 'primary_experimental', 'primary_experimental_evidence'}
    is_review, is_primary = bool(normalized & review_values), bool(normalized & primary_values)
    # Keep review scope when declarations conflict; a generic article type is not primary evidence.
    role = 'review_synthesis' if is_review else 'primary_report_declared' if is_primary else 'unknown'
    return {'role': role, 'conflict': is_review and is_primary, 'declarations': declarations}


def source_view(row, notices):
    original = row.get('original') or {}
    doi = canonical_doi(row.get('doi_key'))
    matched = [n for n in notices.get('notices', []) if isinstance(n, dict) and
               ((doi is not None and canonical_doi(n.get('doi')) == doi) or
                (row.get('source_record_id') and row['source_record_id'] in n.get('affected_record_ids', [])))]
    inherited = [original.get('notice_check'), row.get('notice_check')]
    checks = matched + [n for n in inherited if isinstance(n, dict) and n not in matched]
    checks = [n for i, n in enumerate(checks) if n not in checks[:i]]
    statuses = {str(n.get('status', 'unknown')).casefold() for n in checks}
    statuses.update(x for x in row.get('layers', []) if x in BLOCKED)
    if 'retract' in str(row.get('publication_status', '')).casefold():
        statuses.add('retracted')
    role_details = publication_role(row)
    role = role_details['role']
    issues = []
    if statuses & BLOCKED:
        issues += sorted('source_' + s for s in statuses & BLOCKED)
    if any('correct' in s for s in statuses):
        issues.append('corrected_material_requires_review')
    if not statuses or statuses & {'unknown', 'not_checked'}:
        issues.append('notice_status_unresolved')
    if role == 'unknown':
        issues.append('publication_role_unresolved')
    if role == 'review_synthesis':
        issues.append('review_is_not_primary_experimental_support')
    if role_details['conflict']:
        issues.append('conflicting_publication_role_declarations')
    scope = str(row.get('read_scope') or 'unknown')
    normalized_scope = scope.casefold().replace('_', ' ')
    positive_scope = re.search(r'\b(?:full text|primary methods) (?:read|checked)\b', normalized_scope)
    negated_scope = re.search(r'\b(?:not|no) (?:all )?(?:full text|primary methods)|\b(?:full text|primary methods) (?:not|unread)', normalized_scope)
    if not positive_scope or negated_scope:
        issues.append('primary_methods_read_scope_unresolved')
    if not row.get('locator'):
        issues.append('source_locator_missing')
    if str(row.get('publication_status', 'unknown')).casefold() in UNKNOWN:
        issues.append('publication_state_unresolved')
    if set(row.get('layers', [])) & {'candidate', 'private', 'preprint', 'legacy'}:
        issues.append('restricted_or_candidate_layer_requires_review')
    gate = 'blocked' if statuses & BLOCKED else 'review_required' if issues else 'source_metadata_clear'
    return {
        'record_key': row['record_key'], 'source_record_id': row.get('source_record_id'),
        'record_sha256': object_hash(row), 'doi': doi, 'title': row.get('title'),
        'url': row.get('url'), 'locator': row.get('locator'), 'inherited_read_scope': scope,
        'passages_read_this_run': [], 'publication_role': role,
        'publication_role_declarations': role_details['declarations'],
        'publication_role_conflict': role_details['conflict'],
        'publication_status': row.get('publication_status'), 'layers': row.get('layers', []),
        'context': original.get('context'), 'parent_context': row.get('parent_context', {}),
        'species': row.get('species', []), 'source_version_id': original.get('source_version_id'),
        'dataset_dependencies': original.get('dependency_dataset', original.get('dataset')),
        'study_id': original.get('study_id'), 'provenance': row.get('provenance', []),
        'limitations': row.get('limitations'), 'notices': checks,
        'source_gate': gate, 'review_issues': issues,
    }


def derive(index, notices, registry, selectors, workflow_ids):
    sources = [source_view(r, notices) for r in select(index, selectors)]
    if not isinstance(workflow_ids, list) or not workflow_ids or any(not isinstance(x, str) for x in workflow_ids):
        raise ValueError('Select at least one workflow ID')
    workflows = registry.get('workflows', [])
    if not isinstance(workflows, list) or any(not isinstance(w, dict) for w in workflows):
        raise ValueError('Invalid registry workflow array')
    ids = [w.get('id') for w in workflows]
    if len(ids) != len(set(ids)):
        raise ValueError('Registry has duplicate workflow IDs')
    selected = []
    for wid in dict.fromkeys(workflow_ids):
        match = next((w for w in workflows if w.get('id') == wid), None)
        if match is None:
            raise ValueError('Unknown workflow: ' + wid)
        selected.append({k: match.get(k) for k in ('id', 'version', 'status', 'canonical_status', 'execution_kind', 'unresolved')})
    groups = defaultdict(list)
    for s in sources:
        groups[s['doi'] or 'no_doi:' + s['record_key']].append(s['record_key'])
    reuse = 'blocked' if any(s['source_gate'] == 'blocked' for s in sources) else 'review_required' if any(s['source_gate'] == 'review_required' for s in sources) else 'source_metadata_clear'
    return sources, selected, {
        'source_reuse': reuse,
        'doi_groups': [{'doi_or_missing_record_key': k, 'record_keys': v} for k, v in groups.items()],
        'independent_experiment_count': None,
        'independence_note': 'DOI groups and separate records are not independent experiments. Shared datasets may span papers; independence requires source review.',
    }


def create_packet(index_path, notices_path, registry_path, selectors, workflow_ids, task):
    loaded = [read(p) for p in (index_path, notices_path, registry_path)]
    index, notices, registry = [x[0] for x in loaded]
    sources, workflows, gates = derive(index, notices, registry, selectors, workflow_ids)
    if not present(task):
        raise ValueError('Task must be nonempty')
    return {
        'schema_version': VERSION, 'packet_id': 'PB-DECISION-' + str(uuid.uuid4()),
        'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'planned',
        'pins': {**{name: pin for name, (_, pin) in zip(('index', 'notices', 'registry'), loaded)},
                 'site_commit': index.get('site_commit'), 'registry_version': registry.get('registry_version'),
                 'notice_version': notices.get('version')},
        'selection': selectors, 'workflow_selection': list(dict.fromkeys(workflow_ids)),
        'decision': {
            'objective_and_estimand': {'objective': task, 'outcome': None, 'units': None, 'denominator': None, 'interval': None},
            'source_records': sources,
            'context': {k: None for k in CONTEXT},
            'workflow_ids_versions': workflows,
            'strongest_alternative': None, 'controls_and_comparisons': [], 'required_outputs': [],
            'continue_revise_stop': {'continue': None, 'revise': None, 'stop': None},
            'execution_and_writeback': {'execution': 'not_started', 'run_id': None, 'writeback': 'not_requested'},
        },
        'source_reviews': [], 'workflow_resolutions': [], 'gates': gates, 'scientific_truth_verified': False,
        'limitations': ['A planning packet is not a run, laboratory SOP, source authenticity check or completed writeback.',
                        'Source metadata is inherited; no new primary passage is read by this helper.',
                        'Readiness checks field completeness and traceable review artifacts, not scientific adequacy or authorization.'],
    }


def save_packet(packet, output):
    """Save new packet with byte-identical, content-addressed sibling snapshots."""
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output exists; refusing to overwrite packet')
    saved = json.loads(json.dumps(packet))
    snapshot_dir = output.parent / '.pb-snapshots'
    if snapshot_dir.is_symlink():
        raise ValueError('Snapshot directory must not be a symlink')
    snapshots = []
    for name in ('index', 'notices', 'registry'):
        pin = saved['pins'][name]
        raw = Path(pin['path']).read_bytes()
        sha = digest(raw)
        if sha != pin['sha256']:
            raise ValueError(name + ' changed before snapshot preservation')
        target = snapshot_dir / (sha + '.json')
        snapshots.append((target, raw))
        pin['origin_path'] = pin['path']
        pin['path'] = str(target.relative_to(output.parent))
    snapshot_dir.mkdir(exist_ok=True)
    for target, raw in snapshots:
        if target.is_symlink():
            raise ValueError('Snapshot must not be a symlink')
        try:
            with target.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise ValueError('Existing content-addressed snapshot has different bytes')
    with output.open('x', encoding='utf-8') as stream:
        json.dump(saved, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return saved


def artifact_error(artifact, packet_path, prefix):
    if not isinstance(artifact, dict) or not artifact.get('path'):
        return prefix + '_missing_artifact'
    path = Path(artifact['path'])
    if not path.is_absolute():
        path = Path(packet_path).resolve().parent / path
    if digest(path.read_bytes()) != artifact.get('sha256'):
        return prefix + '_artifact_hash_mismatch'
    return None


def validate_packet(packet, packet_path, overrides=None):
    errors, warnings = [], []
    overrides = overrides or {}
    if not isinstance(packet, dict):
        return {'valid': False, 'errors': ['Packet must be an object'], 'warnings': [], 'scientific_truth_verified': False}
    try:
        if packet.get('schema_version') != VERSION:
            errors.append('unsupported_schema_version')
        if packet.get('status') not in {'planned', 'ready_for_execution'}:
            errors.append('packet_cannot_assert_execution_or_completion')
        if packet.get('scientific_truth_verified') is not False:
            errors.append('scientific_truth_cannot_be_verified_by_packet')
        loaded = []
        for name in ('index', 'notices', 'registry'):
            pin = packet['pins'][name]
            path = Path(overrides.get(name) or pin['path'])
            if not path.is_absolute():
                path = Path(packet_path).resolve().parent / path
            obj, actual = read(path)
            if actual['sha256'] != pin.get('sha256'):
                errors.append(name + '_sha256_mismatch')
            loaded.append(obj)
        index, notices, registry = loaded
        for name, actual in [('site_commit', index.get('site_commit')), ('registry_version', registry.get('registry_version')), ('notice_version', notices.get('version'))]:
            if packet['pins'].get(name) != actual:
                errors.append(name + '_mismatch')
        sources, workflows, gates = derive(index, notices, registry, packet['selection'], packet['workflow_selection'])
        decision = packet['decision']
        if set(decision) != set(FIELDS):
            errors.append('decision_must_contain_exactly_nine_fields')
        # Newly read passages live in traceable source_reviews, never by changing inherited data.
        if decision.get('source_records') != sources:
            errors.append('source_records_differ_from_selected_snapshot')
        if decision.get('workflow_ids_versions') != workflows:
            errors.append('workflow_versions_or_status_differ_from_registry')
        if packet.get('gates') != gates:
            errors.append('source_gates_or_grouping_modified')
        state = decision.get('execution_and_writeback', {})
        if state.get('execution') != 'not_started' or state.get('writeback') not in {'not_requested', 'pending'}:
            errors.append('planning_packet_cannot_claim_execution_or_writeback')
        reviews = packet.get('source_reviews', [])
        if not isinstance(reviews, list):
            raise ValueError('source_reviews must be an array')
        review_map = {}
        source_keys = {s['record_key'] for s in sources}
        for review in reviews:
            if not isinstance(review, dict) or review.get('record_key') not in source_keys:
                errors.append('source_review_has_unknown_record'); continue
            key = review['record_key']
            if key in review_map:
                errors.append('duplicate_source_review')
            review_map[key] = review
            error = artifact_error(review.get('artifact'), packet_path, 'source_review')
            if error:
                errors.append(error)
        resolutions = packet.get('workflow_resolutions', [])
        if not isinstance(resolutions, list):
            raise ValueError('workflow_resolutions must be an array')
        expected_resolutions = {
            (w['id'], w['version'], object_hash(requirement)): requirement
            for w in workflows for requirement in (w.get('unresolved') or [])
        }
        resolved = set()
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                errors.append('invalid_workflow_resolution'); continue
            key = (resolution.get('workflow_id'), resolution.get('workflow_version'),
                   object_hash(resolution.get('unresolved')))
            if key not in expected_resolutions:
                errors.append('workflow_resolution_not_in_pinned_workflow'); continue
            if key in resolved:
                errors.append('duplicate_workflow_resolution')
            if not present(resolution.get('resolution')):
                errors.append('workflow_resolution_missing_concrete_value')
            else:
                resolved.add(key)
            error = artifact_error(resolution.get('artifact'), packet_path, 'workflow_resolution')
            if error:
                errors.append(error)
        if gates['source_reuse'] == 'blocked':
            warnings.append('selected_retracted_or_concern_source_is_blocked_for_scientific_reuse')
        elif gates['source_reuse'] == 'review_required':
            warnings.append('source_reuse_requires_explicit_review; metadata is not scientific approval')
        if packet.get('status') == 'ready_for_execution':
            if gates['source_reuse'] == 'blocked':
                errors.append('blocked_source_cannot_be_declared_ready')
            for field in ('objective_and_estimand', 'context', 'strongest_alternative', 'controls_and_comparisons', 'required_outputs', 'continue_revise_stop'):
                if not present(decision.get(field)):
                    errors.append('ready_packet_missing_' + field)
            if set(decision.get('context', {})) != set(CONTEXT):
                errors.append('ready_packet_context_fields_missing')
            if set(decision.get('objective_and_estimand', {})) != {'objective', 'outcome', 'units', 'denominator', 'interval'}:
                errors.append('ready_packet_estimand_fields_missing')
            if set(decision.get('continue_revise_stop', {})) != {'continue', 'revise', 'stop'}:
                errors.append('ready_packet_decision_gates_missing')
            for source in sources:
                review = review_map.get(source['record_key'], {})
                if not all(present(review.get(k)) for k in ('reviewer', 'reviewed_at', 'passage_locators', 'fit_to_task')):
                    errors.append('ready_packet_missing_source_review:' + source['record_key'])
                addressed = review.get('addressed_issues', {})
                if not isinstance(addressed, dict) or any(not present(addressed.get(issue)) for issue in source['review_issues']):
                    errors.append('ready_packet_unresolved_source_issues:' + source['record_key'])
            if any(w['status'] in {'planned', 'retired'} or w['canonical_status'] == 'superseded' for w in workflows):
                errors.append('workflow_not_execution_ready_or_has_unresolved_parameters')
            for wid, version, requirement_hash in sorted(set(expected_resolutions) - resolved):
                errors.append('workflow_unresolved_parameter:' + wid + '@' + version + ':' + requirement_hash)
        return {'valid': not errors, 'status': packet.get('status'), 'source_reuse': gates['source_reuse'],
                'errors': errors, 'warnings': warnings, 'scientific_truth_verified': False,
                'execution_performed': False, 'writeback_performed': False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(str(exc))
        return {'valid': False, 'errors': errors, 'warnings': warnings, 'scientific_truth_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    new = sub.add_parser('new')
    new.add_argument('--record', action='append', default=[])
    new.add_argument('--doi', action='append', default=[])
    new.add_argument('--workflow', action='append', required=True)
    new.add_argument('--task', required=True)
    new.add_argument('--output', type=Path, required=True)
    check = sub.add_parser('validate')
    check.add_argument('packet', type=Path)
    for command in (new, check):
        for name, filename in [('index', 'current-paper-index.json'), ('notices', 'source-notices.json'), ('registry', 'workflow-registry.json')]:
            command.add_argument('--' + name, type=Path, required=(command is new))
    args = parser.parse_args()
    try:
        if args.command == 'new':
            selectors = [{'kind': kind, 'value': v} for kind, vals in [('record', args.record), ('doi', args.doi)] for v in vals]
            packet = create_packet(args.index, args.notices, args.registry, selectors, args.workflow, args.task)
            packet = save_packet(packet, args.output)
            result = {'created': str(args.output.resolve()), 'status': packet['status'], 'source_reuse': packet['gates']['source_reuse'], 'selected_records': len(packet['decision']['source_records']), 'scientific_truth_verified': False}
            code = 0
        else:
            packet, _ = read(args.packet)
            result = validate_packet(packet, args.packet, {k: getattr(args, k) for k in ('index', 'notices', 'registry') if getattr(args, k)})
            code = 0 if result['valid'] else 1
        print(json.dumps(result, ensure_ascii=False, indent=2)); return code
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'valid': False, 'error': str(exc), 'scientific_truth_verified': False})); return 1


if __name__ == '__main__':
    sys.exit(main())
