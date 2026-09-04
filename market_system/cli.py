from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .analytics import compute_statistics
from .ai_pipeline import (prepare_cross_job, prepare_market_jobs, render_report,
                          validate_cross_result, validate_market_result)
from .bootstrap import run_bootstrap
from .calibration import (build_monthly_calibration, complete_monthly_calibration, evaluate_matured_predictions,
                          record_analysis_run, record_predictions)
from .catalog import load_catalog
from .db import connect, sync_catalog
from .packages import build_packages
from .technical import compute_technicals
from .monitoring import alert_text, evaluate_system, record_runtime


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="eight-market-system")
    parser.add_argument("--db", default=str(ROOT / "data" / "eight_market.duckdb"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("status")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--delay", type=float, default=0.8)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--delay", type=float, default=0.8)
    refresh.add_argument("--revision-lookback-days", type=int, default=10)
    compute = sub.add_parser("compute")
    compute.add_argument("--as-of", default=date.today().isoformat())
    packages = sub.add_parser("build-packages")
    packages.add_argument("--as-of", default=date.today().isoformat())
    packages.add_argument("--output")
    run_all = sub.add_parser("run-all")
    run_all.add_argument("--as-of", default=date.today().isoformat())
    run_all.add_argument("--delay", type=float, default=0.8)
    run_all.add_argument("--output")
    prepare_ai = sub.add_parser("prepare-ai")
    prepare_ai.add_argument("--packages", required=True)
    prepare_ai.add_argument("--output", required=True)
    validate_market = sub.add_parser("validate-market")
    validate_market.add_argument("--result", required=True)
    validate_market.add_argument("--package", required=True)
    prepare_cross = sub.add_parser("prepare-cross")
    prepare_cross.add_argument("--market-results", required=True)
    prepare_cross.add_argument("--packages", required=True)
    prepare_cross.add_argument("--output", required=True)
    validate_cross = sub.add_parser("validate-cross")
    validate_cross.add_argument("--result", required=True)
    validate_cross.add_argument("--market-results", required=True)
    render = sub.add_parser("render-report")
    render.add_argument("--cross", required=True)
    render.add_argument("--market-results", required=True)
    render.add_argument("--output", required=True)
    ledger = sub.add_parser("record-predictions")
    ledger.add_argument("--market-results", required=True)
    ledger.add_argument("--packages", required=True)
    sub.add_parser("evaluate-predictions")
    monthly = sub.add_parser("monthly-calibration")
    monthly.add_argument("--month", required=True)
    monthly.add_argument("--output", required=True)
    mark = sub.add_parser("record-run")
    mark.add_argument("--date", required=True)
    mark.add_argument("--status", choices=["running", "complete", "failed"], required=True)
    mark.add_argument("--failure-reason")
    mark.add_argument("--market-results-dir")
    mark.add_argument("--cross-result-path")
    mark.add_argument("--report-path")
    runtime = sub.add_parser("record-runtime")
    runtime.add_argument("--date", required=True)
    runtime.add_argument("--status", choices=["complete", "failed"], required=True)
    runtime.add_argument("--metrics-json", required=True)
    runtime.add_argument("--detail-json", default="{}")
    monitor = sub.add_parser("monitor")
    monitor.add_argument("--date", required=True)
    monitor.add_argument("--output")
    complete_monthly = sub.add_parser("complete-monthly-calibration")
    complete_monthly.add_argument("--month", required=True)
    complete_monthly.add_argument("--recommendation", required=True)
    complete_monthly.add_argument("--report", required=True)
    args = parser.parse_args()
    if args.command == "bootstrap":
        print(json.dumps(run_bootstrap(args.db, delay=args.delay), ensure_ascii=False, indent=2)); return 0
    if args.command == "refresh":
        print(json.dumps(run_bootstrap(args.db, delay=args.delay, refresh=True,
                                       revision_lookback_days=args.revision_lookback_days),
                         ensure_ascii=False, indent=2)); return 0
    if args.command == "run-all":
        bootstrap_result = run_bootstrap(args.db, delay=args.delay)
        connection = connect(args.db)
        sync_catalog(connection, load_catalog())
        result = {"bootstrap": bootstrap_result,
                  "statistics": compute_statistics(connection, args.as_of),
                  "technicals": compute_technicals(connection, args.as_of),
                  "packages": build_packages(connection, args.as_of, args.output)}
        connection.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "prepare-ai":
        print(json.dumps(prepare_market_jobs(args.packages, args.output), ensure_ascii=False, indent=2)); return 0
    if args.command == "validate-market":
        result = validate_market_result(args.result, args.package)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ok"] else 2
    if args.command == "prepare-cross":
        print(json.dumps(prepare_cross_job(args.market_results, args.packages, args.output), ensure_ascii=False, indent=2)); return 0
    if args.command == "validate-cross":
        result = validate_cross_result(args.result, args.market_results)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ok"] else 2
    if args.command == "render-report":
        print(json.dumps({"ok": True, "output": str(render_report(args.cross, args.market_results, args.output))}, ensure_ascii=False)); return 0
    if args.command == "record-predictions":
        print(json.dumps(record_predictions(args.db, args.market_results, args.packages), ensure_ascii=False)); return 0
    if args.command == "evaluate-predictions":
        print(json.dumps(evaluate_matured_predictions(args.db), ensure_ascii=False)); return 0
    if args.command == "monthly-calibration":
        print(json.dumps(build_monthly_calibration(args.db, args.month, args.output), ensure_ascii=False, indent=2)); return 0
    if args.command == "record-run":
        print(json.dumps(record_analysis_run(args.db, args.date, args.status,
          failure_reason=args.failure_reason, market_results_dir=args.market_results_dir,
          cross_result_path=args.cross_result_path, report_path=args.report_path), ensure_ascii=False)); return 0
    if args.command == "record-runtime":
        print(json.dumps(record_runtime(args.db, args.date, args.status, json.loads(args.metrics_json),
                                        json.loads(args.detail_json)), ensure_ascii=False)); return 0
    if args.command == "monitor":
        result = evaluate_system(args.db, args.date)
        if args.output:
            target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    if args.command == "complete-monthly-calibration":
        print(json.dumps(complete_monthly_calibration(args.db, args.month, args.recommendation, args.report), ensure_ascii=False)); return 0
    connection = connect(args.db)
    sync_catalog(connection, load_catalog())
    if args.command == "init-db":
        result = {"ok": True, "database": str(Path(args.db).resolve()), "series": connection.execute("SELECT COUNT(*) FROM series_registry").fetchone()[0]}
    elif args.command == "status":
        last_run = connection.execute("""SELECT run_id,started_at_utc,completed_at_utc,status,
          success_count,partial_count,failed_count FROM bootstrap_runs ORDER BY run_id DESC LIMIT 1""").fetchone()
        result = {
            "database": str(Path(args.db).resolve()),
            "registry_series": connection.execute("SELECT COUNT(*) FROM series_registry").fetchone()[0],
            "observation_rows": connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0],
            "series_with_data": connection.execute("SELECT COUNT(DISTINCT series_id) FROM market_observations").fetchone()[0],
            "checkpoints_ok": connection.execute("SELECT COUNT(*) FROM source_checkpoints WHERE status='ok'").fetchone()[0],
            "last_run": dict(zip(["run_id", "started_at_utc", "completed_at_utc", "status", "success", "partial", "failed"], last_run)) if last_run else None,
        }
    elif args.command == "compute":
        result = {"statistics": compute_statistics(connection, args.as_of),
                  "technicals": compute_technicals(connection, args.as_of)}
    elif args.command == "build-packages":
        result = build_packages(connection, args.as_of, args.output)
    connection.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
