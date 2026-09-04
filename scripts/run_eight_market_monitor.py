from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_cwd = Path.cwd().resolve()
NEXTGEN = Path(os.environ.get("EIGHT_MARKET_PROJECT_ROOT") or
               (_cwd if (_cwd / "market_system").is_dir() else Path(__file__).resolve().parents[1]))
PROJECT = NEXTGEN.parent
_project_python = PROJECT / ".venv" / "Scripts" / "python.exe"
PYTHON = Path(os.environ.get("EIGHT_MARKET_PYTHON") or (_project_python if _project_python.exists() else sys.executable))
DB = NEXTGEN / "data" / "eight_market.duckdb"


def alert_text(payload: dict) -> str:
    if payload["status"] == "ok":
        return ""
    lines = [f"[八市场系统监控{('失败' if payload['status'] == 'critical' else '提醒')}] {payload['analysis_date']}"]
    if payload["daily_run"]["status"] != "ok":
        lines.append("- 日报：" + payload["daily_run"].get("reason", payload["daily_run"]["status"]))
    stale = [item for item in payload["source_freshness"] if item["status"] != "ok"]
    if stale:
        lines.append("- 来源延迟：" + ", ".join(f"{x['series_id']}({x.get('lag_days', '无')}天)" for x in stale[:12]))
    packages = [item for item in payload["packages"] if item["status"] != "ok"]
    if packages:
        lines.append("- 市场包大小：" + ", ".join(f"{x['market']}={x.get('bytes')}B" for x in packages))
    runtime = payload["runtime"]
    if runtime["status"] != "ok":
        slow = [f"{key}={value['seconds']:.1f}s" for key, value in runtime.get("checks", {}).items() if value["status"] != "ok"]
        lines.append("- 运行耗时：" + (", ".join(slow) or runtime.get("reason", runtime["status"])))
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    analysis_date = (datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)).isoformat()
    output = NEXTGEN / "output" / "monitoring" / f"{analysis_date}.json"
    try:
        process = subprocess.run([str(PYTHON), "-m", "market_system.cli", "--db", str(DB),
                                  "monitor", "--date", analysis_date, "--output", str(output)],
                                 cwd=NEXTGEN, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=180, check=False)
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout or "监控命令失败")[-1000:])
        result = json.loads(process.stdout)
        message = alert_text(result)
        if message:
            print(message)
        return 1 if result["status"] == "critical" else 0
    except Exception as exc:
        print(f"[八市场系统监控失败] {analysis_date}\n原因：数据库或监控程序不可用：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
