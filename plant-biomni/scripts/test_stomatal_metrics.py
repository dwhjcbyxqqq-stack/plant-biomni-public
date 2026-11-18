"""Behavioral safety tests for reviewed count arithmetic, not image accuracy."""
import copy
import csv
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

PATH = pathlib.Path(__file__).with_name('stomatal_metrics.py')
SPEC = importlib.util.spec_from_file_location('stomatal_metrics', PATH)
metrics = importlib.util.module_from_spec(SPEC) if PATH.exists() else None
if metrics is not None:
    SPEC.loader.exec_module(metrics)


def contract():
    return {
        'contract_id': 'reviewed-pairs-v1',
        'formula': 'SI=100*S/(S+E)',
        'stomatal_unit': 'one_guard_cell_pair',
        'nonstomatal_unit': 'all_nonstomatal_epidermal_cells_including_precursors',
        'common_roi': True,
        'boundary_convention': 'Half-open anatomical-centre ROI: top/left in, bottom/right out',
        'field_overlap_policy': 'nonoverlapping_or_deduplicated',
        'area_unit': 'um2',
    }


def row(**changes):
    data = dict(sample_id='experiment-1', plant_id='P1', leaf_id='L1', field_id='F1',
                stomata_pairs=10, nonstomatal_cells=90,
                denominator_complete=True, identity_resolved=True, units_resolved=True,
                border_resolved=True, same_roi=True, area_um2=100000)
    data.update(changes)
    return data


class ReviewedCountTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(metrics, 'Reviewed-count helper is not implemented')

    def analyze(self, rows=None, declaration=None):
        return metrics.analyze_counts([row()] if rows is None else rows,
                                      contract() if declaration is None else declaration)

    def test_si_and_density_use_pair_units_and_mm2_conversion(self):
        result = self.analyze()['rows'][0]
        self.assertEqual(result['metrics']['si_percent']['value'], 10)
        self.assertEqual(result['metrics']['density_per_mm2']['value'], 100)
        self.assertEqual(result['raw']['stomata_pairs'], 10)

    def test_contract_is_required_and_cannot_silently_change_denominator(self):
        for bad in [None, {}, dict(contract(), stomatal_unit='individual_guard_cell'),
                    dict(contract(), nonstomatal_unit='pavement_cells_only'),
                    dict(contract(), common_roi='true'),
                    dict(contract(), formula='SI=100*S/(2*S+E)'),
                    dict(contract(), boundary_convention='')]:
            with self.subTest(bad=bad), self.assertRaises(metrics.ContractError):
                metrics.analyze_counts([row()], bad)

    def test_missing_or_nonpositive_gates_never_default_to_true(self):
        for gate in ['denominator_complete', 'identity_resolved', 'units_resolved',
                     'border_resolved', 'same_roi']:
            for value in [False, None, '', 'unknown', 1, '1', 'yes']:
                item = row(**{gate: value})
                result = self.analyze([item])['rows'][0]
                self.assertIsNone(result['metrics']['si_percent']['value'], (gate, value))
                self.assertTrue(result['metrics']['si_percent']['reasons'])
            item = row()
            del item[gate]
            self.assertIsNone(self.analyze([item])['rows'][0]['metrics']['si_percent']['value'])

    def test_csv_true_is_accepted_but_false_is_not_truthy(self):
        item = row(**{g: 'true' for g in ['denominator_complete', 'identity_resolved',
                                         'units_resolved', 'border_resolved', 'same_roi']})
        self.assertEqual(self.analyze([item])['rows'][0]['metrics']['si_percent']['value'], 10)
        item['identity_resolved'] = 'false'
        self.assertIsNone(self.analyze([item])['rows'][0]['metrics']['si_percent']['value'])

    def test_invalid_counts_are_retained_and_block_affected_metrics(self):
        for field in ['stomata_pairs', 'nonstomatal_cells']:
            for bad in [True, False, -1, 1.25, float('nan'), float('inf'), 'NaN', 'Infinity', '', None]:
                with self.subTest(field=field, bad=bad):
                    result = self.analyze([row(**{field: bad})])['rows'][0]
                    self.assertIsNone(result['metrics']['si_percent']['value'])
                    self.assertTrue(result['metrics']['si_percent']['reasons'])
                    self.assertIn(field, result['raw'])
                    if field == 'stomata_pairs':
                        self.assertIsNone(result['metrics']['density_per_mm2']['value'])

    def test_missing_or_bad_denominator_can_leave_reviewed_stomatal_density_ready(self):
        for item in [row(denominator_complete=False), row(nonstomatal_cells=''),
                     row(nonstomatal_cells='invalid')]:
            result = self.analyze([item])
            self.assertIsNone(result['rows'][0]['metrics']['si_percent']['value'])
            self.assertEqual(result['rows'][0]['metrics']['density_per_mm2']['value'], 100)
            self.assertEqual(result['plants'][0]['metrics']['density_per_mm2']['value'], 100)

    def test_density_requires_every_stomatal_gate(self):
        for gate in ['identity_resolved', 'units_resolved', 'border_resolved', 'same_roi']:
            result = self.analyze([row(**{gate: False})])
            self.assertIsNone(result['rows'][0]['metrics']['density_per_mm2']['value'])

    def test_zero_denominator_blocks_si_but_zero_stomata_with_cells_is_valid(self):
        empty = self.analyze([row(stomata_pairs=0, nonstomatal_cells=0)])['rows'][0]
        self.assertIsNone(empty['metrics']['si_percent']['value'])
        self.assertEqual(empty['metrics']['density_per_mm2']['value'], 0)
        zero = self.analyze([row(stomata_pairs=0)])['rows'][0]
        self.assertEqual(zero['metrics']['si_percent']['value'], 0)

    def test_area_must_be_positive_finite_and_explicitly_um2(self):
        for area in [None, '', True, False, 0, -1, 'NaN', 'Infinity', float('inf')]:
            result = self.analyze([row(area_um2=area)])['rows'][0]
            self.assertEqual(result['metrics']['si_percent']['value'], 10)
            self.assertIsNone(result['metrics']['density_per_mm2']['value'], area)
        bad = dict(contract(), area_unit='pixels2')
        with self.assertRaises(metrics.ContractError):
            self.analyze(declaration=bad)

    def test_nonempty_sample_plant_leaf_field_hierarchy_is_required(self):
        for key in ['sample_id', 'plant_id', 'leaf_id', 'field_id']:
            for bad in ['', '  ', None]:
                result = self.analyze([row(**{key: bad})])
                self.assertIsNone(result['rows'][0]['metrics']['si_percent']['value'])
                self.assertIsNone(result['rows'][0]['metrics']['density_per_mm2']['value'])
                self.assertEqual(len(result['rows']), 1)

    def test_duplicate_fields_block_whole_affected_plant(self):
        result = self.analyze([row(), row(), row(plant_id='P2')])
        self.assertEqual(len(result['rows']), 3)
        self.assertIsNone(result['plants'][0]['metrics']['si_percent']['value'])
        self.assertIsNone(result['plants'][0]['metrics']['density_per_mm2']['value'])
        self.assertIn('duplicate_field', ' '.join(result['plants'][0]['metrics']['si_percent']['reasons']))
        self.assertEqual(result['plants'][1]['metrics']['si_percent']['value'], 10)

    def test_duplicate_detection_keeps_distinct_leaves_and_strata_separate(self):
        result = self.analyze([row(cohort='A'), row(leaf_id='L2', cohort='A'), row(cohort='B')])
        self.assertEqual(len(result['plants']), 2)
        self.assertTrue(all(p['metrics']['si_percent']['value'] == 10 for p in result['plants']))

    def test_pooled_si_is_ratio_of_summed_counts_not_mean_field_si(self):
        result = self.analyze([row(stomata_pairs=1, nonstomatal_cells=1),
                               row(field_id='F2', stomata_pairs=9, nonstomatal_cells=89)])
        plant = result['plants'][0]
        self.assertEqual(plant['summed_counts'], {'stomata_pairs': 10, 'nonstomatal_cells': 90})
        self.assertEqual(plant['metrics']['si_percent']['value'], 10)
        self.assertNotAlmostEqual(plant['metrics']['si_percent']['value'], (50+100*9/98)/2)
        self.assertEqual(plant['metrics']['density_per_mm2']['value'], 50)
        self.assertEqual(plant['field_count'], 2)
        self.assertNotIn('biological_n', plant)

    def test_incomplete_field_is_not_dropped_from_plant_si_or_density(self):
        result = self.analyze([row(), row(field_id='F2', denominator_complete=False)])
        self.assertIsNone(result['plants'][0]['metrics']['si_percent']['value'])
        self.assertEqual(result['plants'][0]['field_count'], 2)
        self.assertEqual(result['plants'][0]['summed_counts']['stomata_pairs'], 20)
        self.assertEqual(result['plants'][0]['metrics']['density_per_mm2']['value'], 100)
        result = self.analyze([row(), row(field_id='F2', area_um2='')])
        self.assertIsNone(result['plants'][0]['metrics']['density_per_mm2']['value'])
        self.assertIsNone(result['plants'][0]['summed_area_um2'])

    def test_plant_grouping_includes_sample_and_declared_strata(self):
        result = self.analyze([row(cohort='C1', condition='control'),
                               row(cohort='C1', condition='stress'),
                               row(sample_id='experiment-2', cohort='C1', condition='control')])
        self.assertEqual(len(result['plants']), 3)
        self.assertEqual(result['replication']['n_plant_groups'], 3)
        self.assertIsNone(result['replication']['biological_n'])

    def test_missing_stratum_does_not_blend_into_a_group(self):
        result = self.analyze([row(cohort='C1'), row(field_id='F2')])
        self.assertIsNone(result['rows'][1]['metrics']['si_percent']['value'])
        self.assertIn(3, result['unassigned_row_numbers'])
        self.assertIsNone(result['plants'][0]['metrics']['si_percent']['value'])

    def test_pooling_requires_overlap_declaration(self):
        declaration = contract()
        del declaration['field_overlap_policy']
        result = self.analyze([row(), row(field_id='F2')], declaration)
        self.assertEqual(result['rows'][0]['metrics']['si_percent']['value'], 10)
        self.assertIsNone(result['plants'][0]['metrics']['si_percent']['value'])
        self.assertIsNone(result['plants'][0]['metrics']['density_per_mm2']['value'])

    def test_empty_dataset_is_not_success(self):
        with self.assertRaises(ValueError):
            self.analyze([])

    def test_input_is_not_mutated(self):
        rows = [row()]
        before = copy.deepcopy(rows)
        self.analyze(rows)
        self.assertEqual(rows, before)

    def test_cli_writes_strict_json_and_returns_nonzero_for_blocked_si(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            input_path, output_path, contract_path = [path / f for f in ['counts.csv','out.json','contract.json']]
            with input_path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row()))
                writer.writeheader()
                writer.writerow(row(denominator_complete='false'))
            contract_path.write_text(json.dumps(contract()))
            process = subprocess.run([sys.executable, str(PATH), '--input', str(input_path),
                                      '--output', str(output_path), '--contract', str(contract_path)],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 1, process.stderr)
            output = json.loads(output_path.read_text())
            self.assertIsNone(output['rows'][0]['metrics']['si_percent']['value'])
            self.assertEqual(output['rows'][0]['raw']['denominator_complete'], 'false')

    def test_cli_requires_contract_and_rejects_missing_required_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            inp, out, con = [path / f for f in ['counts.csv','out.json','contract.json']]
            inp.write_text('stomata_pairs\n10\n')
            con.write_text(json.dumps(contract()))
            for args in [[], ['--contract', str(con)]]:
                process = subprocess.run([sys.executable, str(PATH), '--input', str(inp),
                                          '--output', str(out)] + args, capture_output=True, text=True)
                self.assertEqual(process.returncode, 2)
                self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
