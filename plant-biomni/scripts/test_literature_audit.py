import unittest
from literature_audit import audit, canonical_doi
from pb_contracts import check_evidence


class LiteratureAuditTests(unittest.TestCase):
    def test_notice_must_apply_to_the_record_doi(self):
        r = audit([{'doi':'10.1000/source', 'notice_check':{'doi':'10.1000/other', 'status':'correction_indexed', 'checked_at':'2026-11-06', 'source_url':'https://example.test'}}])
        self.assertIn('notice_source_doi_mismatch', [x['code'] for x in r['issues']])

    def test_matching_notice_fixture_doi_is_accepted(self):
        rows = [{'doi': '10.1000/fixture', 'notice_check': {
            'doi': '10.1000/fixture', 'status': 'checked_no_notice_found',
            'checked_at': '2026-01-01', 'source_url': 'https://example.test/notice'}}]
        self.assertFalse([x for x in audit(rows)['issues']
                          if x['code'] == 'notice_source_doi_mismatch'])

    def test_explicit_doi_urls_and_legal_suffixes(self):
        self.assertEqual(canonical_doi(' HTTPS://doi.org/10.1000%2FABC(1)?utm_source=x#top '), '10.1000/abc(1)')
        self.assertEqual(canonical_doi('doi: 10.1000/abc.'), '10.1000/abc.')
        self.assertIsNone(canonical_doi('https://publisher.test/10.1000/abc'))
        self.assertIsNone(canonical_doi('10.1000/a b'))
        self.assertIsNone(canonical_doi('10.1000/a\nB'))

    def test_contexts_preserved_and_title_mismatch_flagged(self):
        rows = [{'id':'a','doi':'10.1000/X','title':'One title','context':{'species':'A'}},
                {'id':'b','doi':'https://doi.org/10.1000/x','title':'One title','context':{'species':'B'}},
                {'id':'c','doi':'10.1000/x','title':'Unrelated title'}]
        r = audit(rows)
        self.assertEqual(r['record_count'], 3)
        self.assertEqual(r['doi_group_count'], 1)
        self.assertEqual(r['exact_record_duplicate_groups'], [])
        self.assertEqual(r['coverage']['species'], {'A':1,'B':1,'unknown':1})
        self.assertIn('doi_title_variants_review', [x['code'] for x in r['issues']])

    def test_different_dois_are_never_fuzzy_merged(self):
        r=audit([{'doi':'10.1000/one','title':'Identical title'}, {'doi':'10.1000/two','title':'Identical title'}])
        self.assertEqual(r['doi_group_count'],2)
        self.assertIn('title_multiple_dois_review',[x['code'] for x in r['issues']])

    def test_review_and_notice_provenance(self):
        r=audit([{'publication_type':'review','evidence_role':'primary','notice_check':{'status':'checked_no_notice_found'}},
                 {'notice_check':{'status':'retracted','checked_at':'2026-11-06','source_url':'https://example.test'}}])
        self.assertEqual({x['code'] for x in r['issues']},{'review_primary_boundary','notice_status_without_provenance','publication_notice_requires_adjudication'})

    def test_site_primary_report_role_is_not_allowed_for_review(self):
        r=audit([{'publication_type':'review','evidence_role':'primary_report'}])
        self.assertIn('review_primary_boundary', [x['code'] for x in r['issues']])

    def test_snapshot_mismatch_and_incomplete_export(self):
        self.assertEqual(audit({'site_commit':'old','records':[]},'new')['issues'][0]['code'],'snapshot_commit_mismatch')
        for payload in [{'records':[], 'has_more':True},{'records':[],'next_offset':2},{'records':[], 'truncated':True}]:
            with self.assertRaises(ValueError): audit(payload)

    def test_review_not_experimental_replication(self):
        r=check_evidence({'evidence_type':'review_article','claim_type':'independent_replication'})
        self.assertEqual(r['results'][0]['status'],'blocked')


if __name__=='__main__': unittest.main()
