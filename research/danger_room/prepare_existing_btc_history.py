"""Verify already-owned January-June ZIPs and prepare a causal BTC benchmark.

This reads only exact XBTUSD_5.csv members after each whole archive matches its
fixed prior research pin. It neither downloads data nor changes the spot universe.
This tape establishes benchmark observations, not executable derivative prices.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

PINNED_ARCHIVES = {
    "kraken-2026-01-5m-usd.zip": ("892e6f142267a4c6c2d52537d79738107bcf07b39410f3ed6437ad2a75850fb6", 25721797),
    "kraken-2026-02-5m-usd.zip": ("52c1ede050ff0e82f01834d274f7d1cf9591793fbb9cb80a8e089ed2e9690ba3", 23891213),
    "kraken-2026-03-5m-usd.zip": ("844e0209a036eb459ee04ccca0528ab1ff27245b5dde209b78f2eb672befe937", 23968069),
    "kraken-2026-04-5m-usd.zip": ("d25f458c5fc1c862ad9989af7e528a98c701b569913f42baab58ddd295eb232a", 24209403),
    "kraken-2026-05-5m-usd.zip": ("1085bb0a87980338d48811182d2b35695db11edcd467ccd5ba169467d4b6ef29", 27648109),
    "kraken-2026-06-5m-usd.zip": ("7229bfcfbd6327836c4ba6ac605205ae63e5fb1a1a76849fa1416f02f059b0c0", 27448598),
}
MEMBER = "ohlcvt/XBTUSD_5.csv"
COLUMNS = ("open_timestamp", "completed_timestamp", "open", "high", "low", "close", "volume", "trades", "present")
START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
INTERVAL = 300


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(source_directory: Path, output_directory: Path) -> dict:
    observations = {}
    source_receipts = []
    for month, (name, (expected_hash, expected_size)) in enumerate(PINNED_ARCHIVES.items(), 1):
        archive_path = source_directory / name
        if archive_path.is_symlink() or not archive_path.is_file():
            raise ValueError(f"Nonregular or symlink archive: {name}")
        archive_bytes = archive_path.read_bytes()
        actual_size = len(archive_bytes)
        actual_hash = hashlib.sha256(archive_bytes).hexdigest()
        if actual_size != expected_size or actual_hash != expected_hash:
            raise ValueError(f"Unverified archive: {name}")
        month_start = calendar.timegm((2026, month, 1, 0, 0, 0))
        month_end = calendar.timegm((2026, month + 1, 1, 0, 0, 0))
        # Parse the same immutable byte snapshot that passed the pin, rather
        # than reopening a path that another process could change afterwards.
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            selected = [entry for entry in archive.infolist() if entry.filename == MEMBER]
            if len(selected) != 1 or selected[0].is_dir():
                raise ValueError(f"Missing or repeated exact benchmark member: {name}")
            raw = archive.read(MEMBER)  # also validates this member's CRC
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
        previous_timestamp = None
        for row in rows:
            if len(row) != 7 or any(field.strip() != field or not field for field in row):
                raise ValueError(f"Invalid raw row schema: {name}")
            if not row[0].isdigit() or not row[6].isdigit():
                raise ValueError(f"Nonintegral timestamp/trade count: {name}")
            timestamp = int(row[0])
            if timestamp % INTERVAL or not month_start <= timestamp < month_end:
                raise ValueError(f"Off-grid/out-of-month bar: {name}")
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ValueError(f"Nonincreasing benchmark rows: {name}")
            previous_timestamp = timestamp
            try:
                opening, high, low, closing, volume = map(Decimal, row[1:6])
            except InvalidOperation as exc:
                raise ValueError(f"Nonnumeric benchmark: {name}") from exc
            if any(not value.is_finite() for value in (opening, high, low, closing, volume)):
                raise ValueError(f"Nonfinite benchmark: {name}")
            if min(opening, high, low, closing) <= 0 or volume < 0:
                raise ValueError(f"Invalid benchmark value: {name}")
            if not low <= min(opening, closing) <= max(opening, closing) <= high:
                raise ValueError(f"Incoherent OHLC: {name}")
            if timestamp in observations:
                raise ValueError(f"Duplicate benchmark bar: {name}")
            observations[timestamp] = row
        source_receipts.append({
            "archive_name": name,
            "archive_size_bytes": actual_size,
            "archive_sha256": actual_hash,
            "archive_matches_preexisting_pin": True,
            "exact_member": MEMBER,
            "member_size_bytes": len(raw),
            "member_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_rows": len(rows),
            "expected_calendar_rows": (month_end - month_start) // INTERVAL,
            "first_open_timestamp": int(rows[0][0]) if rows else None,
            "last_open_timestamp": int(rows[-1][0]) if rows else None,
            "only_this_member_read": True,
        })
    output_directory.mkdir(parents=True, exist_ok=True)
    tape_path = output_directory / "BTC_USD_2026_H1_5m.csv"
    missing = []
    with tape_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        for timestamp in range(START, END, INTERVAL):
            row = observations.get(timestamp)
            if row is None:
                missing.append(timestamp)
                writer.writerow((timestamp, timestamp + INTERVAL, "", "", "", "", "", "", 0))
            else:
                writer.writerow((timestamp, timestamp + INTERVAL, *row[1:], 1))
    receipt = {
        "schema": "spectra-owned-btc-benchmark-h1-v1",
        "scope": "HISTORICAL_SPOT_BENCHMARK_ONLY",
        "calendar_start_open_utc": "2026-01-01T00:00:00Z",
        "calendar_end_open_exclusive_utc": "2026-07-01T00:00:00Z",
        "interval_seconds": INTERVAL,
        "full_calendar_rows": (END - START) // INTERVAL,
        "actual_observed_rows": len(observations),
        "missing_rows": len(missing),
        "missing_open_timestamps": missing,
        "columns": list(COLUMNS),
        "timestamp_semantics": "Source timestamp is bar opening; OHLCV/trades become usable only at completed_timestamp = opening + 300. An opening fill may use that opening price, never the same bar's later high/low/close/volume.",
        "missingness": "Absent rows retained with present=0 and blank observations; no forward fill or successor substitution.",
        "source_symbol": "XBTUSD",
        "canonical_benchmark_identity": "Bitcoin priced in USD",
        "identity_primary_source": "https://support.kraken.com/articles/360001206766-bitcoin-currency-code-xbt-vs-btc",
        "identity_primary_source_last_updated": "2025-03-31",
        "identity_primary_source_checked_utc_date": "2026-10-10",
        "spot_fixed619_universe_modified": False,
        "spot_btc_eth_exclusion_modified": False,
        "tape_filename": tape_path.name,
        "tape_size_bytes": tape_path.stat().st_size,
        "tape_sha256": sha256_file(tape_path),
        "sources": source_receipts,
        "external_market_data_downloaded": False,
        "existing_owned_archives_materialized": True,
        "sealed_holdout_read": False,
        "spot_array_reconstructed": False,
        "derivative_prices_verified": False,
        "funding_margin_fee_histories_verified": False,
        "historical_venue_executability_claim": False,
        "hedge_profitability_test_performed_by_this_preparer": False,
        "approved_for_live": False,
    }
    receipt_path = output_directory / "btc-benchmark.receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.source_directory, args.output_directory)
    print(json.dumps({key: result[key] for key in ("full_calendar_rows", "actual_observed_rows", "missing_rows", "tape_sha256")}))


if __name__ == "__main__":
    main()

