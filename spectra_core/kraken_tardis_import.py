"""Convert recorded Kraken v2 ticker/instrument NDJSON to replay inputs.

Input is Tardis /data-feeds output (optional gzip), in capture order. No network
calls or fabricated candles. Quotes use capture time rounded UP to whole seconds.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .strategy_replay import Quote, number


def capture_second(stamp):
    if not stamp.endswith("Z"):
        raise ValueError("UTC Z capture timestamp required")
    base = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    suffix = stamp[19:-1]
    fraction = Decimal(suffix) if suffix else Decimal(0)
    return Decimal(int(base.timestamp())) + fraction


def convert(paths, output, manifest, *, symbol="BTC/USD", interval_seconds=60):
    if type(interval_seconds) is not int or interval_seconds < 1:
        raise ValueError("positive sample interval required")
    pair = symbol.replace("/", "-")
    rules = None
    previous_capture = None
    last_sample = None
    rows = []
    sources = []
    stats = {
        "messages": 0,
        "disconnects": 0,
        "missing_rules": 0,
        "ticker_messages": 0,
        "not_online": 0,
    }
    for raw_path in paths:
        path = Path(raw_path)
        sources.append(
            {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as stream:
            for line in stream:
                if not line.strip():
                    rules = None
                    stats["disconnects"] += 1
                    continue
                stamp, payload = line.rstrip("\n").split(" ", 1)
                received = capture_second(stamp)
                if previous_capture is not None and received < previous_capture:
                    raise ValueError("raw capture time moved backwards")
                previous_capture = received
                message = json.loads(payload, parse_float=Decimal)
                stats["messages"] += 1
                if message.get("channel") == "instrument":
                    if message.get("type") == "snapshot":
                        rules = None
                    for rule in message.get("data", {}).get("pairs", []):
                        if rule.get("symbol") == symbol:
                            rules = {**(rules or {}), **rule}
                    continue
                if message.get("channel") != "ticker":
                    continue
                for ticker in message["data"]:
                    if ticker.get("symbol") != symbol:
                        continue
                    stats["ticker_messages"] += 1
                    if rules is None:
                        stats["missing_rules"] += 1
                        continue
                    timestamp = int(received.to_integral_value(rounding=ROUND_CEILING))
                    if (
                        last_sample is not None
                        and timestamp < last_sample + interval_seconds
                    ):
                        continue
                    # Symmetric depth is the smaller observed best-side quantity.
                    depth = min(
                        Decimal(str(ticker["bid_qty"])), Decimal(str(ticker["ask_qty"]))
                    )
                    if rules.get("status") != "online":
                        depth = Decimal(0)
                        stats["not_online"] += 1
                    quote = Quote(
                        timestamp,
                        pair,
                        ticker["bid"],
                        ticker["ask"],
                        depth,
                        rules["qty_min"],
                        rules["cost_min"],
                        rules["qty_increment"],
                        number(ticker["volume"], "volume")
                        * number(ticker["vwap"], "vwap"),
                    )
                    rows.append(quote)
                    last_sample = timestamp
    if len(rows) < 2:
        raise ValueError("insufficient quotes with contemporaneous instrument rules")
    output, manifest = Path(output), Path(manifest)
    columns = (
        "timestamp",
        "pair",
        "bid",
        "ask",
        "depth_base",
        "min_base",
        "min_quote",
        "step_base",
        "quote_volume_24h",
    )
    with output.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(tuple(getattr(q, col) for col in columns) for q in rows)
    gaps = [b.timestamp - a.timestamp for a, b in zip(rows, rows[1:])]
    metadata = {
        "kind": "recorded",
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "source": "Tardis archived Kraken v2 ticker/instrument messages; see raw_sources hashes",
        "venue": "kraken",
        "rules_source": "causal instrument snapshot/updates; reset at disconnect",
        "volume_source": "recorded ticker base volume multiplied by recorded 24h VWAP (reported precision)",
        "depth_definition": "minimum of recorded best bid_qty and ask_qty; zero unless pair online; top level only",
        "protocol_id": "first-day-kraken-pipeline-smoke-v1",
        "point_in_time_rules": True,
        "max_gap_seconds": interval_seconds * 5,
        "start_timestamp": rows[0].timestamp,
        "end_timestamp": rows[-1].timestamp,
        "raw_sources": sources,
        "import_stats": stats,
        "rows": len(rows),
        "sampling": "first observed ticker at least interval_seconds after prior emitted row; no forward fill",
        "interval_seconds": interval_seconds,
        "observed_max_gap_seconds": max(gaps),
        "clock": "local capture timestamp rounded up to integer seconds",
        "limitations": [
            "top-of-book quantities, not full L2 liquidity or guaranteed executable fills",
            "historical point-in-time feed rules; no independently verified fee tier",
            "first-day sample is not representative of all market regimes",
        ],
    }
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--symbol", default="BTC/USD")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    result = convert(
        args.raw,
        args.output,
        args.manifest,
        symbol=args.symbol,
        interval_seconds=args.interval_seconds,
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("rows", "observed_max_gap_seconds", "import_stats")
            }
        )
    )


if __name__ == "__main__":
    main()
