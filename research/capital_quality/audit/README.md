# Portable independent quality audits

These copies read saved July outputs. They do not execute trading strategies, hydrate the account, access an exchange, or score the sealed prospective holdout. Original auditors and receipts remain in their archived research evidence. Changes here are path/CLI portability and separate receipt names; financial predicates and accounting oracles are preserved.

From the repository root, with the external canonical prepared tape and saved outputs available:

```bash
python3 research/capital_quality/audit/audit_quality_runs.py \
  --protocol research/capital_quality/protocol.json \
  --prepared /path/to/prepared \
  --run /path/to/SHARED_QUALITY \
  --output /path/to/quality-audit.json
python3 research/capital_quality/audit/attribute_quality_outputs.py \
  --protocol research/capital_quality/protocol.json \
  --prepared /path/to/prepared \
  --control /path/to/PARK \
  --run /path/to/SHARED_QUALITY \
  --output /path/to/quality-attribution.json
```

Repeat `--run` to audit additional arms. NumPy is required for recorded-tape checks. The base adapter defaults to the existing frozen `research/parked_cash/release_adapter.py`. The financial auditor defaults to the unchanged preserved `financial_source_auditor.py` beside this script, which includes the latest completeness and pending-claim checks. The older parked-cash auditor remains untouched. `--financial-auditor` and `--base-adapter` can select explicit preserved alternatives. The terminal-mark and aggregate-attribution helpers are unchanged copies beside these scripts; their optional CLI overrides require explicit paths. Giant tapes, native archives and account checkpoints are external inputs rather than Git payloads.

```bash
python3 research/capital_quality/audit/test_causal_quality_gate.py
SPECTRA_NATIVE_WRAPPER=/path/to/frozen/continuation_wrapper.py \
  python3 research/capital_quality/audit/test_quality_source_mechanics.py
```

Causal tests use constructed temporary data; native source tests use authentic frozen ownership methods with constructed monetary cases. The native module raises a clear `SkipTest` when its required external wrapper is absent. A skip is not a native validation pass. Ported test receipts have separate filenames so original receipts are preserved.

`Portability_Receipt.json` records original and ported source hashes and bounded verification. This port passed 17 causal checks, 12 native purpose checks, and one saved-output SHARED_RAW audit across 8,640 rows. None of these is a new historical strategy replay or a profitability claim.
