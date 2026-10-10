"""Assemble a frozen prospective capture only after its embargo has ended.

This is an offline integrity and input adapter, not a strategy evaluator. Raw
pages and SQLite stores are never opened by the public API before November 9.
Constructed tests have a separate in-memory fixture API with no capture paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile

from pins import FROZEN_PROTOCOL_SHA256, digest, verify_freeze

UTC = dt.timezone.utc
EMBARGO_END = dt.datetime(2026, 11, 9, tzinfo=UTC)
INTERVAL = 300
WARMUP_FRAMES = 288
MAX_RAW_BYTES = 8_000_000
COLLECTOR_SHA256 = "2e6393f0a6692200b573a680fd813e23a8c6de7010b5156e907f25a79e804248"
ASSEMBLY_SCHEMA = "spectra-holdout-capture-assembly-v1"
ENDPOINTS = {"Time": set(), "AssetPairs": {"aclass_base"},
             "OHLC": {"pair", "interval", "since"}}


def utcnow():
    return dt.datetime.now(UTC)


def instant(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("Capture timestamps must be explicit UTC")
    return parsed


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _embargo_guard():
    if utcnow() < EMBARGO_END:
        raise ValueError("Prospective capture remains sealed until 2026-11-09T00:00:00Z")


def _freeze(repo_root):
    freeze = verify_freeze(Path(repo_root))
    protocol = freeze["protocol"]
    if instant(protocol["end_utc"]) != EMBARGO_END or protocol["evaluation_after_utc"] != protocol["end_utc"]:
        raise ValueError("Frozen embargo boundary differs from the source pin")
    if protocol["collector"]["sha256"] != COLLECTOR_SHA256:
        raise ValueError("Unexpected frozen collector source")
    if freeze["financial"]["warmup"]["hours_before_freeze"] != 24:
        raise ValueError("Unexpected frozen warmup horizon")
    return freeze


def _read(path, guard):
    guard()
    return Path(path).read_bytes()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _whole_number(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " is not an integer")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(label + " is malformed") from error
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise ValueError(label + " is not a nonnegative integer")
    return int(number)


def _row(row):
    if not isinstance(row, list) or len(row) != 8:
        raise ValueError("Malformed collector-format OHLC row")
    stamp = _whole_number(row[0], "OHLC timestamp")
    if stamp % INTERVAL:
        raise ValueError("OHLC timestamp is not aligned to five minutes")
    values = []
    for value in row[1:7]:
        try:
            number = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError("Malformed OHLC value") from error
        if not number.is_finite():
            raise ValueError("OHLC values must be finite")
        values.append(number)
    o, h, low, close, vwap, volume = values
    if min(o, h, low, close, vwap) <= 0 or volume < 0:
        raise ValueError("OHLC prices/volume outside valid domain")
    if h < max(o, low, close) or low > min(o, h, close):
        raise ValueError("OHLC high/low do not contain open and close")
    trades = _whole_number(row[7], "Trade count")
    if trades > 2**53:
        raise ValueError("Trade count exceeds exact native float64 integer range")
    native = [float(x) for x in (o, h, low, close, volume)] + [float(trades)]
    if not all(math.isfinite(x) for x in native):
        raise ValueError("OHLC values cannot be represented in the native float64 tape")
    if min(native[:4]) <= 0 or (volume > 0 and native[4] == 0):
        raise ValueError("OHLC values underflow the native float64 tape")
    return stamp, native


def _online_pairs(payload):
    result = payload.get("result")
    if payload.get("error") or not isinstance(result, dict):
        raise ValueError("Malformed AssetPairs response")
    by_symbol = {}
    for key, details in result.items():
        if (details.get("quote") not in {"USD", "ZUSD"}
                or details.get("aclass_base") != "currency"
                or details.get("status") != "online" or "." in key):
            continue
        symbol = details.get("altname", key)
        if not re.fullmatch(r"[A-Z0-9]{2,40}", symbol):
            raise ValueError("Unsafe captured altname")
        if symbol in by_symbol:
            raise ValueError("Ambiguous captured altname identity")
        # No guessed exchange aliases: retain the frozen Kraken BASE-USD name.
        canonical = symbol[:-3] + "-USD" if symbol.endswith("USD") and len(symbol) > 3 else None
        by_symbol[symbol] = (key, canonical)
    if not by_symbol:
        raise ValueError("No online USD currency pairs in captured AssetPairs")
    canonical = [x[1] for x in by_symbol.values() if x[1] is not None]
    if len(canonical) != len(set(canonical)):
        raise ValueError("Ambiguous canonical pair normalization")
    return by_symbol


def _directory_records(directory, freeze, guard, sources):
    """Yield bounded raw responses only after verifying their collector store."""
    directory = Path(directory).resolve()
    protocol_raw = _read(directory / "frozen-protocol.json", guard)
    if hashlib.sha256(protocol_raw).hexdigest() != FROZEN_PROTOCOL_SHA256:
        raise ValueError("Capture belongs to a different financial/source freeze")
    receipt_raw = _read(directory / "freeze-receipt.json", guard)
    receipt = _json(receipt_raw)
    if receipt["protocol_sha256"] != FROZEN_PROTOCOL_SHA256:
        raise ValueError("Capture freeze receipt mismatch")
    status_raw = _read(directory / "status.json", guard)
    status = _json(status_raw)
    db_path = directory / "capture.sqlite3"
    guard()
    sqlite_hash = digest(db_path)
    if status["sqlite_sha256"] != sqlite_hash:
        raise ValueError("Capture SQLite snapshot checksum mismatch")
    if status.get("protocol_sha256") != FROZEN_PROTOCOL_SHA256:
        raise ValueError("Capture status protocol mismatch")
    if status.get("strategy_evaluated") is not False or status.get("financial_outcomes_printed") is not False:
        raise ValueError("Capture status violates sealed collection-only provenance")
    source = {"capture_directory": str(directory), "sqlite_sha256": sqlite_hash,
              "status_sha256": hashlib.sha256(status_raw).hexdigest(),
              "freeze_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
              "protocol_sha256": FROZEN_PROTOCOL_SHA256, "raw_objects": [],
              "verified_stored_bars": 0}
    sources.append(source)
    guard()
    connection = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Corrupt collector SQLite store")
        counts = dict(connection.execute("SELECT status,COUNT(*) FROM attempts GROUP BY status"))
        if counts != status["attempt_status_counts"]:
            raise ValueError("Collector attempt counts do not match its status")
        if connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] != status["stored_bar_count"]:
            raise ValueError("Collector stored-bar count mismatch")
        if connection.execute("SELECT COUNT(DISTINCT pair) FROM bars").fetchone()[0] != status["stored_pair_count"]:
            raise ValueError("Collector stored-pair count mismatch")
        seen_raw = set()
        for row in connection.execute("SELECT * FROM attempts ORDER BY id"):
            record = dict(row)
            record["params"] = _json(record["params"])
            record["observation"] = _json(record["observation"] or "{}")
            raw_hash = record["raw_sha256"]
            if raw_hash is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", raw_hash):
                    raise ValueError("Invalid raw object identity")
                compressed_path = directory / "raw" / (raw_hash + ".json.gz")
                guard()
                compressed_hash = digest(compressed_path)
                guard()
                with gzip.open(compressed_path, "rb") as handle:
                    raw = handle.read(MAX_RAW_BYTES + 1)
                if len(raw) > MAX_RAW_BYTES or hashlib.sha256(raw).hexdigest() != raw_hash:
                    raise ValueError("Raw response checksum/byte budget mismatch")
                if raw_hash not in seen_raw:
                    source["raw_objects"].append({"raw_sha256": raw_hash,
                                                  "gzip_sha256": compressed_hash, "bytes": len(raw)})
                    seen_raw.add(raw_hash)
                record["raw"] = raw
            else:
                record["raw"] = None
            record["universe_observations"] = [dict(item) for item in connection.execute(
                "SELECT pair,observed_utc FROM universe_observations WHERE attempt_id=?", (record["id"],))]
            stored = [dict(item) for item in connection.execute(
                "SELECT pair,timestamp,row_json,row_sha256 FROM bars WHERE attempt_id=?", (record["id"],))]
            for candle in stored:
                if hashlib.sha256(candle["row_json"].encode()).hexdigest() != candle["row_sha256"]:
                    raise ValueError("Stored candle checksum mismatch")
            record["stored_bars"] = stored
            source["verified_stored_bars"] += len(stored)
            yield record
        if source["verified_stored_bars"] != status["stored_bar_count"]:
            raise ValueError("Stored candle refers to an unknown request")
        guard()
        available = {p.name.removesuffix(".json.gz") for p in (directory / "raw").glob("*.json.gz")}
        source["orphan_raw_objects_not_admitted"] = sorted(available - seen_raw)
        written = instant(status["written_utc"])
        latest = connection.execute("SELECT MAX(received_utc) FROM attempts").fetchone()[0]
        if latest and written < instant(latest):
            raise ValueError("Capture status predates its completed requests")
    finally:
        connection.close()


def _pages(records, freeze, first_seen, counts):
    """Check request times, identities and complete rows without future aliases."""
    start = instant(freeze["protocol"]["start_utc"])
    server = None
    universe = None
    previous_request = None
    previous_received = None
    for record in records:
        endpoint, params = record["endpoint"], record["params"]
        if endpoint not in ENDPOINTS or set(params) != ENDPOINTS[endpoint]:
            raise ValueError("Unexpected collector endpoint or request parameters")
        requested = instant(record["requested_utc"])
        if (requested < start or (previous_request is not None and requested < previous_request)
                or (previous_received is not None and requested < previous_received)):
            raise ValueError("Capture request time precedes freeze or regresses")
        previous_request = requested
        received_text = record.get("received_utc")
        if not received_text:
            if record["status"] not in {"PENDING", "INTERRUPTED"} or record.get("stored_bars"):
                raise ValueError("Completed request has no receipt time")
            counts["incomplete_attempts"] += 1
            continue
        received = instant(received_text)
        if received < requested or received > EMBARGO_END + dt.timedelta(hours=48, seconds=1200):
            raise ValueError("Capture receipt time is outside its permitted chronology")
        previous_received = received
        if record["status"] != "SUCCESS":
            if record.get("stored_bars"):
                raise ValueError("Stored candles refer to an unsuccessful request")
            counts["failed_or_interrupted_attempts"] += 1
            if "committed candle changed" in (record.get("error") or "").lower():
                raise ValueError("Collector reported a committed-candle conflict")
            continue
        if record.get("http_status") != 200 or record.get("raw") is None:
            raise ValueError("Successful request lacks HTTP 200 raw provenance")
        payload = _json(record["raw"])
        if payload.get("error") or not isinstance(payload.get("result"), dict):
            raise ValueError("Successful response contains a provider error")
        if endpoint == "Time":
            now = _whole_number(payload["result"]["unixtime"], "Server time")
            observed = record["observation"]
            if instant(observed["server_utc"]).timestamp() != now:
                raise ValueError("Server time observation differs from raw response")
            skew = float(observed["clock_skew_seconds"])
            if not math.isfinite(skew) or abs(skew) > 90 or abs(now - received.timestamp() - skew) > 5:
                raise ValueError("Unreconciled public server/capture clock skew")
            server = (skew, received, now)
            universe = None
        elif endpoint == "AssetPairs":
            if server is None or requested < server[1] or params != {"aclass_base": "currency"}:
                raise ValueError("AssetPairs has no preceding public time boundary")
            universe = _online_pairs(payload)
            observations = record.get("universe_observations", [])
            by_key = {x["pair"]: instant(x["observed_utc"]) for x in observations}
            if len(by_key) != len(observations) or set(by_key) != {x[0] for x in universe.values()}:
                raise ValueError("AssetPairs observation rows differ from the raw online universe")
            for key, canonical in universe.values():
                observed = by_key[key]
                if observed < requested or observed > received:
                    raise ValueError("Universe observation is outside request/receipt times")
                if canonical in freeze["universe"]["pairs"]:
                    first_seen[canonical] = min(first_seen.get(canonical, observed.timestamp()), observed.timestamp())
        else:
            if server is None or universe is None or requested < server[1]:
                raise ValueError("OHLC request has no captured universe/time provenance")
            if params["interval"] != 5 or params["pair"] not in universe:
                raise ValueError("OHLC request is not a captured five-minute pair identity")
            since = _whole_number(params["since"], "OHLC since")
            if since % INTERVAL:
                raise ValueError("OHLC since is not aligned")
            api_key, canonical = universe[params["pair"]]
            result = payload["result"]
            keys = [key for key in result if key != "last"]
            if keys != [api_key] or not isinstance(result[api_key], list):
                raise ValueError("OHLC raw identity differs from its captured AssetPairs key")
            rows = result[api_key]
            counts["excluded_current_rows"] += min(1, len(rows))
            committed = rows[:-1]
            stamped = [_row(row)[0] for row in committed]
            if stamped != sorted(set(stamped)):
                raise ValueError("Committed rows are duplicated or unordered")
            observed_before = received.timestamp() + server[0]
            lower = (server[2] - 48*3600) // INTERVAL * INTERVAL
            if since < lower or since > observed_before:
                raise ValueError("OHLC since exceeds the frozen bounded 48-hour query window")
            cursor = _whole_number(result["last"], "OHLC cursor")
            if cursor > observed_before:
                raise ValueError("OHLC cursor exceeds its observed public time")
            raw_rows = {hashlib.sha256(encoded(row)).hexdigest(): row for row in committed}
            for stored in record.get("stored_bars", []):
                source_row = raw_rows.get(stored["row_sha256"])
                if (source_row is None or stored["pair"] != api_key
                        or stored["timestamp"] != _row(source_row)[0]
                        or stored["row_json"].encode() != encoded(source_row)):
                    raise ValueError("SQLite candle is not the exact referenced committed raw row")
                stamp = _row(source_row)[0]
                if (stamp < start.timestamp() or stamp >= EMBARGO_END.timestamp()
                        or stamp < since or stamp + INTERVAL > observed_before):
                    raise ValueError("SQLite evaluation candle violates its source time bounds")
            counts["successful_ohlc_pages"] += 1
            yield {"canonical": canonical, "rows": committed,
                   "observed_before": observed_before, "received_utc": received_text,
                   "received_epoch": received.timestamp(), "since": since,
                   "raw_sha256": record["raw_sha256"]}


def _build(capture_streams, freeze, output, *, input_class, sources, guard):
    import numpy as np
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Prepared output must be new; no existing tape is overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    first_eval = math.ceil(instant(freeze["protocol"]["start_utc"]).timestamp() / INTERVAL) * INTERVAL
    actual_freeze = instant(freeze["protocol"]["start_utc"]).timestamp()
    end = int(EMBARGO_END.timestamp())
    start = first_eval - WARMUP_FRAMES * INTERVAL
    frames = (end - start) // INTERVAL
    pairs = freeze["universe"]["pairs"]
    index = {p: i for i, p in enumerate(pairs)}
    counts = Counter()
    first_seen = {}
    first_warmup_page = {}
    with tempfile.TemporaryDirectory(prefix=".holdout_assembly_", dir=output.parent) as temporary:
        work = Path(temporary)
        staging = sqlite3.connect(work / "assembly.sqlite3")
        staging.execute("CREATE TABLE candles(pair TEXT,timestamp INTEGER,row_json TEXT,row_sha256 TEXT,raw_sha256 TEXT,received_epoch REAL,PRIMARY KEY(pair,timestamp))")
        for records in capture_streams:
            for page in _pages(records, freeze, first_seen, counts):
                canonical = page["canonical"]
                if canonical not in index:
                    counts["quarantined_extra_market_rows"] += len(page["rows"])
                    continue
                received = page["received_epoch"]
                first_page = first_warmup_page.get(canonical)
                page_identity = (received, page["raw_sha256"])
                if first_page is None or received < first_page[0]:
                    first_warmup_page[canonical] = page_identity
                elif received == first_page[0] and page["raw_sha256"] != first_page[1]:
                    raise ValueError("Ambiguous first warmup page at the same capture time")
                for row in page["rows"]:
                    stamp, _ = _row(row)
                    if stamp < page["since"] or stamp < start or stamp >= end:
                        counts["outside_frozen_tape_rows"] += 1
                        continue
                    if stamp + INTERVAL > page["observed_before"]:
                        counts["not_yet_complete_at_capture_rows"] += 1
                        continue
                    if stamp < first_eval:
                        if stamp + INTERVAL > actual_freeze:
                            counts["cross_freeze_warmup_rows_excluded"] += 1
                            continue
                    raw = encoded(row)
                    row_hash = hashlib.sha256(raw).hexdigest()
                    old = staging.execute("SELECT row_sha256,received_epoch FROM candles WHERE pair=? AND timestamp=?", (canonical, stamp)).fetchone()
                    if old:
                        if old[0] != row_hash:
                            raise ValueError("Conflicting previously committed candle across captures")
                        counts["identical_overlap_rows"] += 1
                        if received < old[1]:
                            staging.execute("UPDATE candles SET raw_sha256=?,received_epoch=? WHERE pair=? AND timestamp=?", (page["raw_sha256"], received, canonical, stamp))
                        continue
                    staging.execute("INSERT INTO candles VALUES(?,?,?,?,?,?)", (canonical, stamp, raw.decode(), row_hash, page["raw_sha256"], received))
        staging.commit()
        prepared = work / "prepared"
        prepared.mkdir()
        tape = np.lib.format.open_memmap(prepared / "ohlcv.npy", mode="w+", dtype="float64", shape=(frames, len(pairs), 6))
        tape[:] = np.nan
        per_pair = {p: {"warmup_rows": 0, "evaluation_rows": 0,
                        "first_observed_online_utc": (dt.datetime.fromtimestamp(first_seen[p], UTC).isoformat().replace("+00:00", "Z") if p in first_seen else None)} for p in pairs}
        for pair, stamp, row_json, raw_hash, received in staging.execute("SELECT pair,timestamp,row_json,raw_sha256,received_epoch FROM candles ORDER BY timestamp,pair"):
            if stamp < first_eval and (received, raw_hash) != first_warmup_page[pair]:
                counts["later_page_only_warmup_rows_excluded"] += 1
                continue
            if stamp >= first_eval and (pair not in first_seen or stamp < first_seen[pair]):
                counts["pre_first_observation_evaluation_rows_excluded"] += 1
                continue
            _, native = _row(_json(row_json))
            tape[(stamp - start) // INTERVAL, index[pair], :] = native
            per_pair[pair]["warmup_rows" if stamp < first_eval else "evaluation_rows"] += 1
        tape.flush()
        del tape
        staging.close()
        observed_rows = sum(x["warmup_rows"] + x["evaluation_rows"] for x in per_pair.values())
        source_hashes = {"frozen_protocol": FROZEN_PROTOCOL_SHA256,
                         "frozen_candidates": {k: v["sha256"] for k, v in freeze["verified_candidates"].items()},
                         "captures": sources}
        metadata = {"schema": 1, "start": start, "end": end,
                    "interval_seconds": INTERVAL, "pairs": pairs,
                    "symbols": [p.replace("-", "") for p in pairs],
                    "input_class": input_class,
                    "uses_market_observations": input_class == "prospective_capture",
                    "source_hashes": source_hashes, "observed_rows": observed_rows}
        (prepared / "metadata.json").write_bytes(encoded(metadata))
        receipt = {"schema": ASSEMBLY_SCHEMA, "input_class": input_class,
                   "uses_market_observations": input_class == "prospective_capture",
                   "performance_claim": False, "strategy_evaluated": False,
                   "frozen_protocol_sha256": FROZEN_PROTOCOL_SHA256,
                   "first_evaluation_timestamp": first_eval,
                   "actual_freeze_utc": freeze["protocol"]["start_utc"],
                   "end_utc": freeze["protocol"]["end_utc"],
                   "warmup_frames": WARMUP_FRAMES, "evaluation_frames": frames-WARMUP_FRAMES,
                   "shape": [frames, len(pairs), 6], "dtype": "float64",
                   "prepared_sha256": {name: digest(prepared / name) for name in ("metadata.json", "ohlcv.npy")},
                   "source_hashes": source_hashes, "counts": dict(counts), "per_pair": per_pair,
                   "missing_grid_slots": frames * len(pairs) - observed_rows,
                   "missing_policy": "all-NaN absent rows; no forward fill; fixed identities retained",
                   "first_observation_policy": "evaluation candle start at or after first captured online AssetPairs observation; historical warmup separate",
                   "warmup_policy": "first successful raw OHLC page per fixed pair only; complete bars closing at or before actual freeze",
                   "limitations": [
                       "Fixed membership was known before freeze; captured online-status admission is later and never backdated.",
                       "Earliest captured online admission is not a reconstruction of listing, delisting or intervening offline intervals.",
                       "Absent bars may be no-trade intervals or unavailable data; all remain absent and untradeable.",
                       "Prepared self-consistent hashes do not independently authenticate external captures or prove raw-to-tape semantics; use assembler output and independent source review.",
                   ]}
        (prepared / "assembly-receipt.json").write_bytes(encoded(receipt))
        guard()
        prepared.rename(output)
        return receipt


def assemble_captures(capture_dirs, repo_root, output):
    _embargo_guard()
    freeze = _freeze(repo_root)
    directories = [Path(p).resolve() for p in capture_dirs]
    if not directories or len(directories) != len(set(directories)):
        raise ValueError("Unique explicit capture directories are required")
    sources = []
    streams = [_directory_records(path, freeze, _embargo_guard, sources) for path in directories]
    return _build(streams, freeze, output, input_class="prospective_capture", sources=sources, guard=_embargo_guard)


def assemble_fixture_captures(captures, repo_root, output, *, fixture_now):
    """Mechanics-only fixture API: in-memory records, no capture path argument."""
    if not isinstance(fixture_now, dt.datetime) or fixture_now.tzinfo is None or fixture_now < EMBARGO_END:
        raise ValueError("Constructed fixture clock must reach the simulated embargo end")
    freeze = _freeze(repo_root)
    streams, sources = [], []
    for capture in captures:
        if (not isinstance(capture, dict) or capture.get("input_class") != "constructed_fixture"
                or capture.get("uses_market_observations") is not False
                or set(capture) != {"input_class", "uses_market_observations", "records"}):
            raise ValueError("Fixture assembly accepts explicitly constructed in-memory records only")
        records = []
        for original in capture["records"]:
            record = dict(original)
            payload = record.pop("fixture_payload", None)
            if payload is not None:
                record["raw"] = encoded(payload)
                record["raw_sha256"] = hashlib.sha256(record["raw"]).hexdigest()
            elif record.get("raw") is not None:
                raise ValueError("Fixture records cannot inject externally read raw bytes")
            records.append(record)
        streams.append(iter(records))
        sources.append({"input_class": "constructed_fixture", "record_count": len(records),
                        "fixture_records_sha256": hashlib.sha256(encoded(capture)).hexdigest()})
    if not streams:
        raise ValueError("At least one constructed fixture capture is required")
    return _build(streams, freeze, output, input_class="constructed_fixture", sources=sources, guard=lambda: None)


def validate_prepared(repo_root, prepared):
    _embargo_guard()
    freeze = _freeze(repo_root)
    prepared = Path(prepared)
    receipt = _json(_read(prepared / "assembly-receipt.json", _embargo_guard))
    if (receipt.get("schema") != ASSEMBLY_SCHEMA or receipt.get("input_class") != "prospective_capture"
            or receipt.get("uses_market_observations") is not True
            or receipt.get("frozen_protocol_sha256") != FROZEN_PROTOCOL_SHA256):
        raise ValueError("A source-pinned prospective capture assembly is required")
    raw_metadata = _read(prepared / "metadata.json", _embargo_guard)
    if hashlib.sha256(raw_metadata).hexdigest() != receipt["prepared_sha256"]["metadata.json"]:
        raise ValueError("Prepared metadata checksum mismatch")
    metadata = _json(raw_metadata)
    expected_candidates = {k: v["sha256"] for k, v in freeze["verified_candidates"].items()}
    if (metadata.get("source_hashes", {}).get("frozen_protocol") != FROZEN_PROTOCOL_SHA256
            or metadata["source_hashes"].get("frozen_candidates") != expected_candidates):
        raise ValueError("Prepared provenance is not bound to the trusted frozen source pins")
    _embargo_guard()
    if digest(prepared / "ohlcv.npy") != receipt["prepared_sha256"]["ohlcv.npy"]:
        raise ValueError("Prepared tape checksum mismatch")
    first_eval = math.ceil(instant(freeze["protocol"]["start_utc"]).timestamp() / INTERVAL) * INTERVAL
    start, end = first_eval - WARMUP_FRAMES * INTERVAL, int(EMBARGO_END.timestamp())
    frames = (end - start) // INTERVAL
    expected_shape = [frames, 619, 6]
    if (metadata.get("start") != start or metadata.get("end") != end
            or metadata.get("interval_seconds") != INTERVAL
            or metadata.get("pairs") != freeze["universe"]["pairs"]
            or metadata.get("symbols") != [p.replace("-", "") for p in freeze["universe"]["pairs"]]
            or metadata.get("input_class") != "prospective_capture" or metadata.get("uses_market_observations") is not True
            or receipt.get("shape") != expected_shape or receipt.get("dtype") != "float64"
            or receipt.get("warmup_frames") != WARMUP_FRAMES or receipt.get("evaluation_frames") != frames-WARMUP_FRAMES
            or receipt.get("first_evaluation_timestamp") != first_eval
            or receipt.get("actual_freeze_utc") != freeze["protocol"]["start_utc"]
            or receipt.get("end_utc") != freeze["protocol"]["end_utc"]
            or receipt.get("source_hashes") != metadata.get("source_hashes")):
        raise ValueError("Prepared input geometry/source identity differs from the frozen full window")
    import numpy as np
    _embargo_guard()
    tape = np.load(prepared / "ohlcv.npy", mmap_mode="r", allow_pickle=False)
    if list(tape.shape) != expected_shape or tape.dtype != np.dtype("float64"):
        raise ValueError("Native tape shape/dtype mismatch")
    for left in range(0, frames, 512):
        chunk = tape[left:left+512]
        absent = np.isnan(chunk).all(axis=2)
        present = np.isfinite(chunk).all(axis=2)
        if not (absent | present).all():
            raise ValueError("Native tape contains partial or nonfinite candles")
        rows = chunk[present]
        if len(rows) and ((rows[:, :4] <= 0).any() or (rows[:, 4:] < 0).any()
                          or (rows[:, 5] != np.floor(rows[:, 5])).any()
                          or (rows[:, 1] < rows[:, [0, 2, 3]].max(axis=1)).any()
                          or (rows[:, 2] > rows[:, [0, 1, 3]].min(axis=1)).any()):
            raise ValueError("Prepared candles violate native OHLC domains")
    if not np.isnan(tape[WARMUP_FRAMES-1]).all():
        raise ValueError("Warmup candle crossing the actual freeze must remain absent")
    del tape
    return metadata, receipt, frames


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assembly = commands.add_parser("assemble")
    assembly.add_argument("--capture-dir", action="append", required=True)
    assembly.add_argument("--repo-root", required=True)
    assembly.add_argument("--output", required=True)
    verification = commands.add_parser("verify")
    verification.add_argument("--repo-root", required=True)
    verification.add_argument("--prepared", required=True)
    args = parser.parse_args(argv)
    if args.command == "assemble":
        receipt = assemble_captures(args.capture_dir, args.repo_root, args.output)
        print(json.dumps({"input_class": receipt["input_class"], "prepared": args.output,
                          "strategy_evaluated": False}))
    else:
        _, receipt, frames = validate_prepared(args.repo_root, args.prepared)
        print(json.dumps({"verified": True, "frames": frames, "strategy_evaluated": False,
                          "frozen_protocol_sha256": receipt["frozen_protocol_sha256"]}))


if __name__ == "__main__":
    main()
