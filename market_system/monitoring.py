from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect


ROOT = Path(__file__).resolve().parents[1]
CRYPTO_24X7 = {
    "crypto.btc", "crypto.eth", "crypto.sol", "crypto.xrp",
    "crypto.crypto_total_market_cap", "crypto.btc_dominance",
    "crypto.perpetual_funding", "crypto.open_interest", "crypto.liquidations",
}


def record_runtime(db_path: str | Path, analysis_date: str, status: str,
                   metrics: dict[str, float], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    connection = connect(db_path)
    try:
        connection.execute("""INSERT OR REPLACE INTO daily_runtime_metrics VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
            analysis_date, status, metrics.get("refresh"), metrics.get("compute"), metrics.get("package"),
            metrics.get("ai"), metrics.get("validation"), metrics.get("ledger"),
            metrics.get("delivery_document"), metrics.get("delivery_summary"), metrics.get("total"),
            json.dumps(detail or {}, ensure_ascii=False), datetime.now(timezone.utc)])
    finally:
        connection.close()
    return {"ok": True, "analysis_date": analysis_date, "status": status}


def _severity(value: float, limits: dict[str, float]) -> str:
    if value > limits["critical"]:
        return "critical"
    if value > limits["warning"]:
        return "warning"
    return "ok"


def _worst(values: list[str]) -> str:
    order = {"ok": 0, "warning": 1, "critical": 2}
    return max(values or ["ok"], key=order.get)


def evaluate_system(db_path: str | Path, analysis_date: str,
                    policy_path: str | Path | None = None) -> dict[str, Any]:
    target_date = date.fromisoformat(analysis_date)
    policy = json.loads(Path(policy_path or ROOT / "config" / "monitoring_policy.json").read_text(encoding="utf-8"))
    connection = connect(db_path)
    try:
        run_row = connection.execute("""SELECT status,failure_reason,completed_at_utc
          FROM daily_analysis_runs WHERE analysis_date=?""", [analysis_date]).fetchone()
        if not run_row:
            run = {"status": "critical", "reason": "未找到当日日报运行记录"}
        elif run_row[0] != "complete":
            run = {"status": "critical", "reason": run_row[1] or f"日报状态为 {run_row[0]}"}
        else:
            run = {"status": "ok", "completed_at_utc": str(run_row[2])}

        sources = []
        rows = connection.execute("""SELECT r.series_id,r.market,r.cadence,MAX(o.observed_date)
          FROM series_registry r LEFT JOIN canonical_observations o
            ON o.series_id=r.series_id AND o.observed_date<=?
          WHERE NOT r.is_reference
          GROUP BY r.series_id,r.market,r.cadence ORDER BY r.series_id""", [analysis_date]).fetchall()
        freshness_policy = policy["source_freshness_days"]
        for series_id, market, cadence, latest in rows:
            bucket = "crypto_24x7" if series_id in CRYPTO_24X7 else (cadence or "daily")
            limits = freshness_policy.get(bucket, freshness_policy["daily"])
            if latest is None:
                sources.append({"series_id": series_id, "status": "critical", "latest_date": None,
                                "lag_days": None, "reason": "数据库中没有可用历史"})
                continue
            lag = max(0, (target_date - latest).days)
            sources.append({"series_id": series_id, "status": _severity(lag, limits),
                            "latest_date": latest.isoformat(), "lag_days": lag, "cadence": bucket})

        package_rows = connection.execute("""SELECT market,byte_length FROM analysis_packages
          WHERE as_of_date=? ORDER BY market""", [analysis_date]).fetchall()
        package_limit = int(policy["package_max_bytes"])
        warning_limit = int(package_limit * float(policy["package_warning_ratio"]))
        packages = [{"market": market, "bytes": size,
                     "utilization": round(size / package_limit, 4),
                     "status": "critical" if size > package_limit else ("warning" if size >= warning_limit else "ok")}
                    for market, size in package_rows]
        if len(packages) != 8:
            packages.append({"market": "_package_set", "status": "critical", "bytes": None,
                             "reason": f"应有 8 个市场包，实际为 {len(package_rows)} 个"})

        runtime_row = connection.execute("""SELECT status,ai_seconds,delivery_document_seconds,
          delivery_summary_seconds,total_seconds FROM daily_runtime_metrics WHERE analysis_date=?""",
          [analysis_date]).fetchone()
        if not runtime_row:
            runtime = {"status": "warning", "reason": "该日尚无分阶段耗时记录（监控启用前的运行可出现一次）"}
        else:
            delivery = float(runtime_row[2] or 0) + float(runtime_row[3] or 0)
            checks = {
                "ai": {"seconds": runtime_row[1], "status": _severity(float(runtime_row[1] or 0), policy["runtime_seconds"]["ai"])},
                "delivery_total": {"seconds": delivery, "status": _severity(delivery, policy["runtime_seconds"]["delivery_total"])},
                "total": {"seconds": runtime_row[4], "status": _severity(float(runtime_row[4] or 0), policy["runtime_seconds"]["total"])},
            }
            runtime = {"status": _worst([value["status"] for value in checks.values()]), "checks": checks}

        hard_failure = run["status"] == "critical" or any(item["status"] == "critical" for item in packages)
        has_advisory = (runtime["status"] != "ok" or any(item["status"] != "ok" for item in sources) or
                        any(item["status"] != "ok" for item in packages))
        # Source publication lag and slow-but-complete stages lower confidence and
        # produce an advisory; they do not turn a completed daily report into a failure.
        status = "critical" if hard_failure else ("warning" if has_advisory else "ok")
        payload = {"schema_version": 1, "monitor_date": date.today().isoformat(),
                   "analysis_date": analysis_date, "status": status, "daily_run": run,
                   "source_freshness": sources, "packages": packages, "runtime": runtime,
                   "policy": policy, "created_at_utc": datetime.now(timezone.utc).isoformat()}
        connection.execute("INSERT OR REPLACE INTO system_monitoring_runs VALUES (?,?,?,?,?)", [
            payload["monitor_date"], analysis_date, status, json.dumps(payload, ensure_ascii=False),
            datetime.now(timezone.utc)])
        return payload
    finally:
        connection.close()


def alert_text(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok":
        return ""
    lines = [f"[八市场系统监控{('失败' if payload['status'] == 'critical' else '提醒')}] {payload['analysis_date']}"]
    run = payload["daily_run"]
    if run["status"] != "ok":
        lines.append("- 日报：" + run.get("reason", run["status"]))
    stale = [item for item in payload["source_freshness"] if item["status"] != "ok"]
    if stale:
        shown = ", ".join(f"{x['series_id']}({x.get('lag_days', '无')}天)" for x in stale[:12])
        lines.append(f"- 来源延迟：{shown}" + (f"，另有 {len(stale)-12} 项" if len(stale) > 12 else ""))
    packages = [item for item in payload["packages"] if item["status"] != "ok"]
    if packages:
        lines.append("- 市场包大小：" + ", ".join(f"{x['market']}={x.get('bytes')}B" for x in packages))
    runtime = payload["runtime"]
    if runtime["status"] != "ok":
        if runtime.get("checks"):
            slow = [f"{key}={value['seconds']:.1f}s" for key, value in runtime["checks"].items() if value["status"] != "ok"]
            lines.append("- 运行耗时：" + ", ".join(slow))
        else:
            lines.append("- 运行耗时：" + runtime.get("reason", runtime["status"]))
    return "\n".join(lines)
