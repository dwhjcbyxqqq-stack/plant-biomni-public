#!/usr/bin/env python3
"""Validate Plant Biomni metadata and narrow evidence-inference permissions.

Standard library only. This module does not execute workflows, instruments,
experiments or knowledge-store writes, and never certifies scientific truth.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "references" / "workflow-registry.json"
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
WORKFLOW_ID = re.compile(r"^PB-WF-[A-Za-z0-9][A-Za-z0-9-]*$")
REFERENCE = re.compile(r"\b(PB-WF-[A-Za-z0-9][A-Za-z0-9-]*)(?:@([0-9A-Za-z.+-]+))?")
HASH = re.compile(r"^[0-9a-fA-F]{64}$")
WORKFLOW_STATUSES = {"source_backed_partial", "source_backed_protocol", "runnable_analysis_unvalidated", "established_external", "planned", "retired"}
CANONICAL_STATUSES = {"designated_source", "current", "candidate", "superseded"}
RUN_STATUSES = {"planned", "blocked", "running", "partial", "failed", "completed"}
EXECUTION_KINDS = {"wet_lab", "digital", "planning", "mixed"}
STEP_EXECUTION_KINDS = EXECUTION_KINDS - {"mixed"}
PHYSICAL_EXECUTORS = {"human", "instrument"}
SCOPE = "Metadata integrity and listed anti-overclaim rules only; not scientific truth, experimental validation, or execution authorization."


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_create_bytes(path, payload):
    """Publish complete bytes exclusively, leaving no partial destination on failure.

    A same-directory hard link is atomic and refuses an existing destination.
    Unsupported filesystems fail closed rather than falling back to overwrite.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            if stream.write(payload) != len(payload):
                raise OSError("Incomplete write while preserving run artifact.")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def preserve_registry_snapshot(raw, run_directory):
    """Preserve exactly the bytes parsed for this run; never repair a changed pin."""
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = Path(run_directory) / "registry-snapshots" / (digest + ".json")
    if not snapshot.exists():
        try:
            atomic_create_bytes(snapshot, raw)
        except FileExistsError:
            # A concurrent creator may have won; only identical bytes are reusable.
            pass
    if snapshot.read_bytes() != raw:
        raise ValueError("Existing content-addressed registry snapshot differs from its expected bytes; refusing to overwrite it.")
    return snapshot


def issue(items, code, path, message):
    items.append({"code": code, "path": path, "message": message})


def report(errors, warnings=None, **extra):
    return {"ok": not errors, "status": "invalid" if errors else "valid", "errors": errors,
            "warnings": warnings or [], "scope": SCOPE, "scientific_truth_verified": False, **extra}


def normalized_steps(workflow):
    return [{"step_id": item.get("id", item.get("step_id", f"step-{i:02d}")) if isinstance(item, dict) else f"step-{i:02d}",
             "instruction": item.get("instruction", item.get("description", "")) if isinstance(item, dict) else item,
             **({"execution_kind": item["execution_kind"]} if isinstance(item, dict) and "execution_kind" in item else {})}
            for i, item in enumerate(workflow["steps"], 1)]


def normalized_qc(workflow):
    return [{"gate_id": item.get("id", item.get("gate_id", f"qc-{i:02d}")) if isinstance(item, dict) else f"qc-{i:02d}",
             "requirement": item.get("requirement", item.get("description", "")) if isinstance(item, dict) else item,
             "required": item.get("required", True) if isinstance(item, dict) else True}
            for i, item in enumerate(workflow["qc_gates"], 1)]


def has_experimental_claim(record):
    validation = record.get("validation", {})
    return (record.get("experimentally_validated") is True or record.get("biologically_validated") is True
            or (isinstance(validation, dict) and validation.get("level") == "experimentally_validated"))


def validate_registry(registry):
    """Return metadata errors, tolerating legacy string/dict steps and QC gates."""
    errors, warnings = [], []
    if not isinstance(registry, dict):
        issue(errors, "invalid_registry", "$", "Registry must be an object.")
        return report(errors)
    for field in ("schema_version", "registry_version"):
        if not isinstance(registry.get(field), str) or not VERSION.fullmatch(registry[field]):
            issue(errors, "invalid_version", field, "Expected semantic version, optionally with a prerelease suffix.")
    workflows = registry.get("workflows")
    if not isinstance(workflows, list):
        issue(errors, "invalid_workflows", "workflows", "Expected an array.")
        return report(errors)
    if type(registry.get("workflow_count")) is not int or registry["workflow_count"] != len(workflows):
        issue(errors, "workflow_count_mismatch", "workflow_count", "Count must equal the workflows array length.")
    by_id = {}
    for i, workflow in enumerate(workflows):
        prefix = f"workflows[{i}]"
        if not isinstance(workflow, dict):
            issue(errors, "invalid_workflow", prefix, "Workflow must be an object.")
            continue
        identity = workflow.get("id")
        if not isinstance(identity, str) or not WORKFLOW_ID.fullmatch(identity):
            issue(errors, "invalid_workflow_id", prefix + ".id", "Expected a nonempty PB-WF identifier.")
        elif identity in by_id:
            issue(errors, "duplicate_workflow_id", prefix + ".id", identity)
        else:
            by_id[identity] = workflow
        version = workflow.get("version")
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            issue(errors, "invalid_version", prefix + ".version", "Expected a semantic version.")
        for field in ("name", "domain"):
            if not nonempty(workflow.get(field)):
                issue(errors, "missing_workflow_field", prefix + "." + field, "Nonempty string required.")
        if workflow.get("status") not in WORKFLOW_STATUSES:
            issue(errors, "invalid_status", prefix + ".status", "Unknown workflow status.")
        if workflow.get("canonical_status") not in CANONICAL_STATUSES:
            issue(errors, "invalid_canonical_status", prefix + ".canonical_status", "Unknown canonical status.")
        execution_kind = workflow.get("execution_kind")
        if "execution_kind" not in workflow:
            issue(warnings, "legacy_execution_kind_unspecified", prefix + ".execution_kind", "Historical workflow has no explicit execution classification; do not infer physical execution permission from its domain.")
        elif execution_kind not in EXECUTION_KINDS:
            issue(errors, "invalid_execution_kind", prefix + ".execution_kind", "Use wet_lab, digital, planning or mixed.")
        if workflow.get("domain") == "wet_lab" and execution_kind in {"digital", "planning"}:
            issue(errors, "conflicting_execution_kind", prefix + ".execution_kind", "A wet_lab-domain protocol cannot declare digital or planning execution; represent a planning-only workflow separately.")
        for field in ("provenance", "inputs", "outputs", "steps", "dependencies", "qc_gates", "unresolved"):
            if not isinstance(workflow.get(field), list):
                issue(errors, "invalid_workflow_field", prefix + "." + field, "Array required.")
        for field, text_keys in (("steps", ("instruction", "description")), ("qc_gates", ("requirement", "description"))):
            values = workflow.get(field)
            if not isinstance(values, list):
                continue
            if not values:
                issue(errors, "empty_workflow_contract", prefix + "." + field, "At least one entry is required.")
            seen = set()
            for n, item in enumerate(values, 1):
                text = item if isinstance(item, str) else next((item[k] for k in text_keys if k in item), None) if isinstance(item, dict) else None
                if not nonempty(text):
                    issue(errors, "invalid_contract_item", f"{prefix}.{field}[{n-1}]", "Nonempty instruction/requirement required.")
                auto_id = f"{'step' if field == 'steps' else 'qc'}-{n:02d}"
                identity = item.get("id", item.get("step_id" if field == "steps" else "gate_id", auto_id)) if isinstance(item, dict) else auto_id
                if not nonempty(identity) or identity in seen:
                    issue(errors, "duplicate_or_invalid_contract_id", f"{prefix}.{field}[{n-1}]", "Explicit and generated IDs must be unique nonempty strings.")
                if isinstance(identity, str):
                    seen.add(identity)
                if isinstance(item, dict):
                    if "required" in item and type(item["required"]) is not bool:
                        issue(errors, "invalid_required_flag", f"{prefix}.{field}[{n-1}].required", "Boolean required.")
                if field == "steps":
                    step_kind = item.get("execution_kind") if isinstance(item, dict) else None
                    if isinstance(item, dict) and "execution_kind" in item and step_kind not in STEP_EXECUTION_KINDS:
                        issue(errors, "invalid_step_execution_kind", f"{prefix}.{field}[{n-1}].execution_kind", "Use wet_lab, digital or planning for a step.")
                    if execution_kind == "mixed" and step_kind not in STEP_EXECUTION_KINDS:
                        issue(errors, "missing_mixed_step_kind", f"{prefix}.{field}[{n-1}]", "Every mixed-workflow step must pin its execution_kind in the registry.")
                    if step_kind == "wet_lab" and execution_kind in {"digital", "planning"}:
                        issue(errors, "conflicting_step_execution_kind", f"{prefix}.{field}[{n-1}]", "A physical step requires wet_lab or mixed workflow execution.")
        if execution_kind == "mixed" and isinstance(workflow.get("steps"), list):
            step_kinds = {s.get("execution_kind") for s in workflow["steps"] if isinstance(s, dict) and isinstance(s.get("execution_kind"), str)}
            if "wet_lab" not in step_kinds or not step_kinds & {"digital", "planning"}:
                issue(errors, "invalid_mixed_workflow", prefix + ".steps", "Mixed execution requires both a physical step and a digital/planning step.")
        validation = workflow.get("validation", {})
        software_basis = isinstance(validation, dict) and validation.get("basis") in ("software", "software_tests", "json_validation")
        if has_experimental_claim(workflow) and (workflow.get("status") == "planned" or workflow.get("canonical_status") == "candidate" or software_basis):
            issue(errors, "unsupported_experimental_validation", prefix, "Planned/candidate methods or software tests cannot establish experimental validation.")
    for i, workflow in enumerate(workflows):
        if not isinstance(workflow, dict):
            continue
        for field in ("dependencies", "workflow_dependencies", "related_workflows"):
            values = workflow.get(field, [])
            if not isinstance(values, list):
                issue(errors, "invalid_dependencies", f"workflows[{i}].{field}", "Array required.")
                continue
            for n, item in enumerate(values):
                text = item if isinstance(item, str) else item.get("workflow_id", item.get("id", "")) if isinstance(item, dict) else ""
                if not isinstance(text, str):
                    text = ""
                refs = list(REFERENCE.finditer(text))
                if field in ("workflow_dependencies", "related_workflows") and not refs:
                    issue(errors, "invalid_dependency_reference", f"workflows[{i}].{field}[{n}]", "Expected a PB-WF reference.")
                for match in refs:
                    target, version = match.groups()
                    at = f"workflows[{i}].{field}[{n}]"
                    if target not in by_id:
                        issue(errors, "unknown_dependency", at, target)
                    elif version and version != by_id[target].get("version"):
                        issue(warnings, "unverified_historical_version", at, f"{target}@{version} is historical or unavailable; resolve against workflow-history.json before reuse.")
    return report(errors, warnings, workflow_count=len(workflows), registry_version=registry.get("registry_version"))


def new_run(registry, registry_path, workflow_id, task):
    """Build a plan only. Readiness and execution evidence remain unresolved."""
    workflow = next((w for w in registry["workflows"] if w["id"] == workflow_id), None)
    if workflow is None:
        raise ValueError(f"Unknown workflow: {workflow_id}")
    if not nonempty(task):
        raise ValueError("Task must be a nonempty string.")
    sources = []
    for n, source in enumerate(workflow["provenance"], 1):
        sources.append({"id": f"source-{n:02d}", "locator": source, "used": False, "path": None, "sha256": None,
                        "reason": "Reference pointer only; retrieve and hash actual bytes before marking used."})
    run = {
        "schema_version": "1.0.0", "run_id": "PB-RUN-" + str(uuid.uuid4()),
        "workflow_id": workflow["id"], "workflow_version": workflow["version"],
        "registry": {"path": str(Path(registry_path).resolve()), "version": registry["registry_version"], "sha256": sha256(registry_path)},
        "workflow_status": workflow["status"], "canonical_status": workflow["canonical_status"],
        "task": task, "project_id": None, "status": "planned",
        "created_at": datetime.now(timezone.utc).isoformat(), "started_at": None, "ended_at": None,
        "executor": {"kind": "agent", "name": None},
        "source_artifacts": sources, "input_artifacts": [], "parameters": {}, "units": {}, "biological_context": {},
        "software_instrument_versions": [], "controls": [], "planned_steps": normalized_steps(workflow),
        "performed_steps": [], "planned_outputs": workflow["outputs"], "outputs": [],
        "qc": [{**gate, "status": "planned", "evidence": [], "justification": None} for gate in normalized_qc(workflow)],
        "readiness": {"runtime": {"status": "unknown", "reason": "Check actual tools/instruments and versions.", "evidence": []},
                      "artifacts": {"status": "unknown", "reason": "Retrieve inputs and used sources; verify identities and hashes.", "evidence": []}},
        "deviations": [], "exclusions": [], "limitations": list(workflow["unresolved"]), "evidence_links": [],
        "knowledge_writeback": {"status": "pending", "receipt": None},
        "validation": {"level": "unvalidated", "basis": None},
    }
    if "execution_kind" in workflow:
        run["execution_kind"] = workflow["execution_kind"]
    return run


def timestamp(value, errors, field, required=False):
    if value is None and not required:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except ValueError:
        issue(errors, "invalid_timestamp", field, "Timezone-aware ISO 8601 timestamp required.")
        return None


def check_artifact(item, base_dir, errors, field, required=False):
    if not isinstance(item, dict):
        issue(errors, "invalid_artifact", field, "Artifact must be an object.")
        return
    if "used" in item and type(item["used"]) is not bool:
        issue(errors, "invalid_used_flag", field + ".used", "Boolean required.")
    if not required and item.get("used", True) is False:
        if not nonempty(item.get("reason")):
            issue(errors, "missing_unused_reason", field, "Unused source/input requires an explicit reason.")
        return
    digest = item.get("sha256")
    valid_hash = isinstance(digest, str) and bool(HASH.fullmatch(digest))
    if not valid_hash:
        issue(errors, "missing_artifact_hash", field + ".sha256", "Used artifacts and outputs require SHA256 of the actual bytes.")
    raw_path = item.get("path")
    if not nonempty(raw_path):
        issue(errors, "artifact_unavailable", field + ".path", "A readable local path is required; retrieve remote bytes first.")
        return
    path = Path(raw_path)
    if not path.is_absolute():
        path = base_dir / path
    try:
        actual = sha256(path)
    except OSError as exc:
        issue(errors, "artifact_unavailable", field + ".path", str(exc))
        return
    if valid_hash and actual != digest.lower():
        issue(errors, "artifact_hash_mismatch", field + ".sha256", "File bytes differ from the pinned SHA256.")


def resolved_check(record):
    evidence = record.get("evidence")
    has_evidence = isinstance(evidence, list) and bool(evidence) and all(nonempty(e) or isinstance(e, dict) and bool(e) for e in evidence)
    return bool(has_evidence and (record.get("status") in ("pass", "completed", "ready") or
                                 record.get("status") == "not_applicable" and nonempty(record.get("justification"))))


def validate_run(run, registry, registry_path, base_dir):
    """Verify declarations, pinned local bytes and completion evidence references."""
    reg_result = validate_registry(registry)
    errors, warnings = list(reg_result["errors"]), list(reg_result["warnings"])
    if errors:
        return report(errors, warnings)
    if not isinstance(run, dict):
        issue(errors, "invalid_run", "$", "Run must be an object.")
        return report(errors)
    if not isinstance(run.get("run_id"), str) or not run["run_id"].startswith("PB-RUN-"):
        issue(errors, "invalid_run_id", "run_id", "Expected a PB-RUN identifier.")
    if run.get("schema_version") != "1.0.0":
        issue(errors, "unsupported_run_schema", "schema_version", "Supported schema is 1.0.0.")
    if run.get("status") not in RUN_STATUSES:
        issue(errors, "invalid_run_status", "status", "Unknown run status.")
    if not nonempty(run.get("task")):
        issue(errors, "missing_task", "task", "Nonempty task required.")
    pin = run.get("registry")
    if not isinstance(pin, dict):
        pin = {}
    if pin.get("sha256") != sha256(registry_path):
        issue(errors, "registry_hash_mismatch", "registry.sha256", "Use the exact registry snapshot pinned by this run.")
    if pin.get("version") != registry["registry_version"]:
        issue(errors, "registry_version_mismatch", "registry.version", "Registry version differs from pin.")
    workflow = next((w for w in registry["workflows"] if w["id"] == run.get("workflow_id")), None)
    if workflow is None:
        issue(errors, "unknown_workflow", "workflow_id", "Workflow absent from pinned registry.")
        return report(errors, warnings)
    if run.get("workflow_version") != workflow["version"]:
        issue(errors, "workflow_version_mismatch", "workflow_version", "Workflow version differs from pin.")
    for field in ("workflow_status", "canonical_status"):
        expected = workflow["status" if field == "workflow_status" else field]
        if run.get(field) != expected:
            issue(errors, "workflow_state_mismatch", field, "Workflow state differs from registry.")
    if "execution_kind" in workflow and run.get("execution_kind") != workflow["execution_kind"]:
        issue(errors, "execution_kind_mismatch", "execution_kind", "Execution kind must match the pinned registry; preserve historical snapshots when validating old runs.")
    execution_kind = workflow.get("execution_kind")
    done = run.get("status") == "completed"
    timestamp(run.get("created_at"), errors, "created_at", True)
    start = timestamp(run.get("started_at"), errors, "started_at", done or run.get("status") == "running")
    end = timestamp(run.get("ended_at"), errors, "ended_at", done)
    if start and end and end < start:
        issue(errors, "invalid_time_order", "ended_at", "End precedes start.")
    executor = run.get("executor")
    if not isinstance(executor, dict):
        executor = {}
    if done and (not nonempty(executor.get("name")) or not nonempty(executor.get("kind"))):
        issue(errors, "missing_executor", "executor", "Completed run requires actual executor name and kind.")
    if done and (workflow["domain"] == "wet_lab" or execution_kind == "wet_lab") and execution_kind != "mixed" and executor.get("kind") not in PHYSICAL_EXECUTORS:
        issue(errors, "physical_execution_not_established", "executor", "A software/agent plan cannot mark physical wet-lab execution completed; record the actual human/instrument executor and evidence.")
    validation = run.get("validation", {})
    software_basis = isinstance(validation, dict) and validation.get("basis") in ("software", "software_tests", "json_validation")
    if has_experimental_claim(run) and (workflow["status"] == "planned" or workflow["canonical_status"] == "candidate" or executor.get("kind") in ("agent", "software") or software_basis):
        issue(errors, "unsupported_experimental_validation", "validation", "Software validation and candidate/planned run metadata cannot establish experimental validation.")
    for field in ("source_artifacts", "input_artifacts", "outputs"):
        items = run.get(field)
        if not isinstance(items, list):
            issue(errors, "invalid_artifacts", field, "Array required.")
            continue
        if done and field == "outputs" and not items:
            issue(errors, "missing_outputs", field, "Completed run requires at least one real output.")
        if done and field == "input_artifacts" and workflow["inputs"] and not any(isinstance(i, dict) and i.get("used", True) is not False for i in items):
            issue(errors, "missing_used_input", field, "Record at least one used input or a hashed input manifest for this workflow.")
        if done and field == "source_artifacts" and workflow["provenance"] and not any(isinstance(i, dict) and i.get("used", True) is not False for i in items):
            issue(errors, "missing_used_source", field, "Source-backed workflow completion requires at least one used source with actual bytes and SHA256.")
        for i, item in enumerate(items):
            check_artifact(item, base_dir, errors, f"{field}[{i}]", required=field == "outputs")
    if run.get("planned_steps") != normalized_steps(workflow):
        issue(errors, "planned_steps_mismatch", "planned_steps", "Plan must preserve the pinned workflow's steps; record deviations separately.")
    planned_steps = {s["step_id"]: s for s in normalized_steps(workflow)}
    gates = {g["gate_id"]: g for g in normalized_qc(workflow)}
    for field, identity_key, expected in (("performed_steps", "step_id", planned_steps), ("qc", "gate_id", gates)):
        records = run.get(field)
        if not isinstance(records, list):
            issue(errors, "invalid_run_records", field, "Array required.")
            records = []
        seen = set()
        observed_physical_steps = 0
        for i, record in enumerate(records):
            at = f"{field}[{i}]"
            if not isinstance(record, dict):
                issue(errors, "invalid_run_record", at, "Object required.")
                continue
            identity = record.get(identity_key)
            if not isinstance(identity, str) or identity not in expected:
                issue(errors, "unknown_contract_reference", at, "Reference absent from pinned workflow.")
                continue
            if identity in seen:
                issue(errors, "duplicate_contract_record", at, identity)
            seen.add(identity)
            if field == "qc" and (record.get("requirement") != expected[identity]["requirement"] or record.get("required") != expected[identity]["required"]):
                issue(errors, "qc_contract_mismatch", at, "QC definition must match the pinned registry.")
            required = field == "performed_steps" or expected[identity]["required"]
            valid_statuses = {"completed", "not_applicable"} if field == "performed_steps" else {"pass", "not_applicable"}
            if done and required and (record.get("status") not in valid_statuses or not resolved_check(record)):
                issue(errors, "qc_not_resolved" if field == "qc" else "step_not_resolved", at, "Required completion needs evidence plus pass/completed, or justified not_applicable.")
            if field == "performed_steps" and execution_kind == "mixed":
                expected_kind = expected[identity].get("execution_kind")
                if record.get("execution_kind") != expected_kind:
                    issue(errors, "step_execution_kind_mismatch", at + ".execution_kind", "Performed step kind must match its physical/digital/planning classification in the pinned registry.")
                if done and expected_kind == "wet_lab" and record.get("status") == "completed":
                    actual_executor = record.get("executor", {})
                    if not isinstance(actual_executor, dict) or actual_executor.get("kind") not in PHYSICAL_EXECUTORS or not nonempty(actual_executor.get("name")):
                        issue(errors, "physical_step_executor_missing", at + ".executor", "Completed physical step requires its named human/instrument executor; an agent may coordinate but cannot supply the physical observation.")
                    elif resolved_check(record):
                        observed_physical_steps += 1
        for identity, contract in expected.items():
            if identity not in seen and (done or field == "qc"):
                if field == "qc" and not contract["required"]:
                    continue
                issue(errors, "missing_qc_gate" if field == "qc" else "missing_performed_step", field, identity)
        if done and field == "performed_steps" and execution_kind == "mixed" and not observed_physical_steps:
            issue(errors, "physical_execution_not_established", field, "Mixed-workflow completion requires at least one completed, evidenced physical step performed by a named human/instrument; a design-only run uses a planning workflow.")
    readiness = run.get("readiness")
    if not isinstance(readiness, dict):
        readiness = {}
    for field in ("runtime", "artifacts"):
        entry = readiness.get(field)
        if not isinstance(entry, dict) or entry.get("status") not in {"unknown", "blocked", "ready", "not_applicable"}:
            issue(errors, "invalid_readiness", "readiness." + field, "Explicit readiness status required.")
        elif done and not resolved_check(entry):
            issue(errors, field + "_not_ready", "readiness." + field, "Completion requires evidence of readiness or justified non-applicability.")
        elif entry.get("status") in {"unknown", "blocked"}:
            issue(warnings, field + "_unresolved", "readiness." + field, "Plan validation does not make execution ready.")
    writeback = run.get("knowledge_writeback")
    if not isinstance(writeback, dict) or writeback.get("status") not in {"pending", "not_requested", "blocked", "completed"}:
        issue(errors, "invalid_writeback_state", "knowledge_writeback", "Explicit writeback state required.")
    elif writeback["status"] == "completed":
        receipt = writeback.get("receipt")
        if not isinstance(receipt, dict) or not nonempty(receipt.get("destination")) or not nonempty(receipt.get("record_id")):
            issue(errors, "missing_writeback_receipt", "knowledge_writeback.receipt", "Actual receipt requires destination, record_id, local path and hash.")
        else:
            check_artifact(receipt, base_dir, errors, "knowledge_writeback.receipt", required=True)
            try:
                receipt_path = Path(receipt.get("path", ""))
                if not receipt_path.is_absolute():
                    receipt_path = base_dir / receipt_path
                body = load_json(receipt_path)
                if not isinstance(body, dict) or any(body.get(k) != receipt[k] for k in ("destination", "record_id")):
                    issue(errors, "writeback_receipt_mismatch", "knowledge_writeback.receipt", "Receipt bytes must contain the same destination and record_id as this run.")
            except (OSError, ValueError, TypeError):
                issue(errors, "writeback_receipt_mismatch", "knowledge_writeback.receipt", "Readable JSON receipt with destination and record_id required.")
    return report(errors, warnings, run_id=run.get("run_id"), run_status=run.get("status"))


EVIDENCE_ALIASES = {
    "turboid": "proximity_labeling", "proximity": "proximity_labeling", "proximity_labelling": "proximity_labeling",
    "chip_peak_interval": "peak_interval", "peak_calls": "peak_interval", "chip_peak_calls": "peak_interval",
    "chip_occupancy": "occupancy", "chip_binding": "occupancy", "chip_seq": "occupancy",
    "promoter_reporter": "reporter_domain", "reporter_expression_domain": "reporter_domain",
    "catalog_absence": "catalogue_absence", "catalogue_search_absence": "catalogue_absence",
}
CLAIM_ALIASES = {"direct_interaction": "direct_binding", "intensity": "binding_intensity", "activation": "transcriptional_activation",
                 "causality": "causal_regulation", "editing": "target_site_editing", "absence": "biological_absence"}
FORBIDDEN = {
    "review_article": {"primary_experimental_evidence", "independent_replication", "causal_regulation"},
    "predicted_interaction": {"direct_binding", "native_species_function", "causal_regulation"},
    "proximity_labeling": {"direct_binding", "causal_regulation"},
    "peak_interval": {"binding_intensity", "transcriptional_activation", "causal_regulation"},
    "occupancy": {"transcriptional_activation", "causal_regulation"},
    "reporter_domain": {"target_site_editing", "editing_efficiency", "causal_regulation"},
    "catalogue_absence": {"biological_absence"},
    "stomatal_density": {"lineage_entry", "water_use_efficiency", "yield_benefit"},
    "heterologous_rescue": {"native_species_function", "native_circuit_validation"},
    "precursor_expression": {"mature_peptide_activity"},
    "m6a_enrichment": {"single_nucleotide_methylation", "rna_stability_mechanism"},
    "steady_state_rna": {"rna_half_life"},
    "fluorescent_puncta": {"phase_separation"},
    "acute_stomatal_aperture": {"stomatal_development"},
    "intrinsic_wue": {"whole_plant_wue", "yield_benefit"},
    "bulk_chromatin_shift": {"cell_autonomous_recruitment"},
    "dna_language_model_score": {"promoter_activity", "cell_type_specificity", "stress_inducibility"},
    "guide_design": {"target_repression", "target_site_editing"},
    "baseline_rescue": {"stress_response_restoration"},
    "phosphosite_reporter": {"kinase_specific_activity"},
}
WITHIN_SCOPE = {
    "review_article": {"review_synthesis", "primary_source_lead"},
    "predicted_interaction": {"computational_candidate"},
    "proximity_labeling": {"proximity_candidate"}, "peak_interval": {"occupancy_candidate", "interval_overlap"},
    "occupancy": {"occupancy_candidate", "occupancy"}, "reporter_domain": {"expression_domain"},
    "catalogue_absence": {"absence_in_catalogue"}, "rna_seq": {"differential_expression"},
    "chip_signal_track": {"relative_signal"}, "perturbation": {"perturbation_association"},
    "direct_binding_assay": {"direct_binding_in_assay"}, "target_site_sequencing": {"sequence_edit_detected"},
    "stomatal_density": {"density_difference"},
    "heterologous_rescue": {"rescue_in_tested_host"},
    "precursor_expression": {"precursor_detected"},
    "m6a_enrichment": {"rna_region_enrichment"},
    "steady_state_rna": {"rna_abundance_difference"},
    "fluorescent_puncta": {"puncta_observed"},
    "acute_stomatal_aperture": {"aperture_response"},
    "intrinsic_wue": {"a_over_gsw"},
    "bulk_chromatin_shift": {"bulk_chromatin_association"},
    "dna_language_model_score": {"sequence_model_score"},
    "guide_design": {"guide_candidate"},
    "baseline_rescue": {"basal_rescue"},
    "phosphosite_reporter": {"phosphosite_variant_response"},
}


def tag(value, aliases):
    text = value.strip().lower().replace("-", "_").replace(" ", "_") if isinstance(value, str) else ""
    return aliases.get(text, text)


def check_evidence(payload):
    """Check declared inference types; unknown types are never auto-approved."""
    records = payload.get("records") if isinstance(payload, dict) and "records" in payload else payload if isinstance(payload, list) else [payload]
    if not isinstance(records, list) or not records:
        return {**report([], []), "ok": False, "status": "requires_review", "results": [], "reason": "Nonempty evidence records required."}
    results = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            results.append({"index": i, "status": "requires_review", "reason": "Evidence record must be an object."})
            continue
        raw = record.get("evidence_type")
        types = [tag(x, EVIDENCE_ALIASES) for x in (raw if isinstance(raw, list) else [raw])]
        claim = tag(record.get("claim_type"), CLAIM_ALIASES)
        blocked = [e for e in types if claim in FORBIDDEN.get(e, set())]
        if blocked:
            state, reason = "blocked", "This evidence type does not support the requested inference: " + ", ".join(blocked)
        elif len(types) == 1 and claim in WITHIN_SCOPE.get(types[0], set()):
            state, reason = "within_scope", "Conservative declared inference; source quality, controls and biological interpretation remain unverified."
        else:
            state, reason = "requires_review", "Unknown or composite evidence/claim combination; expert review required."
        results.append({"index": i, "status": state, "evidence_type": types, "claim_type": claim, "reason": reason})
    states = {r["status"] for r in results}
    state = "blocked" if "blocked" in states else "requires_review" if "requires_review" in states else "within_scope"
    return {"ok": state == "within_scope", "status": state, "results": results, "scope": SCOPE, "scientific_truth_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    registry_cmd = sub.add_parser("validate-registry")
    registry_cmd.add_argument("path", nargs="?", default=str(DEFAULT_REGISTRY))
    create = sub.add_parser("new-run")
    create.add_argument("--workflow", required=True)
    create.add_argument("--task", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    validate = sub.add_parser("validate-run")
    validate.add_argument("path")
    validate.add_argument("--registry")
    evidence = sub.add_parser("check-evidence")
    evidence.add_argument("json", help="JSON file path, or '-' to read stdin")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-registry":
            result = validate_registry(load_json(args.path))
        elif args.command == "new-run":
            raw_registry = Path(args.registry).read_bytes()
            registry = json.loads(raw_registry)
            result = validate_registry(registry)
            if result["ok"]:
                output = Path(args.output).resolve()
                if output.exists():
                    raise FileExistsError("Run output already exists; refusing to overwrite it.")
                if not nonempty(args.task) or not any(w["id"] == args.workflow for w in registry["workflows"]):
                    raise ValueError("A known workflow and nonempty task are required.")
                snapshot = preserve_registry_snapshot(raw_registry, output.parent)
                run = new_run(registry, snapshot, args.workflow, args.task)
                if run["registry"]["sha256"] != hashlib.sha256(raw_registry).hexdigest():
                    raise ValueError("Registry snapshot changed during run creation; refusing inconsistent provenance.")
                # A relative pin keeps the run and its snapshot portable together.
                run["registry"]["path"] = str(snapshot.relative_to(output.parent))
                payload = (json.dumps(run, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                atomic_create_bytes(output, payload)
                result = report([], result["warnings"], output=str(output), run_id=run["run_id"], run_status="planned")
        elif args.command == "validate-run":
            path = Path(args.path).resolve()
            run = load_json(path)
            pin = run.get("registry", {}) if isinstance(run, dict) else {}
            pinned_path = pin.get("path") if isinstance(pin, dict) else None
            registry_path = Path(args.registry or pinned_path or DEFAULT_REGISTRY)
            if not registry_path.is_absolute():
                registry_path = path.parent / registry_path
            result = validate_run(run, load_json(registry_path), registry_path, path.parent)
        else:
            result = check_evidence(json.load(sys.stdin) if args.json == "-" else load_json(args.json))
    except (OSError, ValueError, TypeError) as exc:
        result = report([{"code": "input_error", "path": "$", "message": str(exc)}])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "requires_review":
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
