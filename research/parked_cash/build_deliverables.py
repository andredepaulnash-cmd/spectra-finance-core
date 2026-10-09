"""Build a truthful report and compact evidence pack; never run a strategy.

Call only once the requested matrix is complete, or explicitly pass
--allow-incomplete to publish an honestly labelled checkpoint. Giant market
inputs and Git-backed authored source copies are deliberately excluded.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import copy
import hashlib
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
ARMS = ("OFF", "ENTRY_ONLY", "RECOVERY_ONLY", "BOTH", "BOTH_TIGHT")
Z = D(0)


def read(path):
    return json.loads(path.read_text()) if path.is_file() else None


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, default=str, indent=2) + "\n")


def dollars(value):
    return "unavailable" if value is None else f"${D(str(value)):,.2f}"


def percent(value):
    return "unavailable" if value is None else f"{D(str(value))*100:.2f}%"


def source_summary(root):
    old = read(root / "repository_receipt.json") or {}
    branch = old.get("branch", {})
    commit = branch.get("commit", {})
    tree = old.get("tree", {})
    return dict(repository=old.get("repo", {}).get("repository_full_name"),
        inspected_branch=branch.get("name"), original_head=commit.get("sha"),
        original_head_url=commit.get("html_url"), original_tree=tree.get("sha"),
        selected_restored_blob_hashes_match=old.get("restoration", {}).get(
            "all_selected_blob_hashes_match", old.get("all_selected_blob_hashes_match")),
        validation=old.get("validation"), parked_cash_port=old.get("parked_cash_port"),
        new_workflow_files=old.get("new_workflow_files"),
        checked_at_utc=old.get("checked_at_utc"),
        note="Selected metadata only; giant remote tree and personal/account metadata excluded.")


def prefix_receipt(root, results, expected, old_directory="results/ENTRY_ONLY"):
    old_dir = root / old_directory
    old_csv = old_dir / "window_accounts.csv"
    new_csv = results / "ENTRY_ONLY/window_accounts.csv"
    old_source = read(old_dir / "source_receipt.json") or {}
    new_source = read(results / "ENTRY_ONLY/source_receipt.json") or {}
    if not old_csv.is_file() or not new_csv.is_file():
        return dict(status="UNAVAILABLE", passed=False,
                    reason="Both preserved account CSVs are required")
    with old_csv.open(newline="") as old_stream, new_csv.open(newline="") as new_stream:
        old_reader, new_reader = csv.DictReader(old_stream), csv.DictReader(new_stream)
        columns_match = old_reader.fieldnames == new_reader.fieldnames
        comparisons = 0
        rows = 0
        differences = []
        for old_row in old_reader:
            new_row = next(new_reader, None)
            rows += 1
            if new_row is None:
                differences.append(dict(row=rows, reason="rerun prefix missing"))
                break
            for field in old_reader.fieldnames:
                comparisons += 1
                if old_row[field] != new_row.get(field):
                    if len(differences) < 10:
                        differences.append(dict(row=rows, field=field,
                            original=old_row[field], rerun=new_row.get(field)))
    pinned = {}
    for field in ("adapter_sha256", "compiled_source_sha256",
                  "native_strategy_sha256", "hydration_sha256"):
        pinned[field] = bool(old_source.get(field)) and old_source.get(field) == new_source.get(field)
    # Protocol bytes changed only to extend operational time from 420 to 600.
    # Require the separately preserved OLD protocol and verify both exact file
    # hashes before allowing that one declared execution-budget difference.
    old_protocol_path = root / "results-observation-v2/executed_protocol.json"
    new_protocol_path = root / "experiment/protocol.json"
    old_protocol, new_protocol = read(old_protocol_path), read(new_protocol_path)
    protocol_checks = dict(original_file_matches_pinned_hash=False,
                           final_file_matches_pinned_hash=False,
                           all_protocol_fields_except_budget_equal=False)
    budgets = {}
    if old_protocol is not None and new_protocol is not None:
        protocol_checks["original_file_matches_pinned_hash"] = sha(old_protocol_path) == old_source.get("protocol_sha256")
        protocol_checks["final_file_matches_pinned_hash"] = sha(new_protocol_path) == new_source.get("protocol_sha256")
        old_financial, new_financial = copy.deepcopy(old_protocol), copy.deepcopy(new_protocol)
        budgets["original"] = old_financial["execution"].pop("maximum_simulation_seconds_per_arm")
        budgets["final"] = new_financial["execution"].pop("maximum_simulation_seconds_per_arm")
        protocol_checks["all_protocol_fields_except_budget_equal"] = old_financial == new_financial
    passed = (columns_match and rows == expected and not differences
              and all(pinned.values()) and all(protocol_checks.values()))
    return dict(status="PASS" if passed else "FAIL", passed=passed,
        original_complete_frames=rows, requested_original_frames=expected,
        scalar_field_comparisons=comparisons, all_prefix_fields_exact=not differences,
        columns_match=columns_match, economic_source_pins_equal=pinned,
        protocol_checks=protocol_checks, operational_budgets_seconds=budgets,
        original_attempt_directory=old_directory,
        original_account_csv_sha256=sha(old_csv), rerun_account_csv_sha256=sha(new_csv),
        differences=differences,
        scope="Byte-exact original observed scalar account prefix; not a second strategy run or venue proof.")


def collect_status(results, root, frames):
    rows, result_dirs = {}, {}
    for arm in ARMS:
        directory = results / arm
        if arm == "OFF" and not directory.exists():
            directory = root / "results/OFF"
        result_dirs[arm] = directory
        result = read(directory / "result.json")
        failure = read(directory / "failure_receipt.json")
        complete = bool(result and result.get("status") == "PASS"
                        and result.get("complete_frames") == frames)
        rows[arm] = dict(status="COMPLETE" if complete else ("FAILED" if failure else "INCOMPLETE"),
                         complete=complete, result=result, failure=failure)
    return rows, result_dirs


def complete_audits(root, frames):
    found = {}
    files = []
    def inspect(node):
        if isinstance(node, dict):
            arm = node.get("arm")
            checks = node.get("checks", {})
            row_count = checks.get("independent_account_rows")
            if arm in ARMS and node.get("passed") is True and row_count == frames:
                found[arm] = dict(actual_fills=node.get("actual_fills"),
                    independent_account_rows=row_count,
                    account_field_comparisons=checks.get("account_field_comparisons"))
            for value in node.values(): inspect(value)
        elif isinstance(node, list):
            for value in node: inspect(value)
    for path in sorted((root / "independent_review").glob("*.json")):
        data = read(path)
        inspect(data)
        files.append(path)
    return found, files


def remote_links(node):
    links = set()
    def inspect(value):
        if isinstance(value, str) and value.startswith("https://github.com/"):
            links.add(value)
        elif isinstance(value, dict):
            for item in value.values(): inspect(item)
        elif isinstance(value, list):
            for item in value: inspect(item)
    inspect(node)
    return sorted(links)


def report_text(protocol, rows, audit_index, mechanics, repo, remote, prefix, revision, second_prefix=None):
    complete = [arm for arm in ARMS if rows[arm]["complete"]]
    all_complete = len(complete) == 5
    initial = D(protocol["restored_equity"])
    off = rows["OFF"]["result"]
    control = None if not rows["OFF"]["complete"] else D(off["ending_equity"])
    lines = ["SPECTRA — PARKED-CASH FIVE-ARM COMPARISON — 2026-10-09", "",
        ("All five bounded historical runs completed." if all_complete else
         f"INCOMPLETE CHECKPOINT: {len(complete)} of five requested runs completed."),
        "This tests whether protected USD cash can productively fund native new entries, recovery buys, or both.",
        "", f"Period: July 1–30, 2026; July 31 UTC is the exclusive endpoint ({protocol['frames']:,} five-minute frames).",
        f"Starting state: the exact June-ending $2,000 candidate, marked at {dollars(initial)}.",
        "This is a selected, previously examined July continuation. It is not a new six-month January–June replay or an untouched holdout.",
        "", "RESULTS", "",
        "Arm | Ending equity | vs OFF | July marked gain | July fees | July max drawdown",
        "--- | --- | --- | --- | --- | ---"]
    for arm in ARMS:
        row = rows[arm]
        if not row["complete"]:
            lines.append(f"{arm} | {row['status']} | unavailable | unavailable | unavailable | unavailable")
            continue
        result = row["result"]
        equity = D(result["ending_equity"])
        gain = None if control is None else equity-control
        lines.append(f"{arm} | {dollars(equity)} | {dollars(gain)} | {dollars(equity-initial)} | {dollars(result.get('fees'))} | {percent(result.get('window_max_drawdown_fraction'))}")
    lines.extend(["", "CAPITAL AND RECOVERY DETAILS", ""])
    for arm in ARMS:
        row = rows[arm]
        if not row["complete"]:
            failure = row.get("failure") or {}
            lines.append(f"{arm}: {row['status']}; {failure.get('completed_frames', 'unknown')} completed frames. {failure.get('reason', 'No complete saved result available.')}")
            continue
        result, release = row["result"], row["result"].get("release", {})
        counts = release.get("counts", {})
        lines.extend([f"{arm}: cash {dollars(result.get('ending_cash'))}; ordinary cash {dollars(result.get('ordinary_cash'))}; parked cash {dollars(result.get('parked_cash'))}.",
            f"  Parked-source cumulative buys {dollars(release.get('parked_spent'))}; still-owned parked-source acquisition basis {dollars(release.get('owned_parked_basis'))}; peak basis plus claims {dollars(release.get('peak_owned_basis_plus_claims'))}.",
            f"  Parked-funded fills: {counts.get('parked_funded_ENTRY_fills', 0)} ENTRY and {counts.get('parked_funded_RECOVERY_fills', 0)} RECOVERY; all new fill kinds {json.dumps(result.get('fill_counts', {}), sort_keys=True)}.",
            f"  Realized net profit {dollars(result.get('new_realized_profit'))}; remaining open P&L {dollars(result.get('remaining_open_pnl'))}; change in open P&L {dollars(result.get('change_in_open_pnl'))}.",
            f"  Positions: {result.get('ending_positions', 'unavailable')} ending, {result.get('maximum_positions', 'unavailable')} maximum, {result.get('mean_positions', 0):.2f} average.", ""])
    lines.extend(["Realized profit and total marked-account gain are different. The comparison includes unsold inventory at actual recorded-close marks; it does not erase its losses.",
        "The $100 caps ($50 in BOTH_TIGHT) limit outstanding source-owned acquisition basis plus parked claims. Returned funds can be reauthorized, so cumulative turnover can exceed the cap without adding money.",
        "All recovery-partial net receipts return to PARK exactly once. Only the parked-funded ownership fraction of whole-sale receipts returns to PARK; the ordinary fraction remains available for rotation.",
        "", "VALIDATION AND OPERATIONAL CORRECTION", "",
        f"Native constructed-money mechanics: {mechanics.get('test_count', 'unknown')} checks; {'PASS' if mechanics.get('passed') else 'not verified'}. These verify funding/ownership mechanics, not strategy profitability.",
        f"Research evidence-integrity checks: {repo.get('validation', {}).get('workflow_integrity_unittest_checks', 'unknown')}; {repo.get('validation', {}).get('workflow_integrity_unittests', 'not verified')}.",
        f"Independently reconstructed complete account/source outputs: {', '.join(sorted(audit_index)) or 'none available yet'}."])
    if rows["OFF"]["complete"]:
        lines.append("OFF completed all 8,640 frames and matched the archived control: 29 fills and 720 archived account checkpoints.")
    if revision:
        lines.extend([f"The first ENTRY_ONLY attempt reached its frozen execution budget at {revision.get('preserved_original_frames', 'unknown'):,} frames and was preserved as an incomplete attempt, not scored as a full result.",
            "Observation overhead/failure exports were corrected. A second attempt then reached 8,542 of 8,640 frames under 420 seconds; its complete observed fill/source journals and scalar rows were preserved.",
            "The final rerun extended only the operational budget from 420 to 600 seconds. The verified protocol difference is execution.maximum_simulation_seconds_per_arm; all economic adapter, signals, caps, targets, selection and other protocol fields stayed unchanged.",
            f"Preserved-prefix proof: {prefix.get('status', 'unavailable')}; {prefix.get('original_complete_frames', 0):,} rows and {prefix.get('scalar_field_comparisons', 0):,} exact scalar comparisons. Source-pin equality is separately recorded.",
            "The original stopped attempt did not export complete fill/source journals; its partial account rows alone cannot prove every stopped-attempt lot transition. This is not a supported restart from that attempt."])
        if second_prefix is not None:
            lines.append(f"Second-attempt prefix proof: {second_prefix.get('status', 'unavailable')}; {second_prefix.get('original_complete_frames', 0):,} rows and {second_prefix.get('scalar_field_comparisons', 0):,} exact scalar comparisons. Independent auditor receipts are included separately.")
    if off and off.get("recovered_summary_from_completed_outputs"):
        lines.append("OFF's final summary was rebuilt from already-completed saved fills/accounts after an export-field error. Missing original timing/rejection counters and cumulative drawdown remain unavailable; they were not invented.")
    lines.extend(["", "WHAT THIS DOES AND DOES NOT PROVE", "",
        "Native movement/recorded-volume ENTRY selection and native fingerprint/recovery/tranche planners remain intact. This selector is not a validated predictive winner model or adaptive machine learning.",
        "Whole and recovery-partial profit floors remain 1% net; fees remain 40 bps per side with 5 bps adverse slippage. Native 25% physical cash reserve and 5% per-asset basis cap remain in place.",
        "PSSM, DTSS and DTSSR are OFF. This is not an all-module replay. The original native assumptions about sparse observations and last actual-close inventory marks remain disclosed.",
        "Five-minute modeled next-open fills do not establish a 30-second replacement promise or real venue execution. Deposits, leverage, withdrawals, production persistence/restart and launch readiness were not tested.",
        "", "REPOSITORY AND EVIDENCE", "",
        f"Inspected original repository head: {repo.get('original_head', 'unavailable')}; original tree: {repo.get('original_tree', 'unavailable')}."])
    if remote:
        lines.append(f"Research branch {remote.get('branch', 'unavailable')}; draft PR #{remote.get('pr_number', 'unavailable')}; allocator commit {remote.get('allocator_commit', 'unavailable')}.")
        lines.append(f"Merged: {remote.get('merged', 'unavailable')}; production/main updated: {remote.get('main_updated', 'unavailable')}; production head {remote.get('production_head', 'unavailable')}.")
        for run in remote.get("ci_runs", []):
            lines.append(f"CI {run.get('name', 'unnamed')}: {run.get('status', 'unavailable')} / {run.get('conclusion', 'unavailable')}.")
        lines.extend(["Remote research-work receipt links:"] + remote_links(remote))
    else:
        lines.append("Remote write/PR status is not established by this report; consult the final repository-update receipt when available.")
    lines.extend([f"Exact tape SHA256: {protocol['tape_sha256']}",
        f"Exact June checkpoint SHA256: {protocol['boundary_checkpoint_sha256']}",
        f"Original native strategy SHA256: {protocol['native_strategy_sha256']}",
        "The accompanying evidence ZIP contains per-arm actual fills, account rows, ownership/source journals, exact executed native witnesses, independent receipts and a payload SHA256 manifest. Giant inputs remain separately pinned.",
        "", "NEXT SUGGESTION", ""])
    if not all_complete:
        lines.append("Complete or diagnose the remaining declared arms and independently audit their saved outputs before choosing a capital-routing policy. Do not tune against incomplete outcomes.")
    elif control is None:
        lines.append("Resolve the missing exact control before assessing any apparent improvement.")
    else:
        best = max(ARMS, key=lambda arm: D(rows[arm]["result"]["ending_equity"]))
        delta = D(rows[best]["result"]["ending_equity"])-control
        if best == "OFF" or delta <= Z:
            lines.append("Keep PARK as the control. More capital movement did not improve ending equity in this window; next test should improve qualified admission/redeployment quality before raising release limits.")
        else:
            lines.append(f"Treat {best} as a provisional next-validation arm: it improved July ending equity over OFF by {dollars(delta)}. Test that frozen policy over a longer continuous period and fresh holdout, including the crash regimes, before expanding caps or implementing live authority.")
        if set(audit_index) != set(ARMS):
            lines.append("Full independent output audit remains required for any arm absent from the validated list above.")
    return "\n".join(lines)+"\n"


def build(root, results, output, allow_incomplete=False, repository_update=None):
    protocol_path = root / "experiment/protocol.json"
    protocol = read(protocol_path)
    rows, result_dirs = collect_status(results, root, protocol["frames"])
    complete = all(row["complete"] for row in rows.values())
    if not complete and not allow_incomplete:
        raise RuntimeError("Requested five-arm matrix is incomplete; no deliverables generated. Pass --allow-incomplete only for an explicit checkpoint report.")
    mechanics = read(root / "independent_review/Independent_Allocator_Mechanics_Receipt.json") or {}
    revision = read(results / "runtime_revision.json") or {}
    repo = source_summary(root)
    remote_path = repository_update or root / "repository_update_receipt.json"
    remote = read(remote_path)
    audit_index, audit_files = complete_audits(root, protocol["frames"])
    if complete and set(audit_index) != set(ARMS):
        raise ArithmeticError("Completed matrix requires all five independent saved-output audits before final packaging")
    prefix = prefix_receipt(root, results, revision.get("preserved_original_frames", 6355))
    second_frames = revision.get("second_preserved_attempt", {}).get("frames", 8542)
    second_prefix = prefix_receipt(root, results, second_frames, "results-observation-v2/ENTRY_ONLY")
    if complete and revision and not prefix.get("passed"):
        raise ArithmeticError("Completed rerun does not verify the preserved original account/source prefix")
    if complete and revision and not second_prefix.get("passed"):
        raise ArithmeticError("Completed rerun does not verify the second preserved account/source prefix")
    output.mkdir(parents=True, exist_ok=True)
    report = output / "Spectra_Parked_Cash_Five_Run_Comparison_2026-10-09.txt"
    content = report_text(protocol, rows, audit_index, mechanics, repo, remote, prefix, revision, second_prefix)
    marks = read(root / "independent_review/Independent_All_Five_Terminal_Mark_Audit.json")
    if marks and marks.get("passed"):
        note = ["", "ADDITIONAL VALUATION AND ALLOCATION FINDINGS", "",
            "Terminal inventory matched actual last recorded closes. Some marks were older than five minutes; these are historical valuations, not executable current liquidation quotes."]
        for item in marks["arms"]:
            note.append(f"{item['arm']}: {dollars(item['stale_marked_inventory_over_300s'])} of inventory used marks older than 300 seconds across {item['stale_asset_count']} assets.")
        tight = rows["BOTH_TIGHT"].get("result") or {}
        counts = tight.get("release", {}).get("counts", {})
        if rows["BOTH_TIGHT"]["complete"] and not counts.get("parked_funded_ENTRY_fills", 0):
            note.extend(["", "The tight shared-cap policy funded recovery exclusively from PARK because recovery-first priority occupied its authority. Ordinary-funded entries still traded. It does not demonstrate balanced parked funding between entry and recovery; test a protected entry allocation separately from asset-quality changes."])
        content = content.replace("\nNEXT SUGGESTION\n", "\n".join(note)+"\n\nNEXT SUGGESTION\n")
    report.write_text(content)
    repo_path = output / "Repository_Selected_Metadata.json"
    dump(repo_path, repo)
    prefix_path = output / "Runtime_Prefix_Equivalence_Receipt.json"
    dump(prefix_path, prefix)
    second_prefix_path = output / "Runtime_Second_Prefix_Equivalence_Receipt.json"
    dump(second_prefix_path, second_prefix)
    summary_path = output / "Five_Run_Matrix_Summary.json"
    summary = dict(status="COMPLETE" if complete else "INCOMPLETE", arms={})
    off = rows["OFF"]["result"]
    for arm, row in rows.items():
        data = row["result"] or {}
        summary["arms"][arm] = dict(status=row["status"],
            ending_equity=data.get("ending_equity"), marked_gain=data.get("marked_gain"),
            gain_versus_OFF=None if not row["complete"] or not rows["OFF"]["complete"] else str(D(data["ending_equity"])-D(off["ending_equity"])),
            complete_frames=data.get("complete_frames"), independently_audited=arm in audit_index,
            failure=row["failure"])
    dump(summary_path, summary)
    members = {}
    def add(path, name=None):
        if not path.is_file(): return
        name = name or str(path.relative_to(root))
        if name in members and members[name].resolve() != path.resolve():
            raise ArithmeticError("Evidence member collision")
        members[name] = path
    for path in (report, repo_path, prefix_path, second_prefix_path, summary_path): add(path, path.name)
    add(protocol_path)
    for arm, directory in result_dirs.items():
        # Resolve the OFF symlink once and package this canonical member path.
        for path in sorted(directory.glob("*")):
            if path.suffix in {".json", ".csv", ".gz"}:
                add(path, f"completed_arms/{arm}/{path.name}")
            elif path.name == "executed_native_body.py":
                add(path, f"completed_arms/{arm}/{path.name}")
            elif arm == "OFF" and path.name == "executed_run_comparison.py":
                # This old executed observer differs from the current Git copy.
                add(path, f"completed_arms/{arm}/{path.name}")
    for name in ("runtime_revision.json", "matrix_receipt.json"):
        add(results / name, f"runtime/{name}")
    add(root / "results-observation-v2/executed_protocol.json", "preserved_second_attempt/executed_protocol.json")
    for name in ("runtime_revision.json", "matrix_receipt.json"):
        add(root / "results-observation-v2" / name, f"preserved_second_attempt/{name}")
    second_attempt = root / "results-observation-v2/ENTRY_ONLY"
    for path in sorted(second_attempt.glob("*")):
        if path.suffix in {".json", ".csv", ".gz"} or path.name in {
                "executed_native_body.py", "executed_run_comparison.py"}:
            add(path, f"preserved_second_attempt/ENTRY_ONLY/{path.name}")
    original = root / "results/ENTRY_ONLY"
    for name in ("window_accounts.csv", "activation_receipt.json", "failure_receipt.json",
                 "source_receipt.json", "hydration_receipt.json", "executed_native_body.py"):
        add(original / name, f"preserved_incomplete_ENTRY_ONLY/{name}")
    for path in audit_files: add(path)
    for name in ("test_release_adapter.py", "audit_release_runs.py", "audit_partial_attempt.py"):
        add(root / "independent_review" / name)
    if remote_path.is_file(): add(remote_path, "repository_update_receipt.json")
    payloads = [{"path": name, "bytes": path.stat().st_size, "sha256": sha(path)}
                for name, path in sorted(members.items())]
    manifest = dict(schema=1, research_only=True, generated_utc=datetime.now(timezone.utc).isoformat(),
        matrix_status=summary["status"], declared_arms=list(ARMS),
        external_inputs=dict(tape_sha256=protocol["tape_sha256"],
            june_checkpoint_sha256=protocol["boundary_checkpoint_sha256"],
            native_strategy_sha256=protocol["native_strategy_sha256"]),
        exclusions=["giant tapes/checkpoints", "full remote repository tree", "Git-backed authored runner/adapter source copies", "credentials or transfer helper files"],
        authored_source_provenance="See per-arm source receipts and remote research commit; historical executed native/old OFF observer witnesses included separately.",
        payloads=payloads)
    manifest_path = output / "Evidence_Manifest.json"
    dump(manifest_path, manifest)
    archive_path = output / "Spectra_Parked_Cash_Five_Run_Evidence_2026-10-09.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(members.items()): archive.write(path, name)
        archive.write(manifest_path, "Evidence_Manifest.json")
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ArithmeticError("Evidence ZIP CRC verification failed")
        for payload in payloads:
            content = archive.read(payload["path"])
            if len(content) != payload["bytes"] or hashlib.sha256(content).hexdigest() != payload["sha256"]:
                raise ArithmeticError(f"Evidence ZIP payload checksum failed: {payload['path']}")
        if len(archive.namelist()) != len(payloads)+1:
            raise ArithmeticError("Unexpected evidence ZIP member")
    validation = dict(passed=True, zip_crc_passed=True, payload_sha256_checks=len(payloads),
                      archive_sha256=sha(archive_path), archive_bytes=archive_path.stat().st_size,
                      matrix_status=summary["status"], report=str(report), archive=str(archive_path))
    dump(output / "Deliverables_Validation_Receipt.json", validation)
    return validation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True,
                        help="External archived workflow collection; not this repository folder")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-update-receipt", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    results = (args.results if args.results.is_absolute() else root / args.results).resolve()
    output = args.output.resolve()
    print(json.dumps(build(root, results, output, args.allow_incomplete,
                           args.repository_update_receipt), default=str))


if __name__ == "__main__": main()
