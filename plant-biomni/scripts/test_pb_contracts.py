#!/usr/bin/env python3
"""Contract regression tests: real CLI, temporary immutable inputs, no wet-lab execution."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("pb_contracts.py")
SPEC = importlib.util.spec_from_file_location("pb_contracts_under_test", SCRIPT)
CONTRACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACTS)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = {
            "schema_version": "1.0.0", "registry_version": "2.0.0",
            "workflow_count": 1, "workflows": [{
                "id": "PB-WF-TEST", "version": "1.0.0-draft", "name": "Fixture analysis",
                "domain": "analysis", "status": "source_backed_partial",
                "canonical_status": "candidate", "provenance": [],
                "inputs": ["Data"], "outputs": ["Result"],
                "steps": ["Inspect input", {"instruction": "Calculate result", "order": 2}],
                "qc_gates": ["Inspect provenance", {"requirement": "Review result", "required": True}],
                "dependencies": ["Python standard library"], "unresolved": [], "supersedes": None,
            }],
        }
        self.registry_path = self.write_json("registry.json", self.registry)

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def call(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip().startswith("{"), result.stdout + result.stderr)
        return json.loads(result.stdout)

    def validate_registry(self, registry, expected=1):
        return self.call("validate-registry", self.write_json("registry.json", registry), expected=expected)

    def new_run(self):
        path = self.root / "run.json"
        self.call("new-run", "--workflow", "PB-WF-TEST", "--task", "Audit fixture data",
                  "--registry", self.registry_path, "--output", path)
        return json.loads(path.read_text()), path

    def artifact(self, name, text="measured value\n"):
        path = self.root / name
        path.write_text(text)
        return {"id": name, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "used": True}

    def completed_run(self):
        run, path = self.new_run()
        run.update(status="completed", started_at="2026-09-29T10:00:00Z", ended_at="2026-09-29T10:01:00Z")
        run["executor"] = {"kind": "software", "name": "fixture"}
        run["input_artifacts"] = [self.artifact("input.txt")]
        run["outputs"] = [self.artifact("output.txt", "analysis result\n")]
        run["source_artifacts"] = []
        run["readiness"] = {
            "runtime": {"status": "ready", "evidence": ["Python available"]},
            "artifacts": {"status": "ready", "evidence": ["input.txt"]},
        }
        run["performed_steps"] = [{"step_id": s["step_id"], "status": "completed", "evidence": ["output.txt"]}
                                  for s in run["planned_steps"]]
        for gate in run["qc"]:
            gate.update(status="pass", evidence=["output.txt"])
        run["knowledge_writeback"] = {"status": "not_requested", "receipt": None}
        return run, path

    def check_run(self, run, expected=0):
        return self.call("validate-run", self.write_json("run.json", run), "--registry", self.registry_path, expected=expected)

    def assert_error(self, result, code):
        self.assertIn(code, [e["code"] for e in result["errors"]], result)

    def test_mixed_step_and_qc_registry_formats_are_supported(self):
        self.assertTrue(self.validate_registry(self.registry, expected=0)["ok"])

    def test_duplicate_workflow_ids_are_rejected(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"].append(copy.deepcopy(reg["workflows"][0]))
        reg["workflow_count"] = 2
        self.assert_error(self.validate_registry(reg), "duplicate_workflow_id")

    def test_missing_id_bad_version_count_and_status_are_rejected(self):
        cases = [("id", None, "invalid_workflow_id"), ("version", "latest", "invalid_version"),
                 ("status", "experimentally_validated", "invalid_status"),
                 ("canonical_status", "fully_validated", "invalid_canonical_status")]
        for field, value, code in cases:
            with self.subTest(field=field):
                reg = copy.deepcopy(self.registry)
                reg["workflows"][0][field] = value
                self.assert_error(self.validate_registry(reg), code)
        reg = copy.deepcopy(self.registry)
        reg["workflow_count"] = 99
        self.assert_error(self.validate_registry(reg), "workflow_count_mismatch")

    def test_dangling_dependency_ids_are_rejected_but_tools_are_not_ids(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0]["dependencies"].append("PB-WF-MISSING")
        self.assert_error(self.validate_registry(reg), "unknown_dependency")

    def test_known_historical_reference_is_reported_without_inventing_history(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0]["related_workflows"] = ["PB-WF-TEST@9.0.0"]
        result = self.validate_registry(reg, expected=0)
        self.assertIn("unverified_historical_version", [w["code"] for w in result["warnings"]])

    def test_workflow_dependencies_are_validated(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0]["workflow_dependencies"] = ["PB-WF-MISSING"]
        self.assert_error(self.validate_registry(reg), "unknown_dependency")

    def test_auto_generated_gate_ids_cannot_collide_with_explicit_ids(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0]["qc_gates"][1]["id"] = "qc-01"
        self.assert_error(self.validate_registry(reg), "duplicate_or_invalid_contract_id")

    def test_planned_wetlab_cannot_claim_software_experimental_validation(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0].update(domain="wet_lab", status="planned",
                                     validation={"level": "experimentally_validated", "basis": "software_tests"})
        self.assert_error(self.validate_registry(reg), "unsupported_experimental_validation")

    def test_new_run_pins_registry_and_plans_qc_without_claiming_readiness(self):
        run, _ = self.new_run()
        self.assertEqual(run["registry"]["version"], "2.0.0")
        self.assertEqual(run["registry"]["sha256"], hashlib.sha256(self.registry_path.read_bytes()).hexdigest())
        self.assertEqual(run["workflow_version"], "1.0.0-draft")
        self.assertEqual(run["status"], "planned")
        self.assertEqual(run["readiness"]["runtime"]["status"], "unknown")
        self.assertEqual(run["readiness"]["artifacts"]["status"], "unknown")
        self.assertEqual(len(run["qc"]), 2)
        self.assertTrue(all(g["status"] == "planned" for g in run["qc"]))
        self.assertTrue(self.check_run(run)["ok"])

    def test_new_run_rejects_unknown_workflow_and_never_overwrites(self):
        self.call("new-run", "--workflow", "PB-WF-NONE", "--task", "x", "--registry", self.registry_path,
                  "--output", self.root / "missing.json", expected=1)
        run, path = self.new_run()
        old = path.read_bytes()
        self.call("new-run", "--workflow", "PB-WF-TEST", "--task", "x", "--registry", self.registry_path,
                  "--output", path, expected=1)
        self.assertEqual(path.read_bytes(), old)

    def test_registry_tampering_invalidates_pinned_run(self):
        run, _ = self.new_run()
        self.registry["scope"] = "changed"
        self.write_json("registry.json", self.registry)
        self.assert_error(self.check_run(run, expected=1), "registry_hash_mismatch")

    def test_new_run_preserves_snapshot_and_survives_original_registry_upgrade(self):
        original = self.registry_path.read_bytes()
        run, path = self.new_run()
        expected_hash = hashlib.sha256(original).hexdigest()
        self.assertEqual(run["registry"]["path"], "registry-snapshots/" + expected_hash + ".json")
        snapshot = path.parent / run["registry"]["path"]
        self.assertEqual(snapshot.read_bytes(), original)
        self.registry["registry_version"] = "3.0.0"
        self.write_json("registry.json", self.registry)
        self.assertTrue(self.call("validate-run", path)["ok"])
        self.registry_path.unlink()
        self.assertTrue(self.call("validate-run", path)["ok"])

    def test_identical_snapshot_is_reused_without_rewriting(self):
        run, _ = self.new_run()
        snapshot = self.root / run["registry"]["path"]
        before = snapshot.stat()
        second = self.root / "second-run.json"
        self.call("new-run", "--workflow", "PB-WF-TEST", "--task", "Second fixture",
                  "--registry", self.registry_path, "--output", second)
        second_run = json.loads(second.read_text())
        self.assertEqual(second_run["registry"], run["registry"])
        self.assertEqual(snapshot.stat().st_ino, before.st_ino)
        self.assertEqual(snapshot.stat().st_mtime_ns, before.st_mtime_ns)

    def test_changed_snapshot_is_detected_and_never_overwritten(self):
        run, path = self.new_run()
        snapshot = self.root / run["registry"]["path"]
        changed = copy.deepcopy(self.registry)
        changed["scope"] = "tampered snapshot"
        tampered = json.dumps(changed).encode("utf-8")
        snapshot.write_bytes(tampered)
        self.assert_error(self.call("validate-run", path, expected=1), "registry_hash_mismatch")
        second = self.root / "second-run.json"
        self.call("new-run", "--workflow", "PB-WF-TEST", "--task", "Second fixture",
                  "--registry", self.registry_path, "--output", second, expected=1)
        self.assertFalse(second.exists())
        self.assertEqual(snapshot.read_bytes(), tampered)

    def test_incomplete_existing_snapshot_is_not_repaired_silently(self):
        original = self.registry_path.read_bytes()
        snapshot = self.root / "registry-snapshots" / (hashlib.sha256(original).hexdigest() + ".json")
        snapshot.parent.mkdir()
        snapshot.write_bytes(b'{"partial":')
        self.call("new-run", "--workflow", "PB-WF-TEST", "--task", "Fixture",
                  "--registry", self.registry_path, "--output", self.root / "run.json", expected=1)
        self.assertEqual(snapshot.read_bytes(), b'{"partial":')
        self.assertFalse((self.root / "run.json").exists())

    def test_atomic_creation_cleans_temporary_file_on_write_failure(self):
        output = self.root / "atomic.json"
        with mock.patch.object(CONTRACTS.os, "fsync", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                CONTRACTS.atomic_create_bytes(output, b'{"complete":true}\n')
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".atomic.json.*.tmp")), [])

    def test_atomic_creation_refuses_existing_destination_and_cleans_temp(self):
        output = self.root / "atomic.json"
        output.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            CONTRACTS.atomic_create_bytes(output, b"replacement")
        self.assertEqual(output.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".atomic.json.*.tmp")), [])

    def test_snapshot_creation_handles_competing_identical_creator(self):
        raw = self.registry_path.read_bytes()
        real_create = CONTRACTS.atomic_create_bytes
        def competing_creator(path, payload):
            real_create(path, payload)
            raise FileExistsError("concurrent identical creator won")
        with mock.patch.object(CONTRACTS, "atomic_create_bytes", side_effect=competing_creator):
            snapshot = CONTRACTS.preserve_registry_snapshot(raw, self.root)
        self.assertEqual(snapshot.read_bytes(), raw)

    def test_used_artifacts_require_hash_even_before_execution(self):
        run, _ = self.new_run()
        item = self.artifact("input.txt")
        del item["sha256"]
        run["input_artifacts"] = [item]
        self.assert_error(self.check_run(run, expected=1), "missing_artifact_hash")

    def test_completed_source_backed_run_requires_used_source_bytes(self):
        self.registry["workflows"][0]["provenance"] = [{"filename": "method.md", "source_key": "fixture-method"}]
        self.write_json("registry.json", self.registry)
        run, _ = self.completed_run()
        self.assert_error(self.check_run(run, expected=1), "missing_used_source")
        run["source_artifacts"] = [{"used": False, "reason": "Not retrieved"}]
        self.assert_error(self.check_run(run, expected=1), "missing_used_source")
        run["source_artifacts"] = [self.artifact("method.md", "Actual source method bytes\n")]
        self.assertTrue(self.check_run(run)["ok"])

    def test_completed_run_with_real_hashed_output_passes(self):
        run, _ = self.completed_run()
        self.assertTrue(self.check_run(run)["ok"])

    def test_completed_run_rejects_missing_steps_or_qc(self):
        for field, code in [("performed_steps", "missing_performed_step"), ("qc", "missing_qc_gate")]:
            with self.subTest(field=field):
                run, path = self.completed_run()
                run[field] = []
                self.assert_error(self.check_run(run, expected=1), code)
                path.unlink()

    def test_qc_unknown_blocked_and_evidenceless_pass_do_not_complete(self):
        for state, evidence in [("unknown", ["result"]), ("blocked", ["result"]), ("pass", [])]:
            with self.subTest(state=state):
                run, path = self.completed_run()
                run["qc"][0].update(status=state, evidence=evidence)
                self.assert_error(self.check_run(run, expected=1), "qc_not_resolved")
                path.unlink()

    def test_qc_definition_cannot_be_removed_or_downgraded(self):
        run, _ = self.completed_run()
        run["qc"][0]["required"] = False
        run["qc"][0]["status"] = "blocked"
        result = self.check_run(run, expected=1)
        self.assert_error(result, "qc_contract_mismatch")
        self.assert_error(result, "qc_not_resolved")

    def test_qc_not_applicable_requires_reason_and_evidence(self):
        run, path = self.completed_run()
        run["qc"][0].update(status="not_applicable", justification="", evidence=[])
        self.assert_error(self.check_run(run, expected=1), "qc_not_resolved")
        run["qc"][0].update(justification="No image transformations in this run", evidence=["input.txt"])
        self.assertTrue(self.check_run(run)["ok"])

    def test_completed_run_rejects_output_tampering_and_absence(self):
        run, _ = self.completed_run()
        Path(run["outputs"][0]["path"]).write_text("tampered")
        self.assert_error(self.check_run(run, expected=1), "artifact_hash_mismatch")
        Path(run["outputs"][0]["path"]).unlink()
        self.assert_error(self.check_run(run, expected=1), "artifact_unavailable")

    def test_completed_run_rejects_missing_timestamps_and_readiness(self):
        run, _ = self.completed_run()
        run["ended_at"] = None
        run["readiness"]["runtime"]["status"] = "unknown"
        result = self.check_run(run, expected=1)
        self.assert_error(result, "invalid_timestamp")
        self.assert_error(result, "runtime_not_ready")

    def test_writeback_claim_requires_hashed_receipt(self):
        run, _ = self.completed_run()
        run["knowledge_writeback"] = {"status": "completed", "receipt": None}
        self.assert_error(self.check_run(run, expected=1), "missing_writeback_receipt")
        receipt = self.artifact("receipt.json", '{"destination":"fixture-store","record_id":"SK-EV-test"}')
        receipt.update(destination="fixture-store", record_id="SK-EV-test")
        run["knowledge_writeback"]["receipt"] = receipt
        self.assertTrue(self.check_run(run)["ok"])

    def test_unrelated_receipt_bytes_cannot_substitute_for_a_writeback_receipt(self):
        run, _ = self.completed_run()
        receipt = self.artifact("unrelated.json", '{"summary":"planning only"}')
        receipt.update(destination="fixture-store", record_id="SK-EV-test")
        run["knowledge_writeback"] = {"status": "completed", "receipt": receipt}
        self.assert_error(self.check_run(run, expected=1), "writeback_receipt_mismatch")

    def test_software_executor_cannot_mark_physical_wetlab_steps_completed(self):
        self.registry["workflows"][0]["domain"] = "wet_lab"
        self.write_json("registry.json", self.registry)
        run, _ = self.completed_run()
        self.assert_error(self.check_run(run, expected=1), "physical_execution_not_established")

    def test_explicit_physical_classification_is_independent_of_domain(self):
        self.registry["workflows"][0].update(domain="imaging", execution_kind="wet_lab")
        self.write_json("registry.json", self.registry)
        run, _ = self.completed_run()
        self.assertEqual(run["execution_kind"], "wet_lab")
        self.assert_error(self.check_run(run, expected=1), "physical_execution_not_established")
        run["executor"] = {"kind": "custom_agent", "name": "unrecognized actor"}
        self.assert_error(self.check_run(run, expected=1), "physical_execution_not_established")
        run["executor"] = {"kind": "human", "name": "fixture operator"}
        self.assertTrue(self.check_run(run)["ok"])

    def test_execution_classification_is_pinned_and_cannot_be_relabeled(self):
        self.registry["workflows"][0]["execution_kind"] = "wet_lab"
        self.write_json("registry.json", self.registry)
        run, _ = self.new_run()
        run["execution_kind"] = "digital"
        self.assert_error(self.check_run(run, expected=1), "execution_kind_mismatch")

    def test_legacy_snapshot_remains_valid_with_explicit_limitation(self):
        run, _ = self.completed_run()
        self.assertNotIn("execution_kind", run)
        report = self.check_run(run)
        self.assertIn("legacy_execution_kind_unspecified", [w["code"] for w in report["warnings"]])

    def test_registry_rejects_invalid_or_conflicting_execution_kinds(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0]["execution_kind"] = "automatic"
        self.assert_error(self.validate_registry(reg), "invalid_execution_kind")
        reg["workflows"][0].update(domain="wet_lab", execution_kind="digital")
        self.assert_error(self.validate_registry(reg), "conflicting_execution_kind")
        reg["workflows"][0].update(domain="analysis")
        reg["workflows"][0]["steps"][1]["execution_kind"] = "wet_lab"
        self.assert_error(self.validate_registry(reg), "conflicting_step_execution_kind")

    def mixed_registry(self):
        self.registry["workflows"][0].update(execution_kind="mixed", steps=[
            {"instruction": "Acquire observation", "execution_kind": "wet_lab"},
            {"instruction": "Calculate result", "execution_kind": "digital"},
        ])
        self.write_json("registry.json", self.registry)

    def test_mixed_registry_requires_pinned_physical_and_digital_steps(self):
        reg = copy.deepcopy(self.registry)
        reg["workflows"][0]["execution_kind"] = "mixed"
        self.assert_error(self.validate_registry(reg), "missing_mixed_step_kind")
        reg["workflows"][0]["steps"] = [
            {"instruction": "Analysis one", "execution_kind": "digital"},
            {"instruction": "Analysis two", "execution_kind": "digital"},
        ]
        self.assert_error(self.validate_registry(reg), "invalid_mixed_workflow")

    def test_mixed_plan_is_valid_without_claiming_physical_execution(self):
        self.mixed_registry()
        run, _ = self.new_run()
        self.assertEqual(run["planned_steps"][0]["execution_kind"], "wet_lab")
        self.assertTrue(self.check_run(run)["ok"])

    def test_mixed_completion_requires_step_specific_physical_executor(self):
        self.mixed_registry()
        run, _ = self.completed_run()
        for step, plan in zip(run["performed_steps"], run["planned_steps"]):
            step["execution_kind"] = plan["execution_kind"]
        self.assert_error(self.check_run(run, expected=1), "physical_step_executor_missing")
        run["performed_steps"][0]["executor"] = {"kind": "agent", "name": "orchestrator"}
        self.assert_error(self.check_run(run, expected=1), "physical_step_executor_missing")
        run["performed_steps"][0]["executor"] = {"kind": "instrument", "name": "fixture instrument"}
        self.assertTrue(self.check_run(run)["ok"])

    def test_mixed_physical_step_cannot_be_relabeled_digital(self):
        self.mixed_registry()
        run, _ = self.completed_run()
        for step in run["performed_steps"]:
            step["execution_kind"] = "digital"
        self.assert_error(self.check_run(run, expected=1), "step_execution_kind_mismatch")

    def test_all_physical_steps_not_applicable_cannot_complete_mixed_workflow(self):
        self.mixed_registry()
        run, _ = self.completed_run()
        for step, plan in zip(run["performed_steps"], run["planned_steps"]):
            step["execution_kind"] = plan["execution_kind"]
        run["performed_steps"][0].update(status="not_applicable", justification="Design-only fixture")
        self.assert_error(self.check_run(run, expected=1), "physical_execution_not_established")

    def test_mixed_physical_step_needs_observation_evidence(self):
        self.mixed_registry()
        run, _ = self.completed_run()
        for step, plan in zip(run["performed_steps"], run["planned_steps"]):
            step["execution_kind"] = plan["execution_kind"]
        run["performed_steps"][0].update(executor={"kind":"human", "name":"fixture operator"}, evidence=[])
        self.assert_error(self.check_run(run, expected=1), "step_not_resolved")

    def test_software_run_cannot_assert_experimental_validation(self):
        run, _ = self.completed_run()
        run["validation"] = {"level": "experimentally_validated", "basis": "software_tests"}
        self.assert_error(self.check_run(run, expected=1), "unsupported_experimental_validation")

    def test_forbidden_evidence_upgrades_are_blocked(self):
        pairs = [("proximity_labeling", "direct_binding"), ("peak_interval", "binding_intensity"),
                 ("occupancy", "transcriptional_activation"), ("occupancy", "causal_regulation"),
                 ("reporter_domain", "editing_efficiency"), ("catalogue_absence", "biological_absence")]
        for ev, claim in pairs:
            with self.subTest(evidence=ev, claim=claim):
                result = self.call("check-evidence", self.write_json("evidence.json", {"evidence_type": ev, "claim_type": claim}), expected=1)
                self.assertEqual(result["status"], "blocked")

    def test_evidence_combination_does_not_cancel_invalid_inference(self):
        obj = {"evidence_type": ["proximity_labeling", "unknown_assay"], "claim_type": "direct_binding"}
        result = self.call("check-evidence", self.write_json("evidence.json", obj), expected=1)
        self.assertEqual(result["status"], "blocked")

    def test_design_and_response_evidence_cannot_be_upgraded_to_mechanism(self):
        pairs = [("dna_language_model_score", claim) for claim in
                 ("promoter_activity", "cell_type_specificity", "stress_inducibility")]
        pairs += [("guide_design", "target_repression"), ("guide_design", "target_site_editing"),
                  ("baseline_rescue", "stress_response_restoration"),
                  ("phosphosite_reporter", "kinase_specific_activity")]
        for evidence, claim in pairs:
            with self.subTest(evidence=evidence, claim=claim):
                result = self.call("check-evidence", self.write_json("evidence.json", {
                    "evidence_type": evidence, "claim_type": claim}), expected=1)
                self.assertEqual(result["status"], "blocked")

    def test_design_and_response_evidence_has_narrow_descriptive_scope(self):
        pairs = [("dna_language_model_score", "sequence_model_score"),
                 ("guide_design", "guide_candidate"), ("baseline_rescue", "basal_rescue"),
                 ("phosphosite_reporter", "phosphosite_variant_response")]
        for evidence, claim in pairs:
            with self.subTest(evidence=evidence, claim=claim):
                result = self.call("check-evidence", self.write_json("evidence.json", {
                    "evidence_type": evidence, "claim_type": claim}))
                self.assertEqual(result["status"], "within_scope")
                self.assertFalse(result["scientific_truth_verified"])
        result = self.call("check-evidence", self.write_json("evidence.json", {
            "evidence_type": "guide_design", "claim_type": "unlisted_inference"}), expected=2)
        self.assertEqual(result["status"], "requires_review")

    def test_unknown_evidence_or_claim_requires_review(self):
        for obj in [{"evidence_type": "unknown_assay", "claim_type": "direct_binding"},
                    {"evidence_type": "proximity_labeling", "claim_type": "unknown_claim"}]:
            result = self.call("check-evidence", self.write_json("evidence.json", obj), expected=2)
            self.assertEqual(result["status"], "requires_review")

    def test_conservative_claim_is_within_scope_without_certifying_truth(self):
        result = self.call("check-evidence", self.write_json("evidence.json", {
            "evidence_type": "proximity_labeling", "claim_type": "proximity_candidate"}))
        self.assertEqual(result["status"], "within_scope")
        self.assertFalse(result["scientific_truth_verified"])

    def test_malformed_json_is_clean_failure(self):
        p = self.root / "bad.json"
        p.write_text("{")
        self.assert_error(self.call("validate-registry", p, expected=1), "input_error")


if __name__ == "__main__":
    unittest.main()
