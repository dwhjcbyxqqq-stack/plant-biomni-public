#!/usr/bin/env python3
"""Read-only bibliographic and coverage audit; never adjudicates biological truth.

Accept a paper list or {records: [...]}; current-paper-index records are supported.
Preserve original source bytes. A DOI group is not an independent experiment.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote, urlsplit


def canonical_doi(value):
    """Normalize only DOI strings and explicit doi.org URLs, not publisher URLs.

    Retain terminal punctuation in bare identifiers: it can be a legal DOI suffix.
    Do not fuzzy-merge DOI identities or assume a successful resolver lookup.
    """
    value = str(value or '').strip()
    if re.match(r'^https?://', value, re.I):
        url = urlsplit(value)
        if (url.hostname or '').casefold() not in {'doi.org', 'dx.doi.org'}:
            return None
        value = unquote(url.path.lstrip('/'))
    else:
        value = re.sub(r'^doi\s*:\s*', '', value, flags=re.I)
    return value.casefold() if re.fullmatch(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]+', value, re.I) else None


def title_key(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ' '.join(re.sub(r'[^\w\s]', ' ', value).split())


def strings(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(x) for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def first(row, *keys):
    return next((row[k] for k in keys if row.get(k) not in (None, '', [])), None)


def audit(payload, expected_source_commit=None):
    records = payload if isinstance(payload, list) else payload.get('records') if isinstance(payload, dict) else None
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError('Input must be a list of paper objects or an object with records array')
    if isinstance(payload, dict) and (payload.get('has_more') is True or payload.get('truncated') is True or payload.get('next_offset') is not None or payload.get('nextOffset') is not None or (isinstance(payload.get('model_projection'), dict) and payload['model_projection'].get('truncated') is True)):
        raise ValueError('A paginated/truncated projection is not a complete corpus audit input')
    groups, titles, exact, counts = defaultdict(list), defaultdict(list), defaultdict(list), Counter()
    coverage = {axis: Counter() for axis in ('species', 'topics', 'publication_type', 'read_scope', 'year')}
    issues = []
    for i, original in enumerate(records):
        row = {**original.get('original', {}), **original} if isinstance(original.get('original'), dict) else original
        rid = str(first(row, 'record_key', 'id', 'source_record_id') or f'index:{i}')
        doi_raw = first(row, 'doi', 'doi_original', 'doi_key')
        doi = canonical_doi(doi_raw)
        if not doi:
            doi = canonical_doi(first(row, 'url', 'source_url'))
        title = first(row, 'title', 'paper_title')
        if doi: groups[doi].append({'record_id': rid, 'index': i, 'title_key': title_key(title)})
        else: counts['records_without_valid_doi'] += 1
        if doi_raw and not canonical_doi(doi_raw):
            issues.append({'code': 'malformed_or_ambiguous_doi', 'record_id': rid, 'value': doi_raw})
        if title: titles[title_key(title)].append({'record_id': rid, 'doi': doi, 'index': i})
        else: counts['records_without_title'] += 1
        source = row.get('original') if isinstance(row.get('original'), dict) else row
        exact[json.dumps(source, sort_keys=True, ensure_ascii=False)].append(rid)
        ptype = str(first(row, 'publication_type', 'paper_type', 'document_type', 'type') or 'unknown').casefold()
        read_scope = str(first(row, 'read_scope', 'readScope', 'source_read_level', 'access_level') or 'unknown')
        coverage['publication_type'][ptype] += 1
        coverage['read_scope'][read_scope] += 1
        coverage['year'][str(first(row, 'year', 'publication_year') or 'unknown')] += 1
        for axis, keys in [('species', ('species',)), ('topics', ('topics', 'tags', 'processes', 'topic'))]:
            values = strings(first(row, *keys))
            if axis == 'species' and not values and isinstance(row.get('context'), dict): values = strings(row['context'].get('species'))
            if not values: coverage[axis]['unknown'] += 1
            for val in set(values): coverage[axis][val] += 1
        if 'review' in ptype and row.get('evidence_role') in {'primary', 'primary_report', 'primary_evidence', 'primary_experimental', 'primary_experimental_evidence', 'independent_replication'}:
            issues.append({'code': 'review_primary_boundary', 'record_id': rid})
        notice = row.get('notice_check') or {}
        if not isinstance(notice, dict):
            issues.append({'code': 'invalid_notice_check', 'record_id': rid})
            notice = {}
        state = notice.get('status', 'not_checked')
        source_doi = canonical_doi(row.get('doi') or row.get('doi_key'))
        notice_target_doi = canonical_doi(notice.get('doi'))
        if source_doi and notice_target_doi and source_doi != notice_target_doi:
            issues.append({'code': 'notice_source_doi_mismatch', 'record_id': rid,
                           'source_doi': source_doi, 'notice_target_doi': notice_target_doi})
        counts['notice_status:' + str(state)] += 1
        if state not in {'unknown', 'not_checked', 'checked_no_notice_found', 'corrected', 'correction_indexed', 'retracted', 'expression_of_concern'}:
            issues.append({'code': 'invalid_notice_status', 'record_id': rid, 'value': state})
        if state not in {'unknown', 'not_checked'} and not (notice.get('checked_at') and notice.get('source_url')):
            issues.append({'code': 'notice_status_without_provenance', 'record_id': rid})
        if state in {'retracted', 'expression_of_concern'}:
            issues.append({'code': 'publication_notice_requires_adjudication', 'record_id': rid, 'notice_status': state})
    for doi, values in groups.items():
        variants = {v['title_key'] for v in values if v['title_key']}
        if len(variants) > 1:
            issues.append({'code': 'doi_title_variants_review', 'doi': doi, 'record_ids': [v['record_id'] for v in values], 'note': 'May be a shortened title, translation, correction or misassigned DOI; do not auto-merge.'})
    for title, values in titles.items():
        dois = {v['doi'] for v in values if v['doi']}
        if len(dois) > 1:
            issues.append({'code': 'title_multiple_dois_review', 'title_key': title, 'doi_keys': sorted(dois), 'record_ids': [v['record_id'] for v in values]})
    actual_commit = payload.get('site_commit') if isinstance(payload, dict) else None
    if expected_source_commit and actual_commit != expected_source_commit:
        issues.append({'code': 'snapshot_commit_mismatch', 'expected': expected_source_commit, 'actual': actual_commit})
    return {
        'schema_version': '1.0.0', 'tool_version': '1.0.0', 'record_count': len(records),
        'doi_group_count': len(groups), 'counts': dict(counts),
        'coverage': {k: dict(v) for k,v in coverage.items()},
        'repeated_doi_groups': [{'doi': k, 'record_ids': [v['record_id'] for v in rows]} for k,rows in groups.items() if len(rows)>1],
        'exact_record_duplicate_groups': [v for v in exact.values() if len(v)>1],
        'issues': issues, 'issue_count': len(issues), 'source_commit': actual_commit,
        'scientific_truth_verified': False, 'network_verification_performed': False,
        'limitations': ['Counts describe supplied records and explicit metadata only.',
                       'Same-DOI contexts remain separate; no record is deleted or merged.',
                       'Missing coverage is a catalogue/metadata gap, not evidence of biological absence.',
                       'Notice fields are user-supplied assertions; the script does not check publishers.',
                       'Zero flags does not verify citations, completeness, full-text access or scientific claims.']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--expected-source-commit')
    args = p.parse_args()
    try:
        raw = args.input.read_bytes()
        result = audit(json.loads(raw), args.expected_source_commit)
        result['input_sha256'] = hashlib.sha256(raw).hexdigest()
        result['audited_at'] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            if args.output.resolve() == args.input.resolve(): raise ValueError('Output cannot overwrite source input')
            with args.output.open('x', encoding='utf-8') as stream: stream.write(encoded)
            print(json.dumps({'output': str(args.output), 'record_count': result['record_count'], 'doi_group_count': result['doi_group_count'], 'issue_count': result['issue_count']}))
        else: print(encoded, end='')
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)})); return 1


if __name__ == '__main__':
    sys.exit(main())
