from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


NEXTGEN = Path(__file__).resolve().parents[1]
PROJECT = NEXTGEN.parent
PYTHON = Path(os.environ.get("EIGHT_MARKET_PYTHON", sys.executable))
_local_app_data = Path(os.environ.get("LOCALAPPDATA", PROJECT.parent))
_hermes_default = _local_app_data / "hermes" / "bin" / "hermes.exe"
HERMES = Path(os.environ.get("HERMES_EXE", shutil.which("hermes") or _hermes_default))
FORWARDER_ROOT = Path(os.environ.get("TELEGRAM_FORWARDER_ROOT", PROJECT.parent / "telegram-forwarder"))
FORWARDER_PYTHON = Path(os.environ.get("TELEGRAM_FORWARDER_PYTHON", FORWARDER_ROOT / ".venv" / "Scripts" / "python.exe"))
FORWARDER = FORWARDER_ROOT / "forwarder.py"
DB = NEXTGEN / "data" / "eight_market.duckdb"
LABELS = {"us": "美国", "china": "中国A股", "hong_kong": "香港", "crypto": "数字货币",
          "energy": "能源", "precious_metals": "贵金属", "agriculture": "农产品", "fx": "外汇"}
STAGES = {"trend_continuation": "趋势延续", "technical_rebound": "技术反弹", "false_breakout": "假突破",
          "role_reversal": "顶底转换", "range": "震荡", "insufficient_evidence": "证据不足"}
DIRECTIONS = {"up": "上行", "down": "下行", "range": "震荡", "uncertain": "不确定"}


def run(command: list[str | Path], *, timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    if input_text is not None:
        input_text = input_text.encode("utf-8", errors="replace").decode("utf-8")
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([str(x) for x in command], cwd=NEXTGEN, input=input_text, text=True,
                          encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False,
                          env=child_env)


def cli(*arguments: str, timeout: int = 900) -> dict:
    result = run([PYTHON, "-m", "market_system.cli", "--db", DB, *arguments], timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "command failed")[-1200:])
    return json.loads(result.stdout)


def missing_summary(package_dir: Path) -> list[dict]:
    missing = []
    for market in LABELS:
        package = json.loads((package_dir / f"{market}.json").read_text(encoding="utf-8"))
        for indicator in package["indicators"]:
            if indicator.get("status") != "ok":
                missing.append({"market": market, "indicator": indicator["key"],
                                "impact": f"{LABELS[market]}判断置信度下降"})
    return missing


def summary_text(result_dir: Path, analysis_date: str) -> str:
    items = []
    for market, label in LABELS.items():
        result = json.loads((result_dir / f"{market}.json").read_text(encoding="utf-8"))
        horizons = result["horizons"]
        items.append(f"{label}：{STAGES[result['stage']]}；1日 {DIRECTIONS[horizons['1d']['direction']]}，"
                     f"1周 {DIRECTIONS[horizons['1w']['direction']]}，1月 {DIRECTIONS[horizons['1m']['direction']]}，"
                     f"3月 {DIRECTIONS[horizons['3m']['direction']]}")
    summary = "这是今天的八市场报告（" + analysis_date + "）。\n" + "\n".join(items)
    return summary.encode("utf-8", errors="replace").decode("utf-8")


def mark(analysis_date: str, status: str, **values: str) -> None:
    arguments = ["record-run", "--date", analysis_date, "--status", status]
    for key, value in values.items():
        if value:
            arguments.extend(["--" + key.replace("_", "-"), value])
    try:
        cli(*arguments, timeout=60)
    except Exception:
        pass


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    now_jst = datetime.now(ZoneInfo("Asia/Tokyo"))
    analysis_date = (now_jst.date() - timedelta(days=1)).isoformat()
    timestamp = now_jst.strftime("%Y%m%d-%H%M%S-JST")
    package_dir = NEXTGEN / "output" / "market-packages" / analysis_date
    run_dir = NEXTGEN / "output" / "ai-runs" / analysis_date
    market_results = run_dir / "market-results"
    report = run_dir / f"Eight_Market_Daily_Report-{analysis_date}-{timestamp}.md"
    metrics: dict[str, float] = {}
    total_started = time.perf_counter()
    final_status = "failed"
    final_detail: dict[str, str] = {}

    def timed(stage: str, function):
        started = time.perf_counter()
        try:
            return function()
        finally:
            metrics[stage] = round(time.perf_counter() - started, 3)

    mark(analysis_date, "running")
    try:
        refresh = timed("refresh", lambda: cli("refresh", "--delay", "0.8", "--revision-lookback-days", "10", timeout=1200))
        counts = refresh.get("counts") or {}
        if not counts.get("success"):
            raise RuntimeError(f"采集器整体没有成功工作：{counts}")
        timed("compute", lambda: cli("compute", "--as-of", analysis_date, timeout=300))
        timed("package", lambda: cli("build-packages", "--as-of", analysis_date, "--output", str(package_dir), timeout=300))
        missing = missing_summary(package_dir)
        cli("prepare-ai", "--packages", str(package_dir), "--output", str(run_dir), timeout=120)
        prompt = (f"执行 {analysis_date} 八市场日报。严格使用 eight-market-economic-analyzer skill，读取 {run_dir / 'market-jobs.json'}，"
                  "完成八个分市场结果和最小完整性检查，再完成跨市场综合和渲染。"
                  f"最终 Markdown 写入 {report}。数据缺失不得阻断，必须说明缺失项、受影响判断和置信度下降。"
                  "首次分析输出就直接使用简体中文；不得先输出英文再翻译，英文新闻只写中文概述。"
                  "不要发送 Telegram，不要写 Hindsight，不要创建或修改 cron。所有 AI 调用不得指定模型或 provider，使用 Hermes 当前 fallback 链。")
        ai = timed("ai", lambda: run([HERMES, "--skills", "eight-market-economic-analyzer", "--in", NEXTGEN, "-z", prompt], timeout=2400))
        if ai.returncode:
            raise RuntimeError("AI 无响应或执行失败：" + (ai.stderr or ai.stdout)[-900:])
        expected = [market_results / f"{market}.json" for market in LABELS]
        absent = [path.name for path in expected if not path.exists()]
        if absent:
            raise RuntimeError(f"AI 没有完成全部八市场分析：{absent}")
        cross = run_dir / "cross-market.json"
        if not cross.exists() or not report.exists():
            raise RuntimeError("AI 没有完成跨市场综合或报告生成")
        def validate_all() -> None:
            for market in LABELS:
                check = cli("validate-market", "--result", str(market_results / f"{market}.json"),
                            "--package", str(package_dir / f"{market}.json"), timeout=60)
                if not check.get("ok"):
                    raise RuntimeError(f"{market} AI 分析未完成：{check.get('errors')}")
            cross_check = cli("validate-cross", "--result", str(cross), "--market-results", str(market_results), timeout=60)
            if not cross_check.get("ok"):
                raise RuntimeError(f"跨市场 AI 分析未完成：{cross_check.get('errors')}")
        timed("validation", validate_all)
        def update_ledger() -> None:
            cli("record-predictions", "--market-results", str(market_results), "--packages", str(package_dir), timeout=120)
            cli("evaluate-predictions", timeout=120)
        timed("ledger", update_ledger)
        summary = summary_text(market_results, analysis_date)
        group_document = timed("delivery_document", lambda: run([FORWARDER_PYTHON, FORWARDER, "--send-document", report,
                              "--report-id", f"Eight_Market_Daily_Report-{analysis_date}-document"], timeout=180))
        if group_document.returncode:
            raise RuntimeError("Telegram 群文件发送失败：" + (group_document.stderr or group_document.stdout)[-600:])
        group_summary = timed("delivery_summary", lambda: run([FORWARDER_PYTHON, FORWARDER, "--send-stdin",
                             "--report-id", f"Eight_Market_Daily_Report-{analysis_date}-summary"], timeout=180, input_text=summary))
        if group_summary.returncode:
            raise RuntimeError("Telegram 群摘要发送失败：" + (group_summary.stderr or group_summary.stdout)[-600:])
        mark(analysis_date, "complete", market_results_dir=str(market_results), cross_result_path=str(cross), report_path=str(report))
        final_status = "complete"
        print("[[as_document]]")
        print(f"MEDIA:{report}")
        print()
        print(summary)
        if missing:
            print("\n数据缺失提醒：")
            for item in missing:
                print(f"- {LABELS[item['market']]} {item['indicator']}：{item['impact']}")
        return 0
    except Exception as exc:
        reason = str(exc).replace("\n", " ")[:1200]
        final_detail = {"failure_reason": reason}
        mark(analysis_date, "failed", failure_reason=reason)
        print(f"[八市场日报运行失败] {analysis_date}\n原因：{reason}")
        return 1
    finally:
        metrics["total"] = round(time.perf_counter() - total_started, 3)
        try:
            cli("record-runtime", "--date", analysis_date, "--status", final_status,
                "--metrics-json", json.dumps(metrics), "--detail-json", json.dumps(final_detail, ensure_ascii=False), timeout=60)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
