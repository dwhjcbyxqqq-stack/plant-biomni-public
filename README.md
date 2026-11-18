# Plant Biomni Skills · Public Core

A small collection of agent skills and deterministic Python tools for auditing plant-research data and evidence metadata. Public-core **0.1.0**, derived from a larger personal workflow system. This repository is **not the upstream Biomni software**, an autonomous laboratory, or the complete private Plant Biomni catalog.

## Included

- Reviewed stomatal count arithmetic: qualified SI, density and plant-level pooling.
- Supplied-sequence identity, annotation and declared score-metadata checks.
- Bibliographic metadata consistency and provenance checks.
- Workflow/run metadata validation and source-pinned decision packets.
- Synthetic examples and regression tests. No paid APIs or external Python packages required.

## Quick start

Python 3.10+ on a local filesystem is recommended. Tests were run on Linux; run them on your own platform before use.

```sh
cd plant-biomni
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/pb_contracts.py validate-registry
python scripts/stomatal_metrics.py --input examples/counts.csv --contract examples/count-contract.json --output count-audit.json
python scripts/sequence_design_qc.py --input examples/sequences.json --output sequence-audit.json --k 4
python scripts/literature_audit.py examples/papers.json --output paper-audit.json
```

For a skill-aware agent, provide the `plant-biomni/` folder with `SKILL.md`, scripts and references together. Installation procedures depend on the host. Merely cloning this repository does not automatically install or activate a skill.

## Scientific limits

Software tests verify behavior on synthetic inputs, not biology. Caller-supplied assertions do not become observations because a metadata checker accepts them. The tools do not segment images, perform wet-lab work, design validated guides, score DNA models, fetch literature, or guarantee that source notices are current. Read `plant-biomni/references/tool-contracts.md` before use.

## Scope and rights

The private research corpus, experimental records, unpublished project plans, full workflow catalog, third-party skill packages and financial adapters are deliberately excluded. A three-adapter registry replaces the private catalog. No paper PDFs, figures, text excerpts or model weights are bundled.

No open-source license has been selected. Public visibility is not a grant of an open-source license; contact the repository owner for reuse permissions. This release does not assert that licensing or institutional IP questions have been adjudicated.
