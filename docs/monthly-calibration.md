# 预测台账与月度校准

## 目的

月度任务不是重新预测市场，也不依赖 Hindsight。它从 DuckDB 的 `prediction_ledger` 读取已经到期的真实预测记录，检验方向判断和置信度是否可靠，再由 Hermes fallback 链判断分析设置是否需要小步优化。

## 每日台账

日报完成且八市场结果通过结构校验后，每个市场以一个固定基准写入四个期限：1日、1周、1月和3月。决策时同时冻结五组量化上下文，避免以后用修订后的数据解释过去判断：

1. 20D/60D 实现波动率；
2. 真实经验分位；
3. 真实最大回撤；
4. OLS 对数价格斜率与 R²；
5. 最强机械支撑/阻力的触碰次数与强度。

期限按未来第 1、5、21、63 个有效观测成熟。评价程序记录到期日期、到期值、真实收益、实际方向和方向是否正确。尚未成熟的预测不会进入月度结论。

## 每月流程

生产入口是 `scripts/run_eight_market_monthly_calibration.py`，建议由 Hermes 以 no-agent 脚本任务在每月 1 日 10:30 JST 运行。它自动选择上一个完整月份：

1. `evaluate-predictions` 更新所有刚成熟的预测。
2. `monthly-calibration` 生成 `output/calibration/<YYYY-MM>/calibration-package.json`。
3. `eight-market-monthly-calibrator` 读取唯一量化输入，经 Hermes 当前 fallback 链生成 `recommendation.json` 和带时间戳的中文 Markdown 报告。
4. `complete-monthly-calibration` 将 AI 建议和报告位置写入 `monthly_calibration_runs`。
5. 成功时交付月度 Markdown；失败时只返回失败原因。

量化包包括总体与分市场准确率、平均置信度、Brier score，以及五组标准在“预测正确”和“预测错误”样本中的均值差异。它用于发现例如高波动期过度自信、极端分位反转误判、弱趋势被误判为延续或支撑强度权重不当等重复问题。

## 优化纪律

- 一个市场/期限少于 20 个成熟样本时不据此修改。
- 同类错误少于 5 次时只记录观察。
- 无充分重复证据时必须输出 `change_needed=false`。
- 一次最多建议一个小变化，并说明风险和回滚方案。
- AI 不直接修改 skill、代码、模型、阈值或调度；应用建议前需要用户确认。

## 相关实现

- 数据库表：`prediction_ledger`、`monthly_calibration_runs`
- 量化实现：`market_system/calibration.py`
- 命令入口：`market_system/cli.py`
- 生产脚本：`scripts/run_eight_market_monthly_calibration.py`
- Hermes skill：`hermes_skill/eight-market-monthly-calibrator/SKILL.md`
