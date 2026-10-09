#!/usr/bin/env python3
"""Read-only prospective raw Kraken capture; no strategy or trading imports.

Daily 48-hour overlapping public OHLC pages, with immutable source freeze,
raw-byte hashes, resumable local cursors and explicit failed/gapped captures.
Console output is operational metadata only. SQLite rows stay sealed until the
protocol's evaluation_after_utc. Only Time, AssetPairs and OHLC GETs exist.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "spectra-prospective-holdout-v1"
PUBLIC = "https://api.kraken.com/0/public/"
ENDPOINTS = {"Time": set(), "AssetPairs": {"aclass_base"},
             "OHLC": {"pair", "interval", "since"}}
INTERVAL = 300
MAX_RESPONSE = 8_000_000


class BudgetExhausted(RuntimeError):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(value):
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def instant(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("Protocol timestamps must be explicit UTC")
    return parsed


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def public_url(endpoint, params):
    if endpoint not in ENDPOINTS or set(params) - ENDPOINTS[endpoint]:
        raise ValueError("Only explicitly allowed public GET parameters are permitted")
    return PUBLIC + endpoint + ("?" + urllib.parse.urlencode(params) if params else "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public capture does not follow redirects")


def public_get(endpoint, params, timeout=12):
    url = public_url(endpoint, params)
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "Spectra-prospective-public-holdout/1"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("Public response exceeds the fixed byte budget")
            return response.status, raw, response.headers.get("Date")
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_RESPONSE), error.headers.get("Date")


def validate_protocol(protocol, source_root):
    if protocol.get("schema") != SCHEMA or protocol.get("status") != "FROZEN":
        raise ValueError("A real source-pinned FROZEN protocol is required")
    start, end = instant(protocol["start_utc"]), instant(protocol["end_utc"])
    if start >= end or protocol["evaluation_after_utc"] != protocol["end_utc"]:
        raise ValueError("Invalid sealed holdout window")
    expected = {"interval_minutes": 5, "lookback_hours": 48, "pace_seconds": 1.1,
                "max_requests": 800, "max_seconds": 1200,
                "universe": "fresh_currency_usd_each_capture"}
    if protocol.get("collection") != expected:
        raise ValueError("Capture settings differ from the declared bounded protocol")
    candidates = protocol.get("candidates", [])
    if not candidates or len({item["name"] for item in candidates}) != len(candidates):
        raise ValueError("Unique financial candidate source pins are required")
    pins = candidates + [protocol["collector"]]
    root = Path(source_root).resolve()
    for item in pins:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Pinned sources must be relative to the supplied source root")
        if sha(path.read_bytes()) != item["sha256"]:
            raise ValueError("Frozen source hash mismatch: " + item["name"])
    return start, end


def freeze(template_path, output, source_root, candidates, end_utc=None, now=None):
    output, source_root = Path(output), Path(source_root).resolve()
    if (output / "frozen-protocol.json").exists():
        raise ValueError("Existing freeze cannot be overwritten")
    protocol = json.loads(Path(template_path).read_bytes())
    if protocol.get("schema") != SCHEMA or protocol.get("status") != "DRAFT_NOT_STARTED":
        raise ValueError("Use the unstarted protocol template for an initial freeze")
    start = now or utcnow()
    protocol.update(status="FROZEN", start_utc=iso(start),
                    end_utc=end_utc or protocol["end_utc"], candidates=[])
    protocol["evaluation_after_utc"] = protocol["end_utc"]
    for label, path in candidates:
        resolved = (source_root / path).resolve()
        relative = resolved.relative_to(source_root).as_posix()
        protocol["candidates"].append({"name": label, "path": relative,
                                       "sha256": sha(resolved.read_bytes())})
    own = Path(__file__).resolve()
    protocol["collector"] = {"name": "public_capture_collector",
                             "path": own.relative_to(source_root).as_posix(),
                             "sha256": sha(own.read_bytes())}
    validate_protocol(protocol, source_root)
    raw = encoded(protocol)
    atomic(output / "frozen-protocol.json", raw)
    receipt = {"schema": SCHEMA, "protocol_sha256": sha(raw),
               "frozen_utc": iso(start), "capture_started": False,
               "first_eligible_bar_utc": iso(dt.datetime.fromtimestamp(
                   math.ceil(start.timestamp() / INTERVAL) * INTERVAL, dt.timezone.utc))}
    atomic(output / "freeze-receipt.json", encoded(receipt))
    return protocol


class Store:
    def __init__(self, output, protocol_raw):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        frozen = self.output / "frozen-protocol.json"
        if frozen.exists() and frozen.read_bytes() != protocol_raw:
            raise ValueError("Output belongs to a different frozen protocol")
        if not frozen.exists():
            atomic(frozen, protocol_raw)
            atomic(self.output / "freeze-receipt.json", encoded({
                "schema": SCHEMA, "protocol_sha256": sha(protocol_raw),
                "frozen_utc": json.loads(protocol_raw)["start_utc"],
                "capture_started": False}))
        receipt = json.loads((self.output / "freeze-receipt.json").read_bytes())
        if receipt["protocol_sha256"] != sha(protocol_raw):
            raise ValueError("Freeze receipt hash mismatch")
        self.db = sqlite3.connect(self.output / "capture.sqlite3")
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS attempts(
              id INTEGER PRIMARY KEY, endpoint TEXT, params TEXT,
              requested_utc TEXT, received_utc TEXT, status TEXT,
              http_status INTEGER, http_date TEXT, raw_sha256 TEXT, error TEXT,
              observation TEXT);
            CREATE TABLE IF NOT EXISTS bars(
              pair TEXT, timestamp INTEGER, row_json TEXT, row_sha256 TEXT,
              attempt_id INTEGER, PRIMARY KEY(pair,timestamp));
            CREATE TABLE IF NOT EXISTS cursors(pair TEXT PRIMARY KEY, since INTEGER);
            CREATE TABLE IF NOT EXISTS universe_observations(
              pair TEXT, observed_utc TEXT, attempt_id INTEGER,
              PRIMARY KEY(pair,attempt_id));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        with self.db:
            self.db.execute("UPDATE attempts SET status='INTERRUPTED', error='prior process ended before receipt completion' WHERE status='PENDING'")

    def close(self):
        self.db.close()

    def begin(self, endpoint, params):
        public_url(endpoint, params)
        with self.db:
            cursor = self.db.execute("INSERT INTO attempts(endpoint,params,requested_utc,status) VALUES(?,?,?,'PENDING')",
                                     (endpoint, encoded(params).decode(), iso(utcnow())))
        return cursor.lastrowid

    def raw(self, raw):
        digest = sha(raw)
        path = self.output / "raw" / (digest + ".json.gz")
        if path.exists():
            if sha(gzip.decompress(path.read_bytes())) != digest:
                raise ValueError("Existing raw response checksum mismatch")
        else:
            atomic(path, gzip.compress(raw, mtime=0))
        return digest

    def finish(self, request_id, status, http_status=None, raw=None, error=None,
               http_date=None, observation=None):
        digest = self.raw(raw) if raw is not None else None
        with self.db:
            self.db.execute("UPDATE attempts SET received_utc=?,status=?,http_status=?,http_date=?,raw_sha256=?,error=?,observation=? WHERE id=?",
                            (iso(utcnow()), status, http_status, http_date, digest, error,
                             encoded(observation or {}).decode(), request_id))

    def ingest(self, request_id, pair, payload, since, start, end, committed_before):
        result = payload["result"]
        keys = [key for key in result if key != "last"]
        if len(keys) != 1 or not isinstance(result[keys[0]], list):
            raise ValueError("Unexpected OHLC result shape")
        if keys[0] != pair:
            raise ValueError("OHLC identity differs from the captured AssetPairs key")
        rows = result[keys[0]]
        # Kraken documents the last array entry as current/uncommitted.
        committed = rows[:-1]
        selected, stamps = [], []
        for row in committed:
            if len(row) != 8 or int(row[0]) % INTERVAL:
                raise ValueError("Malformed five-minute OHLC row")
            stamp = int(row[0])
            if stamp + INTERVAL > committed_before:
                continue
            if stamp >= start.timestamp() and stamp < end.timestamp() and stamp >= since:
                selected.append(row)
                stamps.append(stamp)
        if stamps != sorted(set(stamps)):
            raise ValueError("OHLC timestamps are duplicated or unordered")
        # Absence cannot be labelled a provider failure versus a no-trade bar.
        left = max(math.ceil(start.timestamp()/INTERVAL)*INTERVAL,
                   math.ceil(since/INTERVAL)*INTERVAL)
        right = min(math.floor(committed_before/INTERVAL)*INTERVAL,
                    math.ceil(end.timestamp()/INTERVAL)*INTERVAL)
        expected = max(0, (right-left)//INTERVAL)
        reported_cursor = int(result["last"])
        if reported_cursor < 0 or reported_cursor > committed_before:
            raise ValueError("OHLC cursor is outside the observed time boundary")
        # A newly frozen window can begin after the latest committed candle.
        # A sparse market can also return no new rows. Neither permits cursor
        # regression nor justifies inventing missing candles.
        cursor = max(since, reported_cursor)
        with self.db:
            for row in selected:
                raw = encoded(row)
                existing = self.db.execute("SELECT row_sha256 FROM bars WHERE pair=? AND timestamp=?", (pair,int(row[0]))).fetchone()
                if existing and existing[0] != sha(raw):
                    raise ValueError("Previously committed candle changed; retain conflict and stop this pair")
                self.db.execute("INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?)",
                                (pair,int(row[0]),raw.decode(),sha(raw),request_id))
            self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(pair) DO UPDATE SET since=MAX(since,excluded.since)", (pair,cursor))
        return {"window_committed_rows": len(selected), "expected_grid_slots": expected,
                "unobserved_grid_slots": max(0,expected-len(stamps)),
                "gap_interpretation": "unobserved; may be no-trade or unavailable; never filled",
                "returned_rows":len(rows), "excluded_current_rows":min(1,len(rows)),
                "first_eligible_timestamp": min(stamps) if stamps else None,
                "last_eligible_timestamp": max(stamps) if stamps else None,
                "reported_cursor":reported_cursor,"cursor_after":cursor}

    def status(self, extra=None):
        values = {"schema": SCHEMA, "written_utc":iso(utcnow()),
                  "strategy_evaluated":False, "financial_outcomes_printed":False,
                  "attempt_status_counts": dict(self.db.execute("SELECT status,COUNT(*) FROM attempts GROUP BY status")),
                  "stored_bar_count":self.db.execute("SELECT COUNT(*) FROM bars").fetchone()[0],
                  "stored_pair_count":self.db.execute("SELECT COUNT(DISTINCT pair) FROM bars").fetchone()[0]}
        values.update(extra or {})
        self.db.commit()
        values["sqlite_sha256"] = sha((self.output / "capture.sqlite3").read_bytes())
        atomic(self.output / "status.json", encoded(values))
        return values


def usd_pairs(payload):
    pairs = []
    for key, details in payload["result"].items():
        if (details.get("quote") in {"ZUSD", "USD"}
                and details.get("aclass_base") == "currency"
                and details.get("status") == "online"
                and "." not in key):
            symbol = details.get("altname",key)
            if not re.fullmatch(r"[A-Z0-9]{2,40}",symbol):
                raise ValueError("Unsafe asset pair symbol")
            pairs.append((key,symbol))
    if not pairs:
        raise ValueError("No online USD spot pairs in public universe response")
    return sorted(pairs)


def poll(protocol_path, output, source_root, max_requests=800, max_seconds=1200,
         fetch=public_get, clock=utcnow, sleeper=time.sleep):
    protocol_raw = Path(protocol_path).read_bytes()
    protocol = json.loads(protocol_raw)
    start,end = validate_protocol(protocol,source_root)
    if max_requests < 2 or max_requests > 800 or max_seconds <= 0 or max_seconds > 1200:
        raise ValueError("Execution budget may be reduced, never expanded")
    store = Store(output,protocol_raw)
    commenced, count, last, failures = time.monotonic(),0,None,0
    server_now = None

    def request(endpoint,params):
        nonlocal count,last,failures
        if count >= max_requests or time.monotonic()-commenced >= max_seconds:
            raise BudgetExhausted("Public capture budget reached")
        if last is not None:
            sleeper(max(0,1.1-(time.monotonic()-last)))
        last=time.monotonic()
        count+=1
        request_id=store.begin(endpoint,params)
        raw,status,date=None,None,None
        try:
            status,raw,date=fetch(endpoint,params)
            store.raw(raw) # Save bytes even if parsing or schema validation fails.
            if status != 200:
                raise ValueError("Public HTTP response: "+str(status))
            value=json.loads(raw)
            if value.get("error") or not isinstance(value.get("result"),dict):
                raise ValueError("Kraken public response error or malformed result")
            return request_id,value,raw,status,date
        except Exception as error:
            failures+=1
            store.finish(request_id,"FAILED",status,raw,type(error).__name__+": "+str(error),date)
            raise

    extra={"protocol_sha256":sha(protocol_raw), "complete_universe_attempt":False,
           "capture_started":False}
    try:
        if clock() < start:
            raise ValueError("Capture cannot precede actual source freeze")
        if clock() > end+dt.timedelta(hours=48):
            raise ValueError("Window ended beyond bounded final backfill allowance")
        rid,value,raw,status,date=request("Time",{})
        try:
            server_now=int(value["result"]["unixtime"])
        except Exception as error:
            failures+=1
            store.finish(rid,"FAILED",status,raw,type(error).__name__+": "+str(error),date)
            raise
        skew=server_now-clock().timestamp()
        store.finish(rid,"SUCCESS",status,raw,http_date=date,
                     observation={"server_utc":iso(dt.datetime.fromtimestamp(server_now,dt.timezone.utc)),"clock_skew_seconds":round(skew,3)})
        if abs(skew)>90:
            raise ValueError("Clock skew exceeds 90 seconds; stop before market capture")
        rid,value,raw,status,date=request("AssetPairs",{"aclass_base":"currency"})
        try:
            pairs=usd_pairs(value)
        except Exception as error:
            failures+=1
            store.finish(rid,"FAILED",status,raw,str(error),date)
            raise
        observed=iso(clock())
        with store.db:
            for key,symbol in pairs:
                store.db.execute("INSERT INTO universe_observations VALUES(?,?,?)", (key,observed,rid))
        store.finish(rid,"SUCCESS",status,raw,http_date=date,
                     observation={"online_usd_currency_pairs":len(pairs),"universe_rule":protocol["collection"]["universe"]})
        extra.update(capture_started=True,universe_pairs=len(pairs),
                     universe_snapshot_sha256=sha(raw),universe_snapshot_received_utc=observed,
                     evaluation_requires_first_observed_universe_gate=True)
        attempted=0
        for key,symbol in pairs:
            # Preserve a bounded raw pre-freeze prefix for separately labelled
            # 24h warmup; ingest still admits no pre-freeze evaluation rows.
            lower=(server_now-48*3600)//INTERVAL*INTERVAL
            previous=store.db.execute("SELECT since FROM cursors WHERE pair=?",(key,)).fetchone()
            since=max(lower,previous[0]) if previous else lower
            try:
                rid,value,raw,status,date=request("OHLC",{"pair":symbol,"interval":5,"since":since})
            except BudgetExhausted:
                extra["budget_exhausted"]=True
                break
            except Exception:
                attempted+=1
                continue
            try:
                observation=store.ingest(rid,key,value,since,start,end,
                                         int(clock().timestamp()+skew))
                store.finish(rid,"SUCCESS",status,raw,http_date=date,observation=observation)
            except Exception as error:
                failures+=1
                store.finish(rid,"FAILED",status,raw,type(error).__name__+": "+str(error),date)
            attempted+=1
        extra.update(attempted_pairs=attempted,complete_universe_attempt=attempted==len(pairs))
    except Exception as error:
        extra["capture_error"]=type(error).__name__+": "+str(error)
    finally:
        extra.update(requests_this_invocation=count,failures_this_invocation=failures)
        result=store.status(extra)
        store.close()
    return result


def verify(output):
    """Integrity/metadata inspection only; no strategy returns or winners."""
    output=Path(output)
    protocol_raw=(output/"frozen-protocol.json").read_bytes()
    receipt=json.loads((output/"freeze-receipt.json").read_bytes())
    if receipt["protocol_sha256"] != sha(protocol_raw):
        raise ValueError("Freeze receipt hash mismatch")
    connection=sqlite3.connect("file:"+str(output/"capture.sqlite3")+"?mode=ro",uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Capture database is corrupt")
        referenced={item[0] for item in connection.execute("SELECT raw_sha256 FROM attempts WHERE raw_sha256 IS NOT NULL")}
        hashes={path.name.removesuffix(".json.gz") for path in (output/"raw").glob("*.json.gz")}
        if not referenced.issubset(hashes):
            raise ValueError("A referenced raw response object is missing")
        for digest in hashes:
            if sha(gzip.decompress((output/"raw"/(digest+".json.gz")).read_bytes())) != digest:
                raise ValueError("Raw response checksum mismatch")
        for row,digest in connection.execute("SELECT row_json,row_sha256 FROM bars"):
            if sha(row.encode()) != digest:
                raise ValueError("Stored candle checksum mismatch")
        status=json.loads((output/"status.json").read_bytes())
        if status["sqlite_sha256"] != sha((output/"capture.sqlite3").read_bytes()):
            raise ValueError("Final capture database checksum mismatch")
        return {"integrity":"PASS","raw_response_objects":len(hashes),
                "orphan_raw_objects":len(hashes-referenced),"strategy_evaluated":False}
    finally:
        connection.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    freezer=commands.add_parser("freeze")
    freezer.add_argument("--protocol",required=True)
    freezer.add_argument("--output",required=True)
    freezer.add_argument("--source-root",required=True)
    freezer.add_argument("--candidate",action="append",required=True,help="NAME=relative/path")
    freezer.add_argument("--end")
    freezer.add_argument("--protocolout")
    collector=commands.add_parser("poll")
    collector.add_argument("--protocol",required=True)
    collector.add_argument("--output",required=True)
    collector.add_argument("--source-root",required=True)
    collector.add_argument("--max-requests",type=int,default=800)
    collector.add_argument("--max-seconds",type=int,default=1200)
    integrity=commands.add_parser("verify")
    integrity.add_argument("--output",required=True)
    args=parser.parse_args(argv)
    if args.command=="freeze":
        candidates=[item.split("=",1) for item in args.candidate]
        freeze(args.protocol,args.output,args.source_root,candidates,args.end)
        if args.protocolout:
            target=Path(args.protocolout)
            if target.exists():
                raise ValueError("--protocolout must not overwrite an existing file")
            atomic(target,(Path(args.output)/"frozen-protocol.json").read_bytes())
        print(json.dumps({"status":"FROZEN","capture_started":False}))
        return 0
    if args.command=="verify":
        print(json.dumps(verify(args.output)))
        return 0
    # The fcntl lock expires with the process; no stale lock authorizes races.
    import fcntl
    destination=Path(args.output)
    destination.mkdir(parents=True,exist_ok=True)
    with (destination/"capture.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=poll(args.protocol,args.output,args.source_root,args.max_requests,args.max_seconds)
    print(json.dumps(result,sort_keys=True))
    return 0 if (result["complete_universe_attempt"] and result["failures_this_invocation"]==0) else 1


if __name__=="__main__":
    raise SystemExit(main())
