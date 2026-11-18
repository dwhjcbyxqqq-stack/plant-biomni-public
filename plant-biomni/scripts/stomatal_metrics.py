#!/usr/bin/env python3
"""Calculate gated SI and stomatal density from adjudicated tabular counts.

No image segmentation or identity validation occurs here. Positive review gates
are assertions supplied by the caller. See references/measurement-contracts.md.
"""
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import sys

VERSION = '1.0.0'
HIERARCHY = ('sample_id', 'plant_id', 'leaf_id', 'field_id')
COUNTS = ('stomata_pairs', 'nonstomatal_cells')
STOMATAL_GATES = ('identity_resolved', 'units_resolved', 'border_resolved', 'same_roi')
REQUIRED_COLUMNS = HIERARCHY + COUNTS + ('denominator_complete',) + STOMATAL_GATES
SI = 'si_percent'
DENSITY = 'density_per_mm2'


class ContractError(ValueError):
    """An absent or incompatible counting declaration cannot be calculated."""


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_contract(contract):
    if not isinstance(contract, dict):
        raise ContractError('An explicit JSON counting contract is required')
    expected = {
        'formula': 'SI=100*S/(S+E)',
        'stomatal_unit': 'one_guard_cell_pair',
        'nonstomatal_unit': 'all_nonstomatal_epidermal_cells_including_precursors',
    }
    errors = [f'{key} must be {value!r}' for key, value in expected.items()
              if contract.get(key) != value]
    for key in ('contract_id', 'boundary_convention'):
        if not _nonempty(contract.get(key)):
            errors.append(f'{key} must be a nonempty string')
    if contract.get('common_roi') is not True:
        errors.append('common_roi must be JSON true')
    if 'area_unit' in contract and contract['area_unit'] != 'um2':
        errors.append('area_unit must be um2; pixel areas are not physical areas')
    if errors:
        raise ContractError('; '.join(errors))


def _positive_gate(value):
    # Numeric 1, strings "1"/"yes", and nonempty unknown text are not approval.
    return value is True or (isinstance(value, str) and value.strip().lower() == 'true')


def _number(value, integer=False):
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        parsed = Decimal(str(value).strip())
        if not parsed.is_finite() or parsed < 0:
            return None
        if integer:
            if parsed != parsed.to_integral_value():
                return None
            return int(parsed)
        if parsed == 0:
            return None
        result = float(parsed)
        return result if math.isfinite(result) and result > 0 else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _metric(reasons, numerator=None, denominator=None, factor=1):
    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        try:
            with localcontext() as context:
                context.prec = 30
                value = float(Decimal(numerator) * factor / Decimal(str(denominator)))
            if not math.isfinite(value):
                reasons.append('numeric_result_out_of_range')
        except (ArithmeticError, ValueError, TypeError):
            reasons.append('numeric_result_out_of_range')
    return {'value': None if reasons else value,
            'status': 'blocked' if reasons else 'ready', 'reasons': reasons}


def _json_safe(value):
    """Retain nonfinite raw values as text, keeping strict JSON serializable."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _identifier(value):
    return value.strip() if _nonempty(value) else None


def analyze_counts(rows, contract):
    """Return all field rows and conservative plant pools, including blocked rows.

    The contract is mandatory. Unknown QC is false for release purposes. An
    unassignable row blocks every compatible plant group rather than disappearing.
    """
    validate_contract(contract)
    rows = list(rows)
    if not rows:
        raise ValueError('Input contains no count rows')
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('Each count row must be a mapping')
    strata = [name for name in ('cohort', 'condition') if any(name in row for row in rows)]
    grouping = ['sample_id', *strata, 'plant_id']
    field_key_names = grouping + ['leaf_id', 'field_id']
    keys = [tuple(_identifier(row.get(name)) for name in field_key_names) for row in rows]
    repeats = Counter(key for key in keys if all(value is not None for value in key))
    processed = []
    grouped = defaultdict(list)
    unassigned = []
    for index, row in enumerate(rows):
        row_number = index + 2  # Header is physical CSV line 1; no multiline-cell positioning claimed.
        reasons = [f'missing_identity:{name}' for name in HIERARCHY + tuple(strata)
                   if _identifier(row.get(name)) is None]
        if repeats.get(keys[index], 0) > 1:
            reasons.append('duplicate_field')
        for name in STOMATAL_GATES:
            if not _positive_gate(row.get(name)):
                reasons.append(f'gate_not_true:{name}')
        counts = {name: _number(row.get(name), integer=True) for name in COUNTS}
        area = _number(row.get('area_um2'))
        stomatal_reasons = list(reasons)
        if counts['stomata_pairs'] is None:
            stomatal_reasons.append('invalid_count:stomata_pairs')
        si_reasons = list(stomatal_reasons)
        if counts['nonstomatal_cells'] is None:
            si_reasons.append('invalid_count:nonstomatal_cells')
        if not _positive_gate(row.get('denominator_complete')):
            si_reasons.append('gate_not_true:denominator_complete')
        denominator = None
        if all(value is not None for value in counts.values()):
            denominator = counts['stomata_pairs'] + counts['nonstomatal_cells']
            if denominator == 0:
                si_reasons.append('zero_denominator')
        density_reasons = list(stomatal_reasons)
        if area is None:
            density_reasons.append('missing_or_invalid_physical_area_um2')
        if contract.get('area_unit') != 'um2':
            density_reasons.append('area_unit_not_declared_um2')
        result = {
            'row_number': row_number, 'raw': _json_safe(dict(row)),
            'counts': counts, 'area_um2': area,
            'metrics': {
                SI: _metric(si_reasons, counts['stomata_pairs'], denominator, 100),
                DENSITY: _metric(density_reasons, counts['stomata_pairs'], area, 1000000),
            },
        }
        processed.append(result)
        group_key = tuple(_identifier(row.get(name)) for name in grouping)
        if any(value is None for value in group_key):
            unassigned.append(result)
        else:
            grouped[group_key].append(result)

    plants = []
    for key, members in grouped.items():
        group = dict(zip(grouping, key))
        pool_reasons = []
        if contract.get('field_overlap_policy') != 'nonoverlapping_or_deduplicated':
            pool_reasons.append('field_overlap_policy_not_declared')
        ambiguous = []
        for item in unassigned:
            known = {name: _identifier(item['raw'].get(name)) for name in grouping}
            if all(known[name] is None or known[name] == group[name] for name in grouping):
                ambiguous.append(item['row_number'])
                pool_reasons.append(f'unassigned_compatible_row:{item["row_number"]}')
        counts = {}
        for name in COUNTS:
            values = [item['counts'][name] for item in members]
            counts[name] = sum(values) if all(v is not None for v in values) else None
        areas = [item['area_um2'] for item in members]
        total_area = sum(areas) if all(a is not None for a in areas) else None
        if total_area is not None and not math.isfinite(total_area):
            total_area = None
        reasons_by_metric = {}
        for metric in (SI, DENSITY):
            reasons_by_metric[metric] = pool_reasons + [
                f'row:{item["row_number"]}:{reason}'
                for item in members for reason in item['metrics'][metric]['reasons']]
        if total_area is None and not reasons_by_metric[DENSITY]:
            reasons_by_metric[DENSITY].append('summed_area_out_of_range')
        denominator = (sum(counts.values()) if all(v is not None for v in counts.values()) else None)
        plants.append({
            'group': group, 'field_count': len(members),
            'leaf_count': len({_identifier(item['raw'].get('leaf_id')) for item in members}
                              - {None}),
            'row_numbers': [item['row_number'] for item in members],
            'unassigned_compatible_row_numbers': ambiguous,
            'summed_counts': counts, 'summed_area_um2': total_area,
            'summed_counts_status': 'observed_input_sums_not_approved_when_metrics_blocked',
            'metrics': {
                SI: _metric(reasons_by_metric[SI], counts['stomata_pairs'], denominator, 100),
                DENSITY: _metric(reasons_by_metric[DENSITY], counts['stomata_pairs'], total_area, 1000000),
            },
        })
    all_si_ready = (not unassigned and bool(plants)
                    and all(r['metrics'][SI]['status'] == 'ready' for r in processed + plants))
    return {
        'schema_version': '1.0.0', 'calculator_version': VERSION,
        'calculation_scope': 'arithmetic_on_reviewed_counts_not_segmentation_or_biological_validation',
        'si_release_status': 'ready' if all_si_ready else 'blocked',
        'contract': _json_safe(dict(contract)),
        'grouping_columns': grouping, 'rows': processed, 'plants': plants,
        'unassigned_row_numbers': [item['row_number'] for item in unassigned],
        'replication': {
            'n_field_rows': len(rows), 'n_plant_groups': len(plants), 'biological_n': None,
            'note': 'Field/cell counts are not biological n. Plant groups are descriptive; '
                    'independent experimental units require the experiment design, '
                    'including shared pots, repeated conditions and cohorts.',
        },
        'aggregation_method': 'SI=100*sum(S)/(sum(S)+sum(E)); '
                              'density_per_mm2=1000000*sum(S)/sum(area_um2). '
                              'No failed row is dropped. Mean-field SI is not calculated.',
        'formula_provenance': [
            {'doi': '10.1111/pbi.13070', 'basis': 'pair unit and precursor-inclusive denominator',
             'retrieved_via': 'references/dic-counting-calibration.md'},
            {'doi': '10.1093/plcell/koab260', 'basis': 'nonstomatal cell-count definitions',
             'retrieved_via': 'references/dic-counting-calibration.md'},
        ],
        'qc_basis': 'proposed_operational_checks_not_literature_validated_accuracy_thresholds',
        'raw_encoding_note': 'CSV raw values remain strings; API nonfinite numeric values '
                             'are preserved as text to permit strict JSON.',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='Reviewed-count CSV')
    parser.add_argument('--output', required=True, type=Path, help='JSON report')
    parser.add_argument('--contract', required=True, type=Path, help='Explicit JSON counting contract')
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {args.input.resolve(), args.contract.resolve()}:
            raise ValueError('Output must not overwrite an input or contract file')
        input_bytes = args.input.read_bytes()
        contract_bytes = args.contract.read_bytes()
        declaration = json.loads(contract_bytes)
        with args.input.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise ValueError('CSV has duplicate column names')
            missing = sorted(set(REQUIRED_COLUMNS) - set(headers))
            if missing:
                raise ValueError('CSV missing required columns: ' + ', '.join(missing))
            records = list(reader)
            if any(None in record for record in records):
                raise ValueError('CSV contains extra values without column names')
        report = analyze_counts(records, declaration)
        report['input_provenance'] = {
            'counts_csv_sha256': hashlib.sha256(input_bytes).hexdigest(),
            'contract_json_sha256': hashlib.sha256(contract_bytes).hexdigest(),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                               encoding='utf-8')
    except (OSError, UnicodeError, ValueError, csv.Error) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(f'{len(report["rows"])} fields; {len(report["plants"])} plant groups; '
          f'SI release: {report["si_release_status"]}; report: {args.output}')
    return 0 if report['si_release_status'] == 'ready' else 1


if __name__ == '__main__':
    sys.exit(main())
