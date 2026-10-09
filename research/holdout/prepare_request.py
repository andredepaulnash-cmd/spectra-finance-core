#!/usr/bin/env python3
"""Validate a dated capture request, recording metadata without market access."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

REQUIRED = {"schema", "action", "capture_date_utc", "requested_utc",
            "protocol_sha256", "no_strategy_evaluation"}


def prepare(request_path, protocol_path, output, now=None):
    request_path, protocol_path, output = Path(request_path), Path(protocol_path), Path(output)
    request_raw, protocol_raw = request_path.read_bytes(), protocol_path.read_bytes()
    request, protocol = json.loads(request_raw), json.loads(protocol_raw)
    if set(request) != REQUIRED or request["schema"] != 1:
        raise ValueError("Capture request has an unknown schema or extra authority")
    if request["action"] != "public_ohlc_capture" or request["no_strategy_evaluation"] is not True:
        raise ValueError("Only public capture with no strategy evaluation is authorized")
    date = request["capture_date_utc"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or request_path.name != date+".json":
        raise ValueError("Capture request must have its canonical UTC date filename")
    now = now or dt.datetime.now(dt.timezone.utc)
    requested = dt.datetime.fromisoformat(request["requested_utc"].replace("Z", "+00:00"))
    if requested.tzinfo is None or requested.utcoffset() != dt.timedelta(0):
        raise ValueError("Capture request needs an explicit UTC timestamp")
    if requested.date().isoformat() != date or not -90 <= (now-requested).total_seconds() <= 48*3600:
        raise ValueError("Capture request is incorrectly dated, future, or stale beyond 48 hours")
    digest = hashlib.sha256(protocol_raw).hexdigest()
    if protocol.get("status") != "FROZEN" or request["protocol_sha256"] != digest:
        raise ValueError("Request does not identify the exact frozen protocol")
    output.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": "spectra-public-capture-request-receipt-v1",
               "validated_utc": now.isoformat(), "request_file": request_path.name,
               "request_sha256": hashlib.sha256(request_raw).hexdigest(),
               "protocol_sha256": digest, "checkout_sha": os.environ.get("GITHUB_SHA"),
               "github_run_id": os.environ.get("GITHUB_RUN_ID"),
               "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
               "strategy_evaluated": False, "capture_started": False}
    (output/"capture-request-receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests",required=True)
    parser.add_argument("--protocol",required=True)
    parser.add_argument("--output",required=True)
    args = parser.parse_args()
    names = [path for path in Path(args.requests).glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json",path.name)]
    if not names:
        raise ValueError("No dated public capture request exists")
    receipt = prepare(max(names,key=lambda path:path.name),args.protocol,args.output)
    print(json.dumps({"request_file":receipt["request_file"],"strategy_evaluated":False,"request_validated":True}))


if __name__ == "__main__":
    main()
