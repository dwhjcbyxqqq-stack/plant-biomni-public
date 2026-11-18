---
name: plant-biomni
description: Audit supplied plant research count tables, exact sequence manifests, bibliographic metadata, and source-pinned planning records. Use for stomatal SI/density arithmetic, sequence consistency QC, evidence-boundary checks, and reproducible workflow metadata. Does not provide an autonomous laboratory, literature corpus, image segmentation, or biological validation.
---

# Plant Biomni public core

Public-core release 0.1.0. This reduced distribution is separate from the owner's full private workflow system and from the upstream Biomni project.

## Route and execute

1. Establish the question, supplied inputs, units and success criteria. Preserve originals. Do not fill missing experimental observations with assumptions.
2. Choose a bundled tool. Read `references/tool-contracts.md` and use `--help` for exact command syntax.
   - Reviewed counts: `scripts/stomatal_metrics.py`; read `references/measurement-contracts.md`.
   - Exact supplied sequences: `scripts/sequence_design_qc.py`. Scores are not predicted biological activity.
   - Complete bibliographic metadata: `scripts/literature_audit.py`. This does not fetch or verify papers online.
   - Workflow/run metadata: `scripts/pb_contracts.py`. The bundled registry contains three public digital adapters only.
   - Source-pinned planning packet: `scripts/decision_packet.py`. Supply your own index, notice file and registry explicitly; no literature corpus is bundled.
3. Execute only the requested digital action; save outputs separately from inputs. Review warnings, failures and unresolved fields.
4. Report what was computed, what remains unknown, and which biological conclusions are not established. Software tests and metadata gates do not validate science.

## Boundaries

Count-table checks cannot inspect the underlying image, confirm cell identity or prove the declared gates. Sequence QC cannot design validated promoters/guides, check genome-wide off-targets or predict stress responsiveness. A bibliographic audit cannot establish causality or independent replication. Planning packets do not execute experiments.

Examples are synthetic software fixtures. Use fresh, authorized sources for actual scientific work. Check corrections/retractions against current primary records before reuse; the tools only inspect notice metadata you supply.
