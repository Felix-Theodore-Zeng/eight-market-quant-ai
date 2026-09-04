from __future__ import annotations

import json
import os
import shutil
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
_local_app_data = Path(os.environ.get("LOCALAPPDATA", PROJECT.parent))
_hermes_default = _local_app_data / "hermes" / "bin" / "hermes.exe"
HERMES = Path(os.environ.get("HERMES_EXE", shutil.which("hermes") or _hermes_default))
DB = NEXTGEN / "data" / "eight_market.duckdb"


def run(command: list[str | Path], timeout: int) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([str(x) for x in command], cwd=NEXTGEN, text=True, encoding="utf-8",
                          errors="replace", capture_output=True, timeout=timeout, check=False, env=child_env)


def cli(*arguments: str, timeout: int = 300) -> dict:
    result = run([PYTHON, "-m", "market_system.cli", "--db", DB, *arguments], timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1000:])
    return json.loads(result.stdout)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    prior_month_day = now.date().replace(day=1) - timedelta(days=1)
    month = prior_month_day.strftime("%Y-%m")
    timestamp = now.strftime("%Y%m%d-%H%M%S-JST")
    output = NEXTGEN / "output" / "calibration" / month
    package = output / "calibration-package.json"
    recommendation = output / "recommendation.json"
    report = output / f"Eight_Market_Monthly_Calibration-{month}-{timestamp}.md"
    try:
        cli("evaluate-predictions")
        calibration = cli("monthly-calibration", "--month", month, "--output", str(package))
        prompt = (f"使用 eight-market-monthly-calibrator skill 评估 {package}。"
                  f"把 recommendation.json 写到 {recommendation}，把 Markdown 报告写到 {report}。"
                  "首次输出就直接使用简体中文，不得先生成英文再翻译。"
                  "不得修改任何分析规则。所有 AI 调用不得指定模型或 provider，使用 Hermes 当前 fallback 链。")
        result = run([HERMES, "--skills", "eight-market-monthly-calibrator", "--in", NEXTGEN, "-z", prompt], 1200)
        if result.returncode or not recommendation.exists() or not report.exists():
            raise RuntimeError("月度 AI 校准未完成：" + (result.stderr or result.stdout)[-700:])
        cli("complete-monthly-calibration", "--month", month, "--recommendation", str(recommendation), "--report", str(report))
        decision = json.loads(recommendation.read_text(encoding="utf-8"))
        print("[[as_document]]")
        print(f"MEDIA:{report}")
        print()
        print(f"{month} 八市场月度校准完成。成熟预测 {calibration['matured_predictions']} 条；是否建议调整：{decision.get('change_needed')}。")
        return 0
    except Exception as exc:
        print(f"[八市场月度校准失败] {month}\n原因：{str(exc)[:1000]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
