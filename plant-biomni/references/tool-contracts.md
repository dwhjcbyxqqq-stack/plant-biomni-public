# Tool contracts

All scripts use Python standard-library modules. Run from the `plant-biomni` directory. Use a fresh output path; some tools refuse an existing output, while the count calculator can replace an existing output. None should target an input path.

## Sequence QC

`python scripts/sequence_design_qc.py --input examples/sequences.json --output sequence-audit.json --k 4`

Manifest: `schema_version: "1.0.0"`, `coordinate_convention: "0-based-half-open"`, nonempty `candidates`. Each candidate has unique `id`, exact uppercase ACGT `sequence`, `purpose`, `sources` and `modules`. Modules use `id`, integer `start` and `end`, `strand` (+ or -), optional `expected_sequence`; negative-strand expected sequences are reverse complements. Overlap must be explicitly declared on both modules. Model-score metadata is audited, not recomputed. No actual model is called.

## Bibliographic audit

`python scripts/literature_audit.py examples/papers.json --output paper-audit.json`

Accepts a complete list of records or an object with `records`. Relevant fields include `id`, `doi`, `title`, `species`, `year`, `publication_type`, `read_scope`, and `notice_check`. It flags duplicate/malformed identifiers, inconsistent titles, provenance gaps and notice issues. It neither downloads sources nor proves that a notice is current. The example uses fictional metadata, not a publication.

## Workflow and run metadata

`python scripts/pb_contracts.py validate-registry`

`python scripts/pb_contracts.py new-run --workflow PB-WF-METRICS --task "Audit reviewed counts" --output run.json`

`python scripts/pb_contracts.py validate-run run.json`

A generated run is a plan, not completed work. Populate input/output hashes, actual execution evidence and QC results before asserting completion. The small public registry does not include the private catalog. `check-evidence` applies enumerated rules only, not a full scientific review.

## Decision packets

`python scripts/decision_packet.py new --index YOUR_INDEX.json --notices YOUR_NOTICES.json --registry references/workflow-registry.json --record EXACT_RECORD_KEY --workflow PB-WF-METRICS --task "Your question" --output packet.json`

Index records require unique `record_key`; source-record ID selectors must be unambiguous. Supply source identity, provenance, read scope, context and publication/notice metadata. The notice file is an object with `version` and `notices`. Use explicit current evidence, not the synthetic test fixtures. The nine decision fields cover estimand, source records, context, workflow versions, strongest alternative, comparisons, outputs, decision gates and execution/writeback. Use `python scripts/decision_packet.py validate packet.json` after completing the actual decision record. Unknowns are retained and can block readiness.
