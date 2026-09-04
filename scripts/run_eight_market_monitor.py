from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market_system.monitoring import alert_text, evaluate_system


NEXTGEN = Path(__file__).resolve().parents[1]
DB = NEXTGEN / "data" / "eight_market.duckdb"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    analysis_date = (datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)).isoformat()
    output = NEXTGEN / "output" / "monitoring" / f"{analysis_date}.json"
    try:
        result = evaluate_system(DB, analysis_date)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        message = alert_text(result)
        if message:
            print(message)
        return 1 if result["status"] == "critical" else 0
    except Exception as exc:
        print(f"[八市场系统监控失败] {analysis_date}\n原因：数据库或监控程序不可用：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
