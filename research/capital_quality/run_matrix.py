"""Sequential frozen 2x2 July replay; no strategy outcome tuning."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ARMS = ("SHARED_RAW", "SPLIT_RAW", "SHARED_QUALITY", "SPLIT_QUALITY")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("wrapper", "hydration", "checkpoint", "prepared", "results", "previous"):
        p.add_argument("--"+name, type=Path, required=True)
    a = p.parse_args()
    protocol = json.loads((HERE/"protocol.json").read_text())
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    pins = {name: digest(HERE/name) for name in
            ("protocol.json", "quality_gate.py", "release_adapter.py", "run_comparison.py", "run_matrix.py")}
    base = HERE.parent/"parked_cash"/"release_adapter.py"
    pins["base_adapter"] = digest(base)
    a.results.mkdir(parents=True, exist_ok=True)
    records = []
    def save():
        (a.results/"matrix_receipt.json").write_text(json.dumps(dict(
            research_only=True, frozen_source_pins=pins, records=records), indent=2)+"\n")
    for arm in ARMS:
        for name, expected in pins.items():
            path = base if name == "base_adapter" else HERE/name
            if digest(path) != expected:
                raise ArithmeticError("Frozen matrix source changed: "+name)
        record = dict(arm=arm, status="STARTED", start_wall_time=time.time())
        records.append(record); save()
        print(json.dumps(dict(event="ARM_STARTED", arm=arm)), flush=True)
        command = [sys.executable, str(HERE/"run_comparison.py"), "--arm", arm]
        for name in ("wrapper", "hydration", "checkpoint", "prepared"):
            command += ["--"+name, str(getattr(a,name).resolve())]
        command += ["--output", str((a.results/arm).resolve()), "--seconds",
                    str(protocol["execution"]["maximum_simulation_seconds_per_arm"])]
        with (a.results/(arm+".log")).open("w") as log:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        record.update(end_wall_time=time.time(), returncode=process.returncode)
        path = a.results/arm/"result.json"
        if process.returncode or not path.exists():
            record["status"] = "FAILED"; save()
            raise RuntimeError(arm+" failed; evidence preserved, matrix stopped")
        result = json.loads(path.read_text())
        if result["status"] != "PASS":
            raise ArithmeticError("Financial result did not pass")
        if arm == "SHARED_RAW":
            proof = {}
            for filename in ("window_accounts.csv", "new_fills.json", "terminal_active_lots.json", "terminal_positions.json"):
                proof[filename] = digest(a.results/arm/filename) == digest(a.previous/filename)
            (a.results/arm/"previous_shared_raw_equivalence.json").write_text(json.dumps(
                dict(passed=all(proof.values()), exact_byte_matches=proof), indent=2)+"\n")
            if not all(proof.values()):
                record["status"] = "CONTROL_MISMATCH"; save()
                raise ArithmeticError("SHARED_RAW differs from audited BOTH")
        record.update(status="COMPLETED" if result["activated"] else "INACTIVE_INCONCLUSIVE",
            ending_equity=result["ending_equity"], fills=result["fill_counts"], activated=result["activated"])
        save()
        print(json.dumps(dict(event="ARM_COMPLETED", **record)), flush=True)


if __name__ == "__main__":
    main()
