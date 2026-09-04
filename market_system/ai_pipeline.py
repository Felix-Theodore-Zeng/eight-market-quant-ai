from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MARKETS = ("us", "china", "hong_kong", "crypto", "energy", "precious_metals", "agriculture", "fx")
HORIZONS = ("1d", "1w", "1m", "3m")
STAGES = {"trend_continuation", "technical_rebound", "false_breakout", "role_reversal", "range", "insufficient_evidence"}
DIRECTIONS = {"up", "down", "range", "uncertain"}
STAGE_LABELS = {
    "trend_continuation": "趋势延续",
    "technical_rebound": "技术反弹",
    "false_breakout": "假突破",
    "role_reversal": "顶底转换",
    "range": "震荡",
    "insufficient_evidence": "证据不足",
}
DIRECTION_LABELS = {"up": "上行", "down": "下行", "range": "震荡", "uncertain": "不确定"}
HORIZON_LABELS = {"1d": "1日", "1w": "1周", "1m": "1月", "3m": "3月"}
CHINESE_OUTPUT_REQUIREMENT = (
    "语言要求：首次生成结果时就直接使用简体中文。evidence.reason、technical_view 中的论述、"
    "horizons 的 scenario/trigger/invalidation、总体 invalidation/error_cost、news_events 的 event/market_effect、"
    "global_regime 的 label/thesis、transmission_paths 的 mechanism/invalidation、cross_market_contradictions 和 plan_b "
    "均必须是中文。英文来源须概述为中文，不得复制英文完整句子。只有 schema 固定枚举、市场/指标 ID、"
    "通用金融缩写和 URL 可以保留英文。不要先写英文再翻译。"
)


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_markdown(path: Path, content: str) -> None:
    """Write an iOS/Telegram-friendly Markdown document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.encode("utf-8", errors="replace").decode("utf-8")
    with temporary.open("w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(normalized)
    temporary.replace(path)


def prepare_market_jobs(package_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    package_root, output_root = Path(package_dir), Path(output_dir)
    policy = _load(ROOT / "config" / "ai_policy.json")
    jobs = []
    for market in MARKETS:
        package_path = package_root / f"{market}.json"
        package = _load(package_path)
        if package.get("market") != market:
            raise ValueError(f"package market mismatch: {package_path}")
        result_path = output_root / "market-results" / f"{market}.json"
        prompt_path = output_root / "prompts" / f"{market}.txt"
        prompt = f"""使用 eight-market-economic-analyzer skill 分析 {policy['markets'][market]['label']}。
唯一量化输入：{package_path.resolve()}
结果输出：{result_path.resolve()}
输出 schema：{(ROOT / 'schemas' / 'market-analysis-v1.schema.json').resolve()}
分析重点：{policy['markets'][market]['focus']}
只允许使用输入中的指标。将 fibonacci 与波浪、Rule of 7 一样作为辅助工具：只采用程序给出的已确认锚点，说明回撤/扩展关键位及其与机械支撑阻力的共振；不得自行重选锚点，也不得单凭斐波那契提高方向置信度。可用 web_search 搜集截至 {package['as_of_date']} 的重大市场事件并给出 URL；新闻不得替代量化证据。
{CHINESE_OUTPUT_REQUIREMENT}
严格输出一个 JSON，不输出 Markdown。完成后执行 validate-market 门禁。"""
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        jobs.append({"market": market, "model_override": None, "model_routing": policy["model_routing"],
                     "input": str(package_path.resolve()), "prompt": str(prompt_path.resolve()),
                     "output": str(result_path.resolve())})
    manifest = {"schema_version": 1, "as_of_date": _load(package_root / "manifest.json")["as_of_date"],
                "created_at_utc": datetime.now(timezone.utc).isoformat(), "jobs": jobs}
    _write(output_root / "market-jobs.json", manifest)
    return manifest


def validate_market_result(result_path: str | Path, package_path: str | Path) -> dict[str, Any]:
    result, package = _load(result_path), _load(package_path)
    errors: list[str] = []
    warnings: list[str] = []
    required = {"schema_version", "as_of_date", "market", "stage", "confidence", "data_quality",
                "evidence", "invalidation", "technical_view", "horizons", "news_events"}
    missing_fields = sorted(required - set(result))
    if missing_fields: errors.append(f"missing required fields: {missing_fields}")
    if result.get("schema_version") != 1: errors.append("schema_version must be 1")
    if result.get("market") != package.get("market"): errors.append("market mismatch")
    if result.get("as_of_date") != package.get("as_of_date"): errors.append("as_of_date mismatch")
    if result.get("stage") not in STAGES: errors.append("invalid stage")
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1: errors.append("invalid confidence")
    horizons = result.get("horizons") or {}
    if set(horizons) != set(HORIZONS): errors.append("all four horizons are required")
    for horizon in HORIZONS:
        item = horizons.get(horizon) or {}
        expected = {"direction", "confidence", "scenario", "trigger", "invalidation"}
        missing_horizon = sorted(expected - set(item))
        if missing_horizon: errors.append(f"{horizon}: missing fields {missing_horizon}"); continue
        if item.get("direction") not in DIRECTIONS: errors.append(f"{horizon}: invalid direction")
        if not isinstance(item.get("confidence"), (int, float)) or not 0 <= item["confidence"] <= 1:
            errors.append(f"{horizon}: invalid confidence")
    quality = result.get("data_quality") or {}
    expected_quality = package.get("data_quality") or {}
    if quality.get("usable") != expected_quality.get("ok") or quality.get("missing") != expected_quality.get("unavailable"):
        warnings.append("data_quality counts do not match package")
    evidence = result.get("evidence") or {}
    for group in ("core", "supporting", "contradicting"):
        items = evidence.get(group)
        if not isinstance(items, list): errors.append(f"evidence.{group} must be a list")
    if not isinstance(result.get("news_events"), list): errors.append("news_events must be a list")
    max_bytes = _load(ROOT / "config" / "ai_policy.json")["limits"]["market_result_bytes"]
    size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > max_bytes: warnings.append(f"result exceeds preferred {max_bytes} bytes")
    if expected_quality.get("unavailable", 0):
        warnings.append(f"{expected_quality['unavailable']} indicators unavailable; confidence must be reduced")
    return {"ok": not errors, "market": package.get("market"), "bytes": size, "errors": errors, "warnings": warnings}


def prepare_cross_job(market_result_dir: str | Path, package_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    market_root, package_root, output_root = Path(market_result_dir), Path(package_dir), Path(output_dir)
    policy = _load(ROOT / "config" / "ai_policy.json")
    inputs, failures = [], []
    for market in MARKETS:
        result_path, package_path = market_root / f"{market}.json", package_root / f"{market}.json"
        check = validate_market_result(result_path, package_path)
        if not check["ok"]: failures.append(check)
        inputs.append(str(result_path.resolve()))
    if failures: raise ValueError(f"market validation failed: {failures}")
    as_of_date = _load(market_root / "us.json")["as_of_date"]
    result_path = output_root / "cross-market.json"
    prompt_path = output_root / "prompts" / "cross-market.txt"
    prompt = f"""使用 eight-market-economic-analyzer skill 的跨市场综合阶段。
八个已通过门禁的市场结果：{json.dumps(inputs, ensure_ascii=False)}
结果输出：{result_path.resolve()}
输出 schema：{(ROOT / 'schemas' / 'cross-market-analysis-v1.schema.json').resolve()}
只使用八个结果中已有证据，识别美元/利率、风险偏好、中国需求、商品供给与去杠杆的传导；明确矛盾和 Plan B。
{CHINESE_OUTPUT_REQUIREMENT}
严格输出一个 JSON，不追加新指标、不输出 Markdown。完成后执行 validate-cross 门禁。"""
    prompt_path.parent.mkdir(parents=True, exist_ok=True); prompt_path.write_text(prompt, encoding="utf-8")
    job = {"schema_version": 1, "as_of_date": as_of_date, "model_override": None,
           "model_routing": policy["model_routing"],
           "inputs": inputs, "prompt": str(prompt_path.resolve()), "output": str(result_path.resolve())}
    _write(output_root / "cross-market-job.json", job)
    return job


def validate_cross_result(result_path: str | Path, market_result_dir: str | Path) -> dict[str, Any]:
    result, market_root = _load(result_path), Path(market_result_dir)
    errors: list[str] = []
    required = {"schema_version", "as_of_date", "global_regime", "market_summaries", "transmission_paths", "cross_market_contradictions", "plan_b"}
    missing_fields = sorted(required - set(result))
    if missing_fields: errors.append(f"missing required fields: {missing_fields}")
    expected_date = _load(market_root / "us.json")["as_of_date"]
    if result.get("as_of_date") != expected_date: errors.append("as_of_date mismatch")
    summaries = result.get("market_summaries") or []
    if len(summaries) != 8 or {x.get("market") for x in summaries if isinstance(x, dict)} != set(MARKETS):
        errors.append("market_summaries must contain each market exactly once")
    for item in summaries:
        if set((item.get("horizons") or {})) != set(HORIZONS): errors.append(f"{item.get('market')}: missing horizons")
    warnings = []
    if len(result.get("transmission_paths") or []) > 10: warnings.append("more transmission paths than preferred")
    max_bytes = _load(ROOT / "config" / "ai_policy.json")["limits"]["cross_market_result_bytes"]
    size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > max_bytes: warnings.append(f"result exceeds preferred {max_bytes} bytes")
    return {"ok": not errors, "bytes": size, "errors": errors, "warnings": warnings}


def render_report(cross_path: str | Path, market_result_dir: str | Path, output_path: str | Path) -> Path:
    cross, market_root, target = _load(cross_path), Path(market_result_dir), Path(output_path)
    labels = {key: value["label"] for key, value in _load(ROOT / "config" / "ai_policy.json")["markets"].items()}
    results = {market: _load(market_root / f"{market}.json") for market in MARKETS}
    lines = [f"# 八市场趋势与预测报告｜{cross['as_of_date']}", "",
             f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}", "",
             "本报告以截至决策时点的历史序列为基础，分析周、月、季度和年度结构，并形成未来1日、1周、1月和3月判断。", "",
             "## 八市场预测总览", "",
             "| 市场 | 当前阶段 | 置信度 | 1日 | 1周 | 1月 | 3月 |",
             "|---|---|---:|---|---|---|---|"]
    for market in MARKETS:
        item = results[market]
        lines.append(f"| {labels[market]} | {STAGE_LABELS[item['stage']]} | {item['confidence']:.0%} | " +
                     " | ".join(DIRECTION_LABELS[item["horizons"][h]["direction"]] for h in HORIZONS) + " |")
    lines.extend(["", "## 跨市场总状态", "",
                  f"{cross['global_regime']['label']}（置信度 {cross['global_regime']['confidence']:.0%}）", "",
                  cross["global_regime"]["thesis"], "", "## 跨市场传导路径", ""])
    for path in cross.get("transmission_paths") or []:
        evidence = "、".join(path.get("evidence") or [])
        lines.append(f"- {path['from']} → {path['to']}：{path['mechanism']}；证据：{evidence}；失效：{path['invalidation']}")
    for market in MARKETS:
        item = results[market]; quality = item.get("data_quality") or {}; technical = item.get("technical_view") or {}
        lines.extend(["", f"## {labels[market]}：历史结构与未来路径", "",
                      f"当前阶段：{STAGE_LABELS[item['stage']]}｜置信度：{item['confidence']:.0%}", "",
                      f"数据覆盖：可用 {quality.get('usable', 0)}，缺失 {quality.get('missing', 0)}。{quality.get('impact', '')}", "",
                      "### 判断依据", ""])
        evidence = item.get("evidence") or {}
        for title, key in (("核心", "core"), ("支持", "supporting"), ("矛盾", "contradicting")):
            entries = evidence.get(key) or []
            if entries:
                lines.append(f"- {title}证据：" + "；".join(f"{x.get('reason')}（{x.get('ref')}）" for x in entries))
        lines.extend(["", "### 波浪、Rule of 7、斐波那契与关键价区", "",
                      f"- 波浪结构：{technical.get('wave', '证据不足')}",
                      f"- Rule of 7：{technical.get('rule_of_7', '证据不足')}",
                      f"- 斐波那契：{technical.get('fibonacci', '证据不足')}"])
        for level_name, level_key in (("支撑", "support"), ("阻力", "resistance")):
            levels = technical.get(level_key) or []
            if levels:
                text_levels = "；".join(f"{x.get('series_id')} {x.get('price')}（触碰{x.get('touches')}次，强度{x.get('strength')}）" for x in levels)
                lines.append(f"- {level_name}候选：{text_levels}")
        lines.extend(["", "### 四期限预测", "",
                      "| 期限 | 方向 | 置信度 | 主情景 | 确认条件 | 失效条件 | ROI | R/R |",
                      "|---|---|---:|---|---|---|---:|---:|"])
        for horizon in HORIZONS:
            h = item["horizons"][horizon]
            roi = "—" if h.get("roi_percent") is None else f"{h['roi_percent']:.2f}%"
            rr = "—" if h.get("risk_reward") is None else f"{h['risk_reward']:.2f}"
            lines.append(f"| {HORIZON_LABELS[horizon]} | {DIRECTION_LABELS[h['direction']]} | {h['confidence']:.0%} | {h['scenario']} | {h['trigger']} | {h['invalidation']} | {roi} | {rr} |")
        invalidation = item.get("invalidation") or {}
        lines.extend(["", f"总失效条件：{invalidation.get('condition', '')}", "",
                      f"判断错误代价：{invalidation.get('error_cost', '')}"])
        events = item.get("news_events") or []
        if events:
            lines.extend(["", "### 主要事件脉络", ""])
            for event in events:
                lines.append(f"- {event.get('date')}｜[{event.get('event')}]({event.get('url')})：{event.get('market_effect')}")
    lines.extend(["", "## 矛盾、失效与替代路径", ""])
    for contradiction in cross.get("cross_market_contradictions") or []: lines.append(f"- {contradiction}")
    lines.extend(["", f"Plan B：{cross['plan_b']}", "", "## 评估说明", "",
                  "本次预测已经写入本地预测台账。到期结果按未来第1/5/21/63个真实有效观测评估；月度校准结合20D/60D实现波动率、经验分位、真实最大回撤、OLS对数价格斜率与R²、机械支撑阻力触碰次数与强度。", "",
                  "_本报告用于研究与信息整理，不构成自动交易指令。_", ""])
    _write_markdown(target, "\n".join(lines))
    return target
