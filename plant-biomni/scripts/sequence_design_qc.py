#!/usr/bin/env python3
"""Deterministic sequence/annotation QC; no activity prediction or guide design.

Input schema 1.0.0 uses exact uppercase ACGT sequences and 0-based-half-open
coordinates on each candidate's supplied sequence. Negative-strand module and
scored-region sequences are reverse complemented after slicing. Expected module
sequence therefore follows the declared strand, not always the supplied strand.

Missing biological provenance or model-method metadata yields audit warnings.
Malformed sequence/coordinates and contradictory declarations fail validation.
Outputs preserve the input; numeric metrics are sequence statistics only.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sys

VERSION = '1.0.0'
COORDINATES = '0-based-half-open'
MODEL_METHOD_FIELDS = ('checkpoint', 'tokenization', 'context', 'masking', 'score_definition')


class DesignError(ValueError):
    """Input is structurally invalid or contradicts its exact sequence."""


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def sha256(sequence):
    return hashlib.sha256(sequence.encode('ascii')).hexdigest()


def reverse_complement(sequence):
    return sequence.translate(str.maketrans('ACGT', 'TGCA'))[::-1]


def sequence_value(value, location):
    if not isinstance(value, str) or not re.fullmatch('[ACGT]+', value):
        raise DesignError(f'{location}: require a nonempty exact uppercase ACGT sequence')
    return value


def object_list(value, location):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise DesignError(f'{location}: require an array of objects')
    return value


def region(region, sequence, location):
    if not isinstance(region, dict):
        raise DesignError(f'{location}: require start, end and strand')
    start, end, strand = (region.get(k) for k in ('start', 'end', 'strand'))
    if (type(start) is not int or type(end) is not int or
            not 0 <= start < end <= len(sequence)):
        raise DesignError(f'{location}: require integer 0 <= start < end <= sequence length')
    if strand not in ('+', '-'):
        raise DesignError(f'{location}: strand must be + or -')
    extracted = sequence[start:end]
    if strand == '-':
        extracted = reverse_complement(extracted)
    return {'start': start, 'end': end, 'strand': strand,
            'length': end - start, 'sequence': extracted, 'sha256': sha256(extracted)}


def repeated_kmers(sequence, k):
    """Overlapping forward-strand windows; no motif/function interpretation."""
    counts = Counter(sequence[i:i + k] for i in range(max(0, len(sequence) - k + 1)))
    total = sum(counts.values())
    repeated = sum(count for count in counts.values() if count > 1)
    return {'k': k, 'total_windows': total, 'distinct_kmers': len(counts),
            'repeated_kmer_types': sum(count > 1 for count in counts.values()),
            'windows_in_repeated_kmers': repeated,
            'fraction_windows_in_repeated_kmers': repeated / total if total else None,
            'definition': 'Forward-strand overlapping k-mer windows whose exact k-mer occurs at least twice / all windows; null when sequence is shorter than k. No motif or biological activity claim.'}


def analyze_design(manifest, k=12):
    """Validate all candidates before returning any analysis; never mutate input."""
    if not isinstance(manifest, dict) or manifest.get('schema_version') != '1.0.0':
        raise DesignError('schema_version must be 1.0.0')
    if manifest.get('coordinate_convention') != COORDINATES:
        raise DesignError('coordinate_convention must be 0-based-half-open')
    if type(k) is not int or not 1 <= k <= 31:
        raise DesignError('k must be an integer from 1 through 31')
    candidates = object_list(manifest.get('candidates'), 'candidates')
    if not candidates:
        raise DesignError('candidates must not be empty')
    ids, sequences, outputs, warnings = set(), {}, [], []
    score_groups = defaultdict(list)

    def warn(code, candidate_id, detail):
        warnings.append({'code': code, 'candidate_id': candidate_id, 'detail': detail})

    for index, item in enumerate(candidates):
        cid = item.get('id')
        if not nonempty(cid) or cid != cid.strip() or cid in ids:
            raise DesignError(f'candidates[{index}].id: require unique nonempty ID without edge whitespace')
        ids.add(cid)
        seq = sequence_value(item.get('sequence'), f'{cid}.sequence')
        sequences[cid] = seq
        if not nonempty(item.get('purpose')):
            warn('missing_purpose', cid, 'Candidate purpose is unresolved.')
        sources = object_list(item.get('sources', []), f'{cid}.sources')
        if not sources:
            warn('missing_source_identity', cid, 'No sequence source identity/version declared.')
        for n, source in enumerate(sources):
            if not nonempty(source.get('id')) or not nonempty(source.get('version')):
                warn('incomplete_source_identity', cid, f'sources[{n}] requires id and version before source-grounded reuse; synthetic inputs may use an explicit synthetic source/version.')
        modules, module_ids = [], set()
        for n, module in enumerate(object_list(item.get('modules', []), f'{cid}.modules')):
            mid = module.get('id')
            if not nonempty(mid) or mid != mid.strip() or mid in module_ids:
                raise DesignError(f'{cid}.modules[{n}].id: require unique nonempty module ID')
            module_ids.add(mid)
            extracted = region(module, seq, f'{cid}.modules.{mid}')
            if 'allow_overlap' in module and type(module['allow_overlap']) is not bool:
                raise DesignError(f'{cid}.modules.{mid}.allow_overlap must be a JSON boolean')
            if 'expected_sequence' in module:
                expected = sequence_value(module['expected_sequence'], f'{cid}.modules.{mid}.expected_sequence')
                if expected != extracted['sequence']:
                    raise DesignError(f'{cid}.modules.{mid}: expected_sequence differs from strand-aware sequence slice')
            else:
                warn('module_identity_unverified', cid, f'Module {mid} has no expected_sequence; coordinates were checked but module identity was not.')
            modules.append({'id': mid, **extracted, 'expected_sequence_matches': True if 'expected_sequence' in module else None,
                            'allow_overlap': module.get('allow_overlap', False)})
        for i, left in enumerate(modules):
            for right in modules[i + 1:]:
                if max(left['start'], right['start']) < min(left['end'], right['end']):
                    if not (left['allow_overlap'] and right['allow_overlap']):
                        raise DesignError(f'{cid}: overlapping modules {left["id"]}/{right["id"]} require allow_overlap:true on both')
                    warn('declared_module_overlap', cid, f'{left["id"]}/{right["id"]} overlap deliberately; biological interpretation is unverified.')
        scores = []
        for n, score in enumerate(object_list(item.get('model_scores', []), f'{cid}.model_scores')):
            loc = f'{cid}.model_scores[{n}]'
            value = score.get('value')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise DesignError(f'{loc}.value must be a finite number')
            if score.get('claim_type', 'sequence_model_score') != 'sequence_model_score':
                raise DesignError(f'{loc}: only claim_type sequence_model_score is supported; activity and specificity require biological measurements')
            missing = [field for field in MODEL_METHOD_FIELDS if not nonempty(score.get(field))]
            if missing:
                warn('incomplete_model_method', cid, f'{loc} missing: {", ".join(missing)}')
            if score.get('input_sequence_sha256') is not None and score['input_sequence_sha256'] != sha256(seq):
                raise DesignError(f'{loc}: input_sequence_sha256 does not match candidate')
            if score.get('input_sequence_sha256') is None:
                warn('score_input_hash_unbound', cid, f'{loc} has no input_sequence_sha256; score origin is not byte-bound.')
            scored_region = None
            if score.get('scored_region') is not None:
                scored_region = region(score['scored_region'], seq, loc + '.scored_region')
            else:
                missing.append('scored_region')
                warn('missing_scored_region', cid, f'{loc} has no explicit scored region.')
            group = score.get('comparison_group')
            if group is not None and not nonempty(group):
                raise DesignError(f'{loc}.comparison_group must be a nonempty string when supplied')
            signature = None
            if not missing:
                signature = {field: score[field] for field in MODEL_METHOD_FIELDS}
                signature.update({'input_length': len(seq), 'scored_length': scored_region['length'], 'scored_strand': scored_region['strand']})
            summary = {'score_index': n, 'value': value, 'claim_type': 'sequence_model_score',
                       'scored_region': scored_region, 'comparison_group': group,
                       'method_metadata_complete': not missing,
                       'declared_comparison_signature': signature,
                       'score_execution_verified': False}
            scores.append(summary)
            if group is not None:
                score_groups[group].append({'candidate_id': cid, 'score_index': n, 'signature': signature})
        outputs.append({'id': cid, 'length': len(seq), 'gc_fraction': (seq.count('G') + seq.count('C')) / len(seq),
                        'sequence_sha256': sha256(seq), 'reverse_complement_sha256': sha256(reverse_complement(seq)),
                        'self_reverse_complement': seq == reverse_complement(seq),
                        'repeated_kmers': repeated_kmers(seq, k), 'modules': modules, 'model_scores': scores})

    exact, orientations = defaultdict(list), defaultdict(list)
    for cid, seq in sequences.items():
        exact[seq].append(cid)
        orientations[min(seq, reverse_complement(seq))].append(cid)
    comparison_groups = []
    for group, members in score_groups.items():
        signatures = {json.dumps(x['signature'], sort_keys=True) for x in members if x['signature'] is not None}
        incomplete = any(x['signature'] is None for x in members)
        status = 'incomplete_metadata' if incomplete else 'declared_metadata_differ' if len(signatures) > 1 else 'declared_metadata_match'
        if len(members) < 2:
            status = 'single_score_no_comparison'
        comparison_groups.append({'id': group, 'members': members, 'status': status,
                                  'scientific_comparability_verified': False})
        if status in ('incomplete_metadata', 'declared_metadata_differ'):
            warn('model_comparison_requires_review', None, f'Comparison group {group}: {status}; scores are not ranked or pooled.')
    return {'schema_version': '1.0.0', 'helper_version': VERSION,
            'status': 'audit_warnings' if warnings else 'sequence_checks_passed',
            'coordinate_convention': COORDINATES, 'input': deepcopy(manifest),
            'candidates': outputs,
            'exact_duplicate_groups': [ids for ids in exact.values() if len(ids) > 1],
            'reverse_complement_groups': [ids for ids in orientations.values() if len({sequences[x] for x in ids}) > 1],
            'model_comparison_groups': comparison_groups, 'warnings': warnings,
            'scientific_truth_verified': False, 'promoter_activity_predicted': False,
            'guide_offtargets_checked': False,
            'limitations': ['Sequence statistics and declared metadata checks only; no promoter activity, cell specificity, stress induction, guide efficacy or off-target claim.',
                            'Hashes establish supplied byte identity; source authenticity, score execution and model comparability require separate review.',
                            'Repeated overlapping k-mers describe exact sequence redundancy; they do not quantify transcriptional activity or model leakage.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--k', type=int, default=12, help='Overlapping forward-strand k-mer length (1–31; default 12)')
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise DesignError('Output exists; refusing overwrite')
        raw = args.input.read_bytes()
        result = analyze_design(json.loads(raw), args.k)
        result['input_file_sha256'] = hashlib.sha256(raw).hexdigest()
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(payload)
        print(json.dumps({'ok': True, 'output': str(args.output), 'candidate_count': len(result['candidates']),
                          'warning_count': len(result['warnings']), 'scientific_truth_verified': False}))
        return 0
    except (DesignError, OSError, ValueError, TypeError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
