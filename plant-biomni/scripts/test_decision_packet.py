import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from decision_packet import create_packet, digest, save_packet, validate_packet


class DecisionPacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = [self.root/name for name in ('index.json', 'notices.json', 'registry.json')]
        self.row = {'record_key': 'source.json#/0', 'source_record_id': 'R-1',
                    'doi_key': '10.1234/example', 'title': 'Example primary', 'url': 'https://example.org/primary',
                    'locator': 'Figure 1', 'read_scope': 'full_text_read', 'publication_status': 'published',
                    'layers': [], 'species': ['Example species'], 'provenance': [{'source': 'source.json', 'json_pointer': '/0'}],
                    'original': {'publication_type': 'primary_research', 'context': {'tissue': 'leaf'},
                                 'dependency_dataset': 'DATA-1', 'notice_check': {'status': 'checked_no_notice_found'}}}
        second = copy.deepcopy(self.row)
        second.update(record_key='source.json#/1', source_record_id='R-2')
        second['original']['context'] = {'tissue': 'root'}
        self.index = {'site_commit': 'a'*40, 'records': [self.row, second]}
        self.notices = {'version': '1.0.0', 'notices': []}
        self.registry = {'registry_version': '1.0.0', 'workflows': [
            {'id': 'PB-WF-TEST', 'version': '1.0.0', 'status': 'runnable_analysis_unvalidated',
             'canonical_status': 'current', 'execution_kind': 'digital', 'unresolved': []}]}
        self.write_sources()
        self.packet_path = self.root/'packet.json'

    def write_sources(self):
        for path, obj in zip(self.paths, (self.index, self.notices, self.registry)):
            path.write_text(json.dumps(obj))

    def new(self, selectors=None):
        return create_packet(*self.paths, selectors or [{'kind': 'record', 'value': 'R-1'}], ['PB-WF-TEST'], 'Test decision')

    def validate(self, packet):
        return validate_packet(packet, self.packet_path)

    def ready(self, packet):
        packet['status'] = 'ready_for_execution'
        dec = packet['decision']
        dec['objective_and_estimand'] = {'objective': 'Test', 'outcome': 'SI', 'units': '%', 'denominator': 'epidermal cells', 'interval': 'day 5'}
        dec['context'] = {k: 'Explicit ' + k for k in dec['context']}
        dec['strongest_alternative'] = 'Expansion rather than lineage production'
        dec['controls_and_comparisons'] = ['Stage-matched control; plant is randomized unit']
        dec['required_outputs'] = ['Counts and area by plant']
        dec['continue_revise_stop'] = {'continue': 'Matched count definitions', 'revise': 'Ambiguous stage', 'stop': 'Unresolved identity'}
        artifact = self.root/'review.txt'
        artifact.write_text('Source passage and methods review notes')
        packet['source_reviews'] = [
            {'record_key': s['record_key'], 'reviewer': 'Analyst', 'reviewed_at': '2026-11-06T08:00:00+08:00',
             'passage_locators': ['Methods; Figure 1'], 'fit_to_task': 'Measurement method is applicable to stated estimand',
             'addressed_issues': {issue: 'Reviewed in attached notes, with scope retained' for issue in s['review_issues']},
             'artifact': {'path': artifact.name, 'sha256': digest(artifact.read_bytes())}}
            for s in dec['source_records']]
        return packet

    def test_planned_unknowns_are_valid(self):
        packet = self.new()
        result = self.validate(packet)
        self.assertTrue(result['valid'], result)
        self.assertIsNone(packet['decision']['context']['genotype'])
        self.assertFalse(result['scientific_truth_verified'])

    def test_doi_selection_preserves_contexts_and_reused_dataset(self):
        packet = self.new([{'kind': 'doi', 'value': 'https://doi.org/10.1234/EXAMPLE'}])
        sources = packet['decision']['source_records']
        self.assertEqual(len(sources), 2)
        self.assertNotEqual(sources[0]['context'], sources[1]['context'])
        self.assertEqual(sources[0]['dataset_dependencies'], sources[1]['dataset_dependencies'])
        self.assertEqual(len(packet['gates']['doi_groups']), 1)
        self.assertIsNone(packet['gates']['independent_experiment_count'])

    def test_ambiguous_source_id_requires_record_key(self):
        self.index['records'][1]['source_record_id'] = 'R-1'
        self.write_sources()
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.new()
        self.assertTrue(self.validate(self.new([{'kind': 'record', 'value': 'source.json#/0'}]))['valid'])

    def test_unknown_workflow_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown workflow'):
            create_packet(*self.paths, [{'kind': 'record', 'value': 'R-1'}], ['PB-WF-MISSING'], 'Task')

    def test_retraction_cannot_be_declared_ready(self):
        self.notices['notices'] = [{'doi': '10.1234/example', 'status': 'retracted'}]
        self.write_sources()
        packet = self.new()
        self.assertEqual(packet['gates']['source_reuse'], 'blocked')
        self.assertTrue(self.validate(packet)['valid'])  # honest audit plan remains possible
        result = self.validate(self.ready(packet))
        self.assertIn('blocked_source_cannot_be_declared_ready', result['errors'])

    def test_expression_of_concern_record_id_blocks_even_without_doi(self):
        self.notices['notices'] = [{'affected_record_ids': ['R-1'], 'status': 'expression_of_concern'}]
        self.write_sources()
        self.assertEqual(self.new()['gates']['source_reuse'], 'blocked')

    def test_correction_unknown_scope_and_review_are_preserved(self):
        self.notices['notices'] = [{'doi': '10.1234/example', 'status': 'correction_indexed'}]
        self.row['original']['publication_type'] = 'review'
        self.row['read_scope'] = 'abstract_only'
        self.write_sources()
        packet = self.new()
        source = packet['decision']['source_records'][0]
        self.assertEqual(source['publication_role'], 'review_synthesis')
        self.assertEqual(source['source_gate'], 'review_required')
        self.assertIn('corrected_material_requires_review', source['review_issues'])
        self.assertIn('primary_methods_read_scope_unresolved', source['review_issues'])
        self.assertIn('review_is_not_primary_experimental_support', source['review_issues'])

    def test_negated_full_text_scope_requires_review(self):
        self.row['read_scope'] = 'full text not read; abstract only'
        self.write_sources()
        issues = self.new()['decision']['source_records'][0]['review_issues']
        self.assertIn('primary_methods_read_scope_unresolved', issues)

    def test_camelcase_live_review_keeps_review_scope(self):
        self.row['original'].pop('publication_type')
        self.row['original'].update(publicationType='review', evidenceRole='review_synthesis')
        self.write_sources()
        source = self.new()['decision']['source_records'][0]
        self.assertEqual(source['publication_role'], 'review_synthesis')
        self.assertIn('review_is_not_primary_experimental_support', source['review_issues'])
        self.assertFalse(source['publication_role_conflict'])

    def test_camelcase_live_primary_is_explicit_declaration(self):
        self.row['original'].pop('publication_type')
        self.row['original'].update(publicationType='primary_research', evidenceRole='primary_report')
        self.write_sources()
        source = self.new()['decision']['source_records'][0]
        self.assertEqual(source['publication_role'], 'primary_report_declared')
        self.assertFalse(source['publication_role_conflict'])

    def test_cross_species_kind_primary_is_explicit_declaration(self):
        self.row['original'].pop('publication_type')
        self.row['original']['kind'] = 'primary'
        self.write_sources()
        source = self.new()['decision']['source_records'][0]
        self.assertEqual(source['publication_role'], 'primary_report_declared')

    def test_generic_article_is_not_automatically_primary(self):
        for generic in ['article', 'journal article', 'journal_article']:
            self.row['original']['publication_type'] = generic
            self.write_sources()
            source = self.new()['decision']['source_records'][0]
            self.assertEqual(source['publication_role'], 'unknown')
            self.assertIn('publication_role_unresolved', source['review_issues'])

    def test_conflicting_schema_declarations_require_review(self):
        self.row['original']['publicationType'] = 'review'
        self.row['original']['evidenceRole'] = 'primary_report'
        self.write_sources()
        source = self.new()['decision']['source_records'][0]
        self.assertEqual(source['publication_role'], 'review_synthesis')
        self.assertTrue(source['publication_role_conflict'])
        self.assertIn('conflicting_publication_role_declarations', source['review_issues'])
        self.assertEqual({d['field'] for d in source['publication_role_declarations']},
                         {'publication_type', 'publicationType', 'evidenceRole'})

    def test_top_level_notice_cannot_be_hidden_by_original_check(self):
        self.row['notice_check'] = {'status': 'retracted'}
        self.write_sources()
        self.assertEqual(self.new()['gates']['source_reuse'], 'blocked')

    def test_source_hash_drift_rejected(self):
        packet = self.new()
        self.paths[0].write_text(self.paths[0].read_text() + '\n')
        self.assertIn('index_sha256_mismatch', self.validate(packet)['errors'])

    def test_notice_registry_drift_rejected(self):
        packet = self.new()
        self.paths[1].write_text(self.paths[1].read_text() + '\n')
        self.paths[2].write_text(self.paths[2].read_text() + '\n')
        result = self.validate(packet)
        self.assertIn('notices_sha256_mismatch', result['errors'])
        self.assertIn('registry_sha256_mismatch', result['errors'])

    def test_forged_source_and_gate_rejected(self):
        packet = self.new()
        packet['decision']['source_records'][0]['context'] = {'tissue': 'other'}
        packet['gates']['independent_experiment_count'] = 2
        result = self.validate(packet)
        self.assertIn('source_records_differ_from_selected_snapshot', result['errors'])
        self.assertIn('source_gates_or_grouping_modified', result['errors'])

    def test_unknown_selector_rejected(self):
        packet = self.new()
        packet['selection'] = [{'kind': 'record', 'value': 'invented'}]
        self.assertFalse(self.validate(packet)['valid'])

    def test_false_completion_or_writeback_rejected(self):
        packet = self.new()
        packet['status'] = 'completed'
        packet['decision']['execution_and_writeback']['writeback'] = 'completed'
        result = self.validate(packet)
        self.assertIn('packet_cannot_assert_execution_or_completion', result['errors'])
        self.assertIn('planning_packet_cannot_claim_execution_or_writeback', result['errors'])

    def test_ready_requires_scientific_fields_and_review(self):
        packet = self.new()
        packet['status'] = 'ready_for_execution'
        result = self.validate(packet)
        self.assertFalse(result['valid'])
        self.assertIn('ready_packet_missing_context', result['errors'])
        self.assertTrue(any('missing_source_review' in e for e in result['errors']))

    def test_ready_explicit_fields_and_traceable_review_pass_metadata_only(self):
        result = self.validate(self.ready(self.new()))
        self.assertTrue(result['valid'], result)
        self.assertFalse(result['scientific_truth_verified'])
        self.assertFalse(result['execution_performed'])

    def test_modified_review_artifact_fails(self):
        packet = self.ready(self.new())
        (self.root/'review.txt').write_text('changed')
        self.assertIn('source_review_artifact_hash_mismatch', self.validate(packet)['errors'])

    def test_planned_workflow_cannot_be_marked_execution_ready(self):
        self.registry['workflows'][0]['status'] = 'planned'
        self.write_sources()
        result = self.validate(self.ready(self.new()))
        self.assertIn('workflow_not_execution_ready_or_has_unresolved_parameters', result['errors'])

    def resolution(self, requirement, value='1 uM, as specified by the linked assay design'):
        artifact = self.root/'parameter-review.txt'
        artifact.write_text('Parameter source and context-specific decision')
        return {'workflow_id': 'PB-WF-TEST', 'workflow_version': '1.0.0',
                'unresolved': requirement, 'resolution': value,
                'artifact': {'path': artifact.name, 'sha256': digest(artifact.read_bytes())}}

    def test_run_specific_unresolved_parameter_can_be_resolved(self):
        self.registry['workflows'][0]['unresolved'] = ['Dose requires source-specific selection']
        self.write_sources()
        packet = self.ready(self.new())
        before = copy.deepcopy(packet['decision']['workflow_ids_versions'])
        self.assertFalse(self.validate(packet)['valid'])
        packet['workflow_resolutions'] = [self.resolution('Dose requires source-specific selection')]
        result = self.validate(packet)
        self.assertTrue(result['valid'], result)
        self.assertEqual(before, packet['decision']['workflow_ids_versions'])

    def test_missing_one_workflow_parameter_remains_blocker(self):
        self.registry['workflows'][0]['unresolved'] = ['Dose', 'Exposure duration']
        self.write_sources()
        packet = self.ready(self.new())
        packet['workflow_resolutions'] = [self.resolution('Dose')]
        result = self.validate(packet)
        self.assertFalse(result['valid'])
        self.assertTrue(any(e.startswith('workflow_unresolved_parameter:') for e in result['errors']))

    def test_workflow_resolution_wrong_version_or_requirement_rejected(self):
        self.registry['workflows'][0]['unresolved'] = ['Dose']
        self.write_sources()
        packet = self.new()
        resolution = self.resolution('Dose')
        resolution['workflow_version'] = '2.0.0'
        packet['workflow_resolutions'] = [resolution]
        self.assertIn('workflow_resolution_not_in_pinned_workflow', self.validate(packet)['errors'])
        resolution['workflow_version'] = '1.0.0'
        resolution['unresolved'] = 'Different parameter'
        self.assertIn('workflow_resolution_not_in_pinned_workflow', self.validate(packet)['errors'])

    def test_workflow_resolution_unknown_or_unjustified_na_rejected(self):
        self.registry['workflows'][0]['unresolved'] = ['Dose']
        self.write_sources()
        packet = self.new()
        for value in ['unknown', {'status': 'not_applicable', 'reason': ''}]:
            packet['workflow_resolutions'] = [self.resolution('Dose', value)]
            self.assertIn('workflow_resolution_missing_concrete_value', self.validate(packet)['errors'])

    def test_workflow_resolution_justified_na_and_hash_checked(self):
        self.registry['workflows'][0]['unresolved'] = ['Dose']
        self.write_sources()
        packet = self.ready(self.new())
        packet['workflow_resolutions'] = [self.resolution('Dose', {'status': 'not_applicable', 'reason': 'Reanalysis of supplied untreated samples'})]
        self.assertTrue(self.validate(packet)['valid'])
        (self.root/'parameter-review.txt').write_text('changed')
        self.assertIn('workflow_resolution_artifact_hash_mismatch', self.validate(packet)['errors'])

    def test_saved_packet_preserves_snapshots_after_originals_change(self):
        packet = save_packet(self.new(), self.packet_path)
        for name in ('index', 'notices', 'registry'):
            pin = packet['pins'][name]
            self.assertFalse(Path(pin['path']).is_absolute())
            snapshot = self.root/pin['path']
            self.assertEqual(digest(snapshot.read_bytes()), pin['sha256'])
        for path in self.paths:
            path.write_text('{}')
        result = self.validate(packet)
        self.assertTrue(result['valid'], result)

    def test_saved_snapshot_modification_detected(self):
        packet = save_packet(self.new(), self.packet_path)
        path = self.root/packet['pins']['notices']['path']
        path.write_text(path.read_text() + '\n')
        self.assertIn('notices_sha256_mismatch', self.validate(packet)['errors'])

    def test_snapshot_save_detects_drift_before_write(self):
        packet = self.new()
        self.paths[0].write_text('{}')
        with self.assertRaisesRegex(ValueError, 'changed before'):
            save_packet(packet, self.packet_path)
        self.assertFalse(self.packet_path.exists())

    def test_identical_snapshots_reused_and_corrupt_snapshot_refused(self):
        first = save_packet(self.new(), self.packet_path)
        second = save_packet(self.new(), self.root/'second.json')
        self.assertEqual(first['pins'], second['pins'])
        snapshot = self.root/first['pins']['registry']['path']
        snapshot.write_text('corrupt')
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            save_packet(self.new(), self.root/'third.json')
        self.assertFalse((self.root/'third.json').exists())

    def test_relocated_identical_snapshot_accepted(self):
        packet = self.new()
        copied = self.root/'copied.json'
        copied.write_bytes(self.paths[0].read_bytes())
        self.assertTrue(validate_packet(packet, self.packet_path, {'index': copied})['valid'])

    def test_cli_refuses_existing_output(self):
        script = Path(__file__).with_name('decision_packet.py')
        command = [sys.executable, str(script), 'new', '--record', 'R-1', '--workflow', 'PB-WF-TEST', '--task', 'Task', '--output', str(self.packet_path)]
        for key, path in zip(('index', 'notices', 'registry'), self.paths):
            command += ['--'+key, str(path)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stdout)
        before = self.packet_path.read_bytes()
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(before, self.packet_path.read_bytes())


if __name__ == '__main__':
    unittest.main()
