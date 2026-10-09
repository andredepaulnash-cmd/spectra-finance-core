"""Package audited saved outputs. Does not run or change a trading policy."""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import io
import json
from pathlib import Path
import zipfile


def read(path):
    return json.loads(path.read_text())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(workflow, output):
    phase = workflow / "qualification-next"
    results = phase / "results"
    audits = phase / "independent_review"
    verdict = read(audits / "Independent_Factorial_Validation_Verdict.json")
    attribution = read(audits / "Independent_All_Four_Terminal_And_Source_Attribution.json")
    audit = read(audits / "Independent_All_Four_Quality_Output_Audit.json")
    matrix = read(results / "matrix_receipt.json")
    assert verdict["engineering_validation_passed"] and audit["passed"] and attribution["passed"]
    assert len(matrix["records"]) == 4 and all(r["status"] == "COMPLETED" for r in matrix["records"])
    control = read(workflow / "results-runtime-v3/OFF/result.json")
    rows = [{"arm": "PARK", **{k: control[k] for k in (
        "ending_equity", "new_realized_profit", "remaining_open_pnl", "fees",
        "window_max_drawdown_fraction", "ending_cash", "parked_cash", "ordinary_cash")},
        "equity_delta_vs_PARK": "0", "actual_P_fills": 0}]
    at_by_arm = {a["arm"]: a for a in attribution["arms"]}
    for item in verdict["per_arm"]:
        arm = item["arm"]
        actual = read(results / arm / "result.json")
        row = {"arm": arm, **{k: actual[k] for k in rows[0] if k in actual},
            "equity_delta_vs_PARK": item["equity_delta_vs_PARK"],
            "actual_P_fills": actual["release"]["counts"]["parked_funded_buy_fills"],
            "P_spent_by_purpose": actual["release"]["parked_spent_by_kind"],
            "P_terminal_basis_by_purpose": actual["release"]["owned_parked_basis_by_kind"],
            "P_source_marked_pnl_by_purpose": item["P_source_by_kind"],
            "stale_marked_inventory_over_300s": item["stale_marked_inventory_over_300s"],
            "meets_predeclared_candidate_criteria": item["meets_predeclared_candidate_criteria"]}
        assert not row["meets_predeclared_candidate_criteria"]
        rows.append(row)
    repo_receipt = read(phase / "repository_receipt.json")
    repo_receipt.pop("automation_id", None)
    capture = read(phase / "holdout-capture-receipt.json")
    summary = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Previously viewed July 1-30 continuation, 8640 completed five-minute frames per arm; not an untouched holdout or new January-June replay.",
        "original_initial_capital": "2000",
        "restored_June_checkpoint_equity": control["initial_equity"],
        "financial_candidate_accepted": False,
        "engineering_validation_passed": True,
        "rows": rows, "validation": verdict,
        "factorial_contrasts": verdict["factorial_ending_equity_contrasts"],
        "repository": repo_receipt,
        "holdout_capture": {k: capture[k] for k in (
            "raw_prices_inspected", "sha256", "size_bytes", "members",
            "github_run_id", "github_artifact_id", "workflow_conclusion", "operational_metadata")},
        "recommendation": "Keep PARK as the reference. Diagnose entry-time behavior of the unsuccessful source-funded asset/lots from saved journals before proposing another frozen matrix. Do not increase release caps or promote the tested gates. Maintain the frozen prospective holdout without tuning or scoring before November 9; build and independently audit its fresh-account replay adapter before scoring.",
    }
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "Spectra_Capital_Quality_Summary_2026-10-09.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    report = [
        "SPECTRA FINANCE — CAPITAL QUALITY EXPERIMENT — 2026-10-09",
        "",
        "DECISION: No new candidate passed the frozen economic criteria.",
        "Engineering passed. Parked money funded authentic modeled buys, but each tested policy finished with less marked equity, more July drawdown, and worse remaining open P&L than PARK.",
        "",
        "WHAT WAS COMPLETED",
        "Four completed matched July continuation runs, strict ENTRY/RECOVERY source accounting, causal trend/rebound qualification, independent fill/lot/cash/tape audits, a frozen prospective holdout, and the first successful cloud data capture. Research code is saved in draft PR #26; production remains at PR #25.",
        "",
        "WINDOW AND MONEY",
        "July 1 through July 30, 2026 UTC; July 31 excluded. Each arm processed 8,640 actual five-minute frames. All restore the same exact June checkpoint, worth $1,898.277183091943957381228996, inherited from the original $2,000 account.",
        "This is an already inspected development period, not a fresh six-month rerun or untouched performance test. PARK uses the previously saved independently audited control path.",
        "",
        "MATCHED RESULTS (cash plus honestly marked retained inventory)",
        "Arm             Ending equity   vs PARK   July max DD   New realized   Open P&L    Fees",
    ]
    for r in rows:
        report.append(f"{r['arm']:<16}${D(r['ending_equity']):>11.2f}  {D(r['equity_delta_vs_PARK']):>8.2f}   {D(r['window_max_drawdown_fraction'])*100:>9.3f}%  ${D(r['new_realized_profit']):>10.2f}  ${D(r['remaining_open_pnl']):>8.2f}  ${D(r['fees']):>5.2f}")
    report += [
        "",
        "SHARED_RAW: $100 maximum outstanding parked-funded acquisition basis plus pending claims, shared by ENTRY and RECOVERY; no new gate.",
        "SPLIT_RAW: strict $50 ENTRY / $50 RECOVERY authority; no cross-borrowing; no new gate.",
        "SHARED_QUALITY and SPLIT_QUALITY: the same respective budgets, plus the frozen causal gates described below.",
        "This $100 is outstanding authority, not a cumulative-spending limit. Successfully retired basis can be authorized again. Original reserve, pair cap, fees, exit floor and planners still apply.",
        "",
        "QUALIFICATION THAT WAS ACTUALLY TESTED",
        "ENTRY: current observed completed close is above exact observed 1-hour and 24-hour anchors.",
        "RECOVERY: six contiguous actual five-minute bars; current close exceeds the close 15 minutes ago, and the last three bars' minimum low exceeds the preceding three bars' minimum low.",
        "Every referenced row must have six finite OHLCVT fields and four positive OHLC prices. Missing actual anchors deny only the parked supplement. No forward filling or future execution-bar inspection. Conditional replacements use their original submission cutoff. Native causal movement/volume ranking remains unchanged.",
        "These are exploratory rules, not trained AI or a proven predictive winner model. Exact wording corrections are preserved in protocol-clarifications.json; financial policies and source pins stayed frozen throughout the runs.",
        "",
        "WHY MORE TRADING DID NOT WIN",
        "SPLIT_QUALITY realized $32.37 during July versus PARK's $8.86. Its extra $23.51 realized profit was outweighed by $34.52 worse terminal open P&L, leaving $11.01 less account value. The failure is not that cash could not move; the bought inventory did not justify the new risk.",
        "The total account comparison includes inherited inventory and changes to ordinary-funded decisions. P-source attribution below isolates exact lot ownership for description; it is not the causal account effect of one sleeve.",
        "",
        "PARKED-FUNDED SOURCE UTILIZATION AND MARKED P&L",
        "Arm             Actual P buys    Cumulative P spent   P Entry P&L    P Recovery P&L",
    ]
    for r in rows[1:]:
        spent = sum((D(v) for v in r["P_spent_by_purpose"].values()), D(0))
        pnl = r["P_source_marked_pnl_by_purpose"]
        report.append(f"{r['arm']:<16}{r['actual_P_fills']:>13}    ${spent:>16.2f}   ${D(pnl['ENTRY']):>10.2f}   ${D(pnl['RECOVERY']):>13.2f}")
    report += ["", "FACTORIAL COMPARISONS (ending marked equity)"]
    for label, value in verdict["factorial_ending_equity_contrasts"].items():
        report.append(f"{label}: ${D(value):.2f}")
    report += [
        "Splitting mitigated the shared gate's damage, but neither gated policy beat PARK. No threshold was relaxed and no terminal winner/loser list became an entry allowlist after seeing these outcomes.",
        "",
        "INDEPENDENT VALIDATION",
        f"{verdict['constructed_causal_gate_tests']} causal gate tests and {verdict['constructed_purpose_quality_mechanics_tests']} independent purpose/quality mechanics tests passed.",
        f"All {verdict['total_completed_account_rows']:,} account rows / {verdict['total_account_field_comparisons']:,} accounting-field comparisons, {verdict['total_actual_modeled_fills']:,} modeled fills, {verdict['total_unique_raw_tape_gate_decisions']:,} raw-tape gate decisions, and {verdict['total_actual_parked_claim_gate_links']:,} actual parked-claim qualification links passed.",
        "Checks cover nonnegative cash/quantity, reserve and source caps, original lot purpose, exact disposal, cash and basis conservation, fees, genuine next-open tape/quantity/capacity/minimum checks, hard profitable exit floors, independent every-row July drawdown, and terminal marks.",
        "SHARED_RAW exactly reproduced the previously audited BOTH control's account/fill/terminal exports. This catches accidental changes beyond the intended factors.",
        "Engineering PASS is distinct from candidate rejection: every experimental arm failed all three financial criteria (equity, drawdown, open P&L).",
        "",
        "MARK AND EXECUTION LIMITATIONS",
        "Retained inventory uses each asset's last actual historical close. The audit separately discloses marks older than 300 seconds; experimental arms contain $94.75–$112.87 of such marked inventory at the endpoint. Missing observations were not fabricated. These values are historical accounting marks, not current executable liquidation quotes.",
        "Modeled fills include the unchanged 40-bps fee per side, 5-bps adverse slippage, $5 generic quote minimum, and recorded-volume participation rule. They do not establish venue fills, order-book capacity, asset-specific exchange minimums, a 30-second live refill guarantee, or production-safe protected exit execution.",
        "PSSM, DTSS and DTSSR remain OFF. No new DCA ladder, source-cap increase, ML/RL training, deposits, withdrawals or live trading was introduced.",
        "",
        "PROSPECTIVE HOLDOUT — ACTIVATED",
        f"Frozen UTC: {repo_receipt['holdout_freeze_utc']}; first fully eligible bar: {repo_receipt['first_eligible_bar_utc']}; endpoint exclusive: {repo_receipt['end_utc']}.",
        "The part of October 9 before the freeze is not claimed as untouched. Fixed 619-identity universe, raw public Kraken data, separated warmup, bounded capture and overlap, recorded gaps/failures, and no selection/performance evaluation before the endpoint.",
        "All five policies (OFF plus these four) are frozen for matched fresh $2,000 accounts. Their fresh-account simulation adapter is not yet implemented; it must be independently audited before scoring. No July winner is chosen to replace the frozen matrix.",
        f"The first cloud capture completed successfully: run {capture['github_run_id']}, artifact {capture['github_artifact_id']}. Raw data integrity passed in the cloud; the downloaded sealed archive passed ZIP CRC. Its raw prices were not inspected or used for strategy tuning.",
        "Daily capture is scheduled from October 10 through November 9, including the final overlap needed after the endpoint. GitHub artifact retention is 90 days; the first sealed capture is also preserved with this evidence. Future capture failures must be reported rather than filled with synthetic observations.",
        f"Frozen holdout protocol SHA256: {repo_receipt['holdout_protocol_sha256']}",
        f"First sealed capture SHA256: {capture['sha256']}",
        "",
        "REPOSITORY AND RELEASE BOUNDARY",
        "Draft PR: https://github.com/andredepaulnash-cmd/spectra-finance-core/pull/26",
        f"Frozen research source commit: {repo_receipt['source_commit']}",
        "All three source-commit GitHub checks passed. Production branch spectra-core-phase2 remains at 6e894fa9c2b55384d0f6120f41ca91892e1fe207 (PR #25). No merge or production activation is authorized by these results.",
        "",
        "SUGGESTION",
        summary["recommendation"],
        "Before another simulation, inspect the already saved unsuccessful source-funded lots: when they were bought, what causal liquidity/trend/recovery evidence existed then, and how losses accumulated. Preserve exact accounting and protected-execution work; do not blacklist assets from hindsight or infer live profitability from closed-trade wins.",
        "",
        "The evidence package contains saved financial journals, causal gate decisions, independent receipts and source pins. It excludes the large original historical tape and private credentials. The separately sealed holdout archive is opaque prospective data, not evaluated performance.",
    ]
    report_path = output / "Spectra_Capital_Quality_Report_2026-10-09.txt"
    report_path.write_text("\n".join(report) + "\n")
    csv_path = output / "Spectra_Capital_Quality_Results_2026-10-09.csv"
    fields = ("arm", "ending_equity", "equity_delta_vs_PARK", "new_realized_profit", "remaining_open_pnl", "fees", "window_max_drawdown_fraction", "actual_P_fills")
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    members = {}
    def add(path, name):
        data = path.read_bytes()
        if name in members: raise ValueError("Duplicate evidence member")
        members[name] = data
    for f in (report_path, summary_path, csv_path): add(f, f.name)
    for f in audits.glob("*.json"): add(f, "independent_review/" + f.name)
    for f in (phase / "design_review").glob("*.*"): add(f, "design_review/" + f.name)
    add(results / "matrix_receipt.json", "results/matrix_receipt.json")
    allow = {"result.json", "new_fills.json", "release_events.json", "quality_decisions.json", "terminal_active_lots.json", "terminal_positions.json", "terminal_release.json", "window_accounts.csv", "source_receipt.json", "source_changes.json", "initial_active_lots.json", "hydration_receipt.json", "previous_shared_raw_equivalence.json", "native_new_events.json.gz"}
    for r in rows[1:]:
        for f in (results / r["arm"]).iterdir():
            if f.name in allow: add(f, "results/" + r["arm"] + "/" + f.name)
        add(results / (r["arm"] + ".log"), "results/" + r["arm"] + ".log")
    for name in ("result.json", "window_accounts.csv", "source_receipt.json", "control_archive_audit.json"):
        add(workflow / "results-runtime-v3/OFF" / name, "control_PARK/" + name)
    protocol_dir = workflow / "repo/research/capital_quality"
    for name in ("protocol.json", "protocol-clarifications.json"):
        add(protocol_dir / name, "protocol/" + name)
    add(phase / "holdout-capture-receipt.json", "holdout/capture-receipt.json")
    add(phase / "Spectra_Holdout_2026-10-09_Sealed.zip", "holdout/Spectra_Holdout_2026-10-09_Sealed.zip")
    # Redacted operational record, not an automation credential or provider URL.
    members["repository-receipt.json"] = (json.dumps(repo_receipt, indent=2) + "\n").encode()
    manifest = {"schema": 1, "members": [{"path": name, "size_bytes": len(data), "sha256": digest(data)} for name, data in sorted(members.items())], "raw_holdout_prices_inspected": False}
    members["Evidence_Manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    evidence = output / "Spectra_Capital_Quality_Evidence_2026-10-09.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as z:
        for name, data in sorted(members.items()): z.writestr(name, data)
    payload = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert z.testzip() is None
        for r in manifest["members"]:
            data = z.read(r["path"])
            assert digest(data) == r["sha256"] and len(data) == r["size_bytes"]
    temporary = evidence.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(evidence)
    assert evidence.read_bytes() == payload
    receipt = {"passed": True, "zip_crc_passed": True, "all_member_hashes_verified": True, "member_count": len(members), "size_bytes": evidence.stat().st_size, "sha256": digest(evidence.read_bytes())}
    (output / "Evidence_Package_Receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"report": str(report_path.resolve()), "evidence": str(evidence.resolve()), **receipt}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workflow", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    build(args.workflow, args.output)
