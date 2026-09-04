---
name: eight-market-monthly-calibrator
description: 读取八市场预测台账的月度量化评估包，判断分析设置是否需要小步、可回滚的优化；不依赖 Hindsight。
---

# Eight Market Monthly Calibrator

唯一输入是 `nextgen/output/calibration/<YYYY-MM>/calibration-package.json`。包内预测结果来自 DuckDB，并包含：20D/60D 实现波动率、真实经验分位、真实最大回撤、OLS 对数价格斜率与 R²、机械支撑阻力触碰次数与强度。

使用 Hermes 当前主模型及 fallback 链，不指定模型或 provider。不得读取 Hindsight，不直接修改程序、skill、模型配置或阈值。

首次输出就直接使用简体中文。除 JSON 键、固定布尔值、指标 ID 和通用金融缩写外，`evidence`、`proposed_changes`、`risks`、`rollback_plan` 及 Markdown 报告正文不得包含英文完整句子；不设置事后翻译阶段。

输出两个文件：

1. `recommendation.json`：包含 `month`、`change_needed`、`evidence`、`proposed_changes`、`risks`、`rollback_plan`。证据不足时 `change_needed=false` 且 `proposed_changes=[]`。
2. 带时间戳的 Markdown 月度校准报告：说明成熟样本数、准确率、置信度校准、五类量化条件下的表现，以及是否建议修改。

判断纪律：

- 不用未到期预测评价模型。
- 一个市场/期限少于 20 个成熟样本时，不据此修改。
- 同类错误少于 5 次时，只记录观察，不修改。
- 只建议一次一个小变化，必须可回滚；实际修改仍需用户确认。
