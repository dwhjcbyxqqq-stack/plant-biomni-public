"""Inference boundaries exposed by crop/peptide/RNA literature, not biology tests."""
import unittest
from pb_contracts import check_evidence


class PaperEvidenceTests(unittest.TestCase):
    def check(self, evidence, claim, expected):
        result = check_evidence({"evidence_type": evidence, "claim_type": claim})
        self.assertEqual(result["status"], expected, result)
        self.assertIs(result["scientific_truth_verified"], False)

    def test_cross_scale_claims_are_explicitly_blocked(self):
        for evidence, claims in {
            "stomatal_density": ["lineage_entry", "water_use_efficiency", "yield_benefit"],
            "heterologous_rescue": ["native_species_function", "native_circuit_validation"],
            "precursor_expression": ["mature_peptide_activity"],
            "m6a_enrichment": ["single_nucleotide_methylation", "rna_stability_mechanism"],
            "steady_state_rna": ["rna_half_life"],
            "fluorescent_puncta": ["phase_separation"],
            "acute_stomatal_aperture": ["stomatal_development"],
            "intrinsic_wue": ["whole_plant_wue", "yield_benefit"],
            "bulk_chromatin_shift": ["cell_autonomous_recruitment"],
        }.items():
            for claim in claims:
                with self.subTest(evidence=evidence, claim=claim):
                    self.check(evidence, claim, "blocked")

    def test_matched_descriptions_remain_available(self):
        for evidence, claim in [
            ("stomatal_density", "density_difference"),
            ("heterologous_rescue", "rescue_in_tested_host"),
            ("precursor_expression", "precursor_detected"),
            ("m6a_enrichment", "rna_region_enrichment"),
            ("steady_state_rna", "rna_abundance_difference"),
            ("fluorescent_puncta", "puncta_observed"),
            ("acute_stomatal_aperture", "aperture_response"),
            ("intrinsic_wue", "a_over_gsw"),
            ("bulk_chromatin_shift", "bulk_chromatin_association"),
        ]:
            with self.subTest(evidence=evidence):
                self.check(evidence, claim, "within_scope")

    def test_added_evidence_does_not_launder_forbidden_inference(self):
        self.check(["stomatal_density", "perturbation"], "yield_benefit", "blocked")

    def test_unlisted_inference_remains_review_required(self):
        self.check("m6a_enrichment", "reader_causal_specificity", "requires_review")


if __name__ == "__main__":
    unittest.main()
