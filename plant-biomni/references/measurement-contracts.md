# Reviewed count arithmetic

The calculator takes adjudicated tabular counts, not images. The fixed convention is one guard-cell pair per stoma (`S`), and all nonstomatal epidermal cells including precursors (`E`). SI is `100*S/(S+E)`. Density is `1,000,000*S/area_um2`. Do not silently substitute individual guard cells or pavement-cell-only denominators.

CSV fields: `sample_id, plant_id, leaf_id, field_id, stomata_pairs, nonstomatal_cells, denominator_complete, identity_resolved, units_resolved, border_resolved, same_roi`; `area_um2` is needed for density. Counts must be finite nonnegative integers. Gates must be explicitly positive before release. Record actual ROI, image, calibration and reviewer evidence outside this table.

A JSON contract must explicitly set the formula, units, common ROI and boundary convention; see `examples/count-contract.json`. Plant pooling requires declared nonoverlapping/deduplicated fields. Gates are caller assertions, not verified observations. Failed rows are not silently dropped to produce pooled results. Outputs retain QC issues and source hashes. This does not calculate SLI, segment cells or validate biological accuracy.

```sh
python scripts/stomatal_metrics.py --input examples/counts.csv --contract examples/count-contract.json --output count-audit.json
```

The bundled synthetic field has S=10, E=90 and area=100000 square micrometres, giving SI=10% and density=100/mm². These numbers are test data, not a real experiment.
