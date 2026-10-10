"""Constructed mechanics-only candles; no market observations or performance claim."""
from __future__ import annotations

import json
from pathlib import Path

from pins import digest, verify_freeze


def create_fixture(repo_root, output, *, evaluation_frames=256, sparse=False):
    import numpy as np
    freeze = verify_freeze(Path(repo_root))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    pairs = freeze["universe"]["pairs"]
    frames = 288 + evaluation_frames
    tape = np.full((frames, 619, 6), np.nan, dtype="float64")
    # Only 60 assets have constructed rows. Missing619-universe members remain
    # visible and untradeable instead of being removed from the comparison.
    for i in range(frames):
        k = max(0, i-288) % 80
        phase = 1.0 if k < 4 else (max(.78, 1.0-(k-3)*.008) if k < 32 else min(1.10, .78+(k-32)*.008))
        for j in range(60):
            # Fixed heterogeneity exercises native movement ranking. Constructed
            # sell/recovery paths are fixtures, never estimated trading returns.
            price = (100+j) * phase * (1 + .00001*j*i)
            tape[i, j] = [price, price*(1.005+j*.00001), price*.995, price, 10000, 20]
    if sparse:
        tape[286:289, :8] = np.nan
        tape[310:315, 10:15] = np.nan
    np.save(output / "ohlcv.npy", tape)
    start = 1767225600
    metadata = dict(schema=1, start=start, end=start+frames*300,
        interval_seconds=300, pairs=pairs,
        symbols=[p.replace("-", "") for p in pairs],
        input_class="constructed_fixture", uses_market_observations=False,
        source_hashes={}, observed_rows=int(np.isfinite(tape[:,:,0]).sum()))
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    receipt = dict(schema="spectra-fresh-replay-fixture-v1",
        input_class="constructed_fixture", uses_market_observations=False,
        performance_claim=False, warmup_frames=288, evaluation_frames=evaluation_frames,
        sha256={name:digest(output/name) for name in ("metadata.json", "ohlcv.npy")})
    (output / "fixture-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
