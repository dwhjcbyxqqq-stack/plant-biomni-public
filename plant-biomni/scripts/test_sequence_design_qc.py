"""Sequence identity, annotation and non-biological score audit behavior."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PATH = Path(__file__).with_name('sequence_design_qc.py')
SPEC = importlib.util.spec_from_file_location('sequence_design_qc', PATH)
qc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qc)


def candidate(cid='P1', sequence='AAAACGTTCCGG'):
    return {'id': cid, 'sequence': sequence, 'purpose': 'synthetic software fixture',
            'sources': [{'id': 'synthetic:test-sequence', 'version': '1'}],
            'modules': [{'id': 'left', 'start': 0, 'end': 4, 'strand': '+', 'expected_sequence': sequence[:4]}]}


def manifest(*items):
    return {'schema_version': '1.0.0', 'coordinate_convention': '0-based-half-open',
            'candidates': list(items) if items else [candidate()]}


def score(sequence, **changes):
    item = {'value': -0.25, 'checkpoint': 'synthetic-v1', 'tokenization': 'single-base',
            'context': 'full supplied candidate; no padding', 'masking': 'leave-one-base-out',
            'score_definition': 'mean masked log probability per base',
            'input_sequence_sha256': qc.sha256(sequence),
            'scored_region': {'start': 0, 'end': 4, 'strand': '+'},
            'comparison_group': 'group-A'}
    item.update(changes)
    return item


class DesignTests(unittest.TestCase):
    def test_exact_statistics_and_input_preservation(self):
        source = manifest()
        original = deepcopy(source)
        result = qc.analyze_design(source, k=2)
        row = result['candidates'][0]
        self.assertEqual(row['length'], 12)
        self.assertEqual(row['gc_fraction'], 0.5)
        self.assertEqual(source, original)
        self.assertEqual(result['input'], original)
        self.assertEqual(row['modules'][0]['sequence'], 'AAAA')
        self.assertFalse(result['scientific_truth_verified'])
        self.assertFalse(result['promoter_activity_predicted'])
        self.assertFalse(result['guide_offtargets_checked'])

    def test_minus_strand_expected_module_uses_reverse_complement(self):
        item = candidate()
        item['modules'] = [{'id': 'antisense', 'start': 4, 'end': 8, 'strand': '-', 'expected_sequence': 'AACG'}]
        result = qc.analyze_design(manifest(item))
        self.assertEqual(result['candidates'][0]['modules'][0]['sequence'], 'AACG')
        item['modules'][0]['expected_sequence'] = 'CGTT'
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(manifest(item))

    def test_invalid_exact_sequences_fail_without_normalizing(self):
        for sequence in ['', 'ACGN', 'acgt', 'AC GT', 'ACGT\n', 'ACGU', None, 123]:
            with self.subTest(sequence=sequence), self.assertRaises(qc.DesignError):
                qc.analyze_design(manifest(dict(candidate(), sequence=sequence)))

    def test_bad_coordinates_and_convention_fail(self):
        for changes in [{'start': -1}, {'end': 13}, {'start': 4}, {'start': True},
                        {'end': 4.0}, {'strand': 'unknown'}]:
            item = candidate()
            item['modules'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(qc.DesignError):
                qc.analyze_design(manifest(item))
        bad = manifest()
        bad['coordinate_convention'] = '1-based-inclusive'
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(bad)

    def test_duplicate_candidate_or_module_ids_fail(self):
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(manifest(candidate(), candidate()))
        item = candidate()
        item['modules'].append(deepcopy(item['modules'][0]))
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(manifest(item))

    def test_overlap_requires_both_declarations_and_remains_visible(self):
        item = candidate()
        item['modules'].append({'id': 'overlap', 'start': 2, 'end': 6, 'strand': '+', 'expected_sequence': 'AACG'})
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(manifest(item))
        item['modules'][0]['allow_overlap'] = True
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(manifest(item))
        item['modules'][1]['allow_overlap'] = True
        result = qc.analyze_design(manifest(item))
        self.assertIn('declared_module_overlap', {x['code'] for x in result['warnings']})
        item['modules'][1]['allow_overlap'] = 'true'
        with self.assertRaises(qc.DesignError):
            qc.analyze_design(manifest(item))

    def test_adjacent_modules_do_not_overlap(self):
        item = candidate()
        item['modules'].append({'id': 'adjacent', 'start': 4, 'end': 8, 'strand': '+', 'expected_sequence': 'CGTT'})
        self.assertEqual(qc.analyze_design(manifest(item))['warnings'], [])

    def test_sequence_duplicates_distinguished_from_reverse_complements(self):
        a, b, c = candidate('a'), candidate('b'), candidate('c', qc.reverse_complement(candidate()['sequence']))
        result = qc.analyze_design(manifest(a, b, c))
        self.assertEqual(result['exact_duplicate_groups'], [['a', 'b']])
        self.assertEqual(result['reverse_complement_groups'], [['a', 'b', 'c']])
        only_exact = qc.analyze_design(manifest(a, b))
        self.assertEqual(only_exact['reverse_complement_groups'], [])

    def test_kmer_burden_counts_overlapping_redundancy(self):
        result = qc.repeated_kmers('AAAAAC', 2)
        self.assertEqual(result['total_windows'], 5)
        self.assertEqual(result['windows_in_repeated_kmers'], 4)
        self.assertEqual(result['fraction_windows_in_repeated_kmers'], 0.8)
        self.assertIsNone(qc.repeated_kmers('ACG', 12)['fraction_windows_in_repeated_kmers'])
        for bad in [True, 0, 32, 2.0]:
            with self.assertRaises(qc.DesignError):
                qc.analyze_design(manifest(), k=bad)

    def test_missing_biological_provenance_warns_but_sequence_qc_runs(self):
        item = {'id': 'unresolved', 'sequence': 'ACGT', 'modules': [{'id': 'm', 'start': 0, 'end': 2, 'strand': '+'}]}
        result = qc.analyze_design(manifest(item))
        self.assertEqual(result['status'], 'audit_warnings')
        self.assertEqual({x['code'] for x in result['warnings']}, {'missing_purpose', 'missing_source_identity', 'module_identity_unverified'})
        self.assertIsNone(result['candidates'][0]['modules'][0]['expected_sequence_matches'])

    def test_missing_score_metadata_prevents_comparison_status(self):
        a, b = candidate('a'), candidate('b')
        a['model_scores'] = [score(a['sequence'])]
        b['model_scores'] = [{'value': -1, 'comparison_group': 'group-A'}]
        result = qc.analyze_design(manifest(a, b))
        self.assertEqual(result['model_comparison_groups'][0]['status'], 'incomplete_metadata')
        self.assertFalse(result['model_comparison_groups'][0]['scientific_comparability_verified'])

    def test_comparison_requires_same_method_and_lengths(self):
        a, b = candidate('a'), candidate('b')
        a['model_scores'] = [score(a['sequence'])]
        b['model_scores'] = [score(b['sequence'])]
        result = qc.analyze_design(manifest(a, b))
        self.assertEqual(result['model_comparison_groups'][0]['status'], 'declared_metadata_match')
        b['model_scores'][0]['masking'] = 'simultaneous-repeat-mask'
        result = qc.analyze_design(manifest(a, b))
        self.assertEqual(result['model_comparison_groups'][0]['status'], 'declared_metadata_differ')
        b = candidate('b', 'AAAACGTTCCGGAA')
        b['model_scores'] = [score(b['sequence'])]
        result = qc.analyze_design(manifest(a, b))
        self.assertEqual(result['model_comparison_groups'][0]['status'], 'declared_metadata_differ')

    def test_score_claim_hash_and_nonfinite_errors(self):
        for changes in [{'claim_type': 'promoter_activity'}, {'input_sequence_sha256': '0' * 64},
                        {'value': float('nan')}, {'value': float('inf')}, {'value': True}]:
            item = candidate()
            item['model_scores'] = [score(item['sequence'], **changes)]
            with self.subTest(changes=changes), self.assertRaises(qc.DesignError):
                qc.analyze_design(manifest(item))


class CommandTests(unittest.TestCase):
    def run_command(self, directory, data):
        source, dest = Path(directory) / 'input.json', Path(directory) / 'output.json'
        source.write_text(json.dumps(data), encoding='utf-8')
        run = subprocess.run([sys.executable, str(PATH), '--input', str(source), '--output', str(dest)], capture_output=True, text=True)
        return run, source, dest

    def test_cli_creates_hash_bound_audit_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            run, source, dest = self.run_command(directory, manifest())
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(dest.read_text())
            self.assertEqual(result['input_file_sha256'], __import__('hashlib').sha256(source.read_bytes()).hexdigest())
            old = dest.read_bytes()
            run, _, _ = self.run_command(directory, manifest())
            self.assertEqual(run.returncode, 1)
            self.assertEqual(dest.read_bytes(), old)

    def test_validation_error_produces_no_output_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as directory:
            item = candidate()
            item['modules'][0]['expected_sequence'] = 'CCCC'
            run, source, dest = self.run_command(directory, manifest(item))
            self.assertEqual(run.returncode, 1)
            self.assertFalse(dest.exists())
            self.assertEqual(run.stdout, '')
            self.assertEqual(json.loads(source.read_text()), manifest(item))


if __name__ == '__main__':
    unittest.main()
