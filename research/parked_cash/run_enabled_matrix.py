"""Run the four enabled arms sequentially after an exact OFF control passes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ARMS = ("ENTRY_ONLY", "RECOVERY_ONLY", "BOTH", "BOTH_TIGHT")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("wrapper", "hydration", "checkpoint", "prepared", "results"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    control = json.loads((args.results / "OFF" / "result.json").read_text())
    audit = json.loads((args.results / "OFF" / "control_archive_audit.json").read_text())
    if control["status"] != "PASS" or not audit["passed"]:
        raise ArithmeticError("The unchanged control has not passed its archived comparison")
    pins = {name: hashlib.sha256((HERE/name).read_bytes()).hexdigest()
        for name in ("protocol.json", "release_adapter.py", "run_comparison.py")}
    records = []
    def save():
        (args.results / "matrix_receipt.json").write_text(json.dumps(
            dict(research_only=True, fixed_source_pins=pins, records=records), indent=2)+"\n")
    for arm in ARMS:
        for name, digest in pins.items():
            if hashlib.sha256((HERE/name).read_bytes()).hexdigest() != digest:
                raise ArithmeticError("Frozen matrix source changed")
        record = dict(arm=arm, status="STARTED", start_wall_time=time.time())
        records.append(record)
        save()
        print(json.dumps(dict(event="ARM_STARTED", arm=arm)), flush=True)
        command = [sys.executable, str(HERE/"run_comparison.py"), "--arm", arm]
        for name in ("wrapper", "hydration", "checkpoint", "prepared"):
            command.extend(["--"+name, str(getattr(args, name).resolve())])
        command.extend(["--output", str((args.results/arm).resolve())])
        with (args.results/(arm+".log")).open("w") as output:
            process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        record.update(end_wall_time=time.time(), returncode=process.returncode)
        path = args.results/arm/"result.json"
        if process.returncode or not path.exists():
            record["status"] = "FAILED"
            save()
            raise RuntimeError(f"{arm} failed; preserving evidence and stopping matrix")
        result = json.loads(path.read_text())
        record.update(status="COMPLETED", ending_equity=result["ending_equity"],
            actual_fills=result["fill_counts"], activated=result["activated"])
        save()
        print(json.dumps(dict(event="ARM_COMPLETED", **record)), flush=True)
        if result["status"] != "PASS" or not result["activated"]:
            record["status"] = "BLOCKED"
            save()
            raise RuntimeError("Enabled capital authority did not produce an audited actual fill")


if __name__ == "__main__":
    main()
