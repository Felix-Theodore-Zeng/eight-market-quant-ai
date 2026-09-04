# Eight Market Quant AI

一个以历史结构为核心的八市场数据采集、确定性量化计算与 AI 分析系统。系统覆盖美国、中国 A 股、香港、数字货币、能源、贵金属、农产品和外汇，并输出未来 1 日、1 周、1 月、3 月的条件预测。

它不是单日行情摘要器。首次运行会尽可能批量初始化可用历史，随后每日仅追加或修订近期数据；AI 读取的是固定长度市场包，因此数据库可以持续增长而提示词不会无限变长。

## 核心能力

- DuckDB 点时历史库，记录观测日、实际可用时点、来源、质量和方法版本。
- 周/月/季/年区间收益、高低点、20D/60D 实现波动率、经验分位、真实 MDD、OLS 对数价格斜率与 R²。
- 机械化支撑阻力、波浪候选、Rule of 7，以及斐波那契回撤与扩展。
- 八个独立、固定容量的市场分析包和一个跨市场综合层。
- Hermes 模型 fallback 链驱动的中文分析，不在代码中绑定特定模型或供应商。
- 预测台账、到期评价和月度校准。
- 非阻断式系统监控：来源新鲜度、市场包大小、AI 耗时、投递耗时和整条链路状态。

详细设计见 [系统架构](docs/greenfield-eight-market-architecture.md)，来源覆盖和局限见 [来源审计](docs/source-audit.md)，月度复盘细节见 [预测台账与月度校准](docs/monthly-calibration.md)。

## 低频数据与每日分析兼容

OPEC+ 和黑海出口不会被伪装成每日实际值。能源包优先读取最新 OPEC MOMR 的 DoC 月度产量，保留生产月份、报告地址和实际可用时点；由于该口径与较旧 EIA 22 国拼接历史不完全一致，系统主动关闭这条混合序列的收益率、波动率、分位、回撤和趋势统计。它仍作为能源市场的月度供给背景正常进入 AI 分析。

黑海指标是组合数据：`latest` 保存最近一次可比较的 PortWatch 出口数值，`current_context` 保存截至决策日的每日港口天气，并分别给出出口与天气日期。市场包的 `data_quality.advisories` 会向 AI 明确说明组件时差；AI 不会把天气更新说成出口更新，也不会因原生周度发布而终止日报。

## 预测台账与每月定期评估

每日成功分析后，八个市场基准的 1日、1周、1月、3月预测写入 DuckDB。程序按照未来第 1、5、21、63 个真实有效观测到期评价，不用自然日或尚未到期的结果。

`scripts/run_eight_market_monthly_calibration.py` 每月处理上一个完整月份，执行以下流程：

1. 更新已到期预测的真实方向和收益。
2. 汇总方向准确率、平均置信度、Brier score 和分市场表现。
3. 按五组量化标准比较正确与错误预测：20D/60D 实现波动率、真实经验分位、真实最大回撤、OLS 对数价格斜率与 R²、机械支撑/阻力触碰次数与强度。
4. 使用 `eight-market-monthly-calibrator` skill 和 Hermes 当前 fallback 链生成中文定期思考报告及 `recommendation.json`。
5. 将样本量、量化诊断、AI 建议和报告位置写回 `monthly_calibration_runs`。

少于 20 个成熟样本的市场/期限或少于 5 次的同类错误只记录观察。AI 只能提出一次一个、可回滚的优化建议，不会自动修改生产规则；是否应用仍由用户确认。生产环境当前计划为每月 1 日 10:30 JST，Hermes cron 本身属于机器运行状态，不提交到公开仓库。

## 快速开始

要求 Python 3.10+；TradingView 的非浏览器备用适配器还需要 Node.js 与 pnpm。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-nextgen.txt
pnpm install --frozen-lockfile
python -m market_system.cli init-db
python -m market_system.cli bootstrap --delay 0.8
```

每日确定性数据流程：

```bash
python -m market_system.cli refresh --delay 0.8 --revision-lookback-days 10
python -m market_system.cli compute --as-of YYYY-MM-DD
python -m market_system.cli build-packages --as-of YYYY-MM-DD
```

生产日报脚本 `scripts/run_eight_market_daily.py` 需要 Hermes 和一个兼容的 Telegram 转发器。路径可通过 `.env.example` 中的环境变量配置。API 凭证也只从环境变量读取。

监控脚本：

```bash
python scripts/run_eight_market_monitor.py
```

健康时脚本保持安静；发现来源延迟、包接近上限或耗时异常时输出提醒；数据库、日报、AI 或投递链路失败时返回非零状态。

## 数据与密钥安全

仓库不包含数据库、采集结果、AI 报告、浏览器会话、Telegram 配置或 API key。复制 `.env.example` 到本机私有环境配置，并确保真实值永不提交。公开部署前建议再次运行密钥扫描。

## 仓库内容范围

公开仓库包含新系统可移植的全部源代码：83 项指标/引用目录、采集与历史初始化器、DuckDB schema、统计及技术引擎、八市场包生成器、AI 编排与输出 schema、预测台账/月度校准、三个生产入口脚本、两个 Hermes skills、监控、来源探针、依赖锁文件和自动测试。

有意不上传的只有机器运行数据和秘密：DuckDB 数据库、市场包、AI 结果、Markdown 报告、备份、`.env`、API key、Telegram 配置、TradingView 会话、Hermes 全局模型/provider 配置，以及含服务器路径或审计快照的本机交接文档。这些不是缺失源码，且不应进入公开仓库。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

## 使用边界

技术结构和预测用于研究与每日市场判断，不是自动交易指令。波浪、Rule of 7、斐波那契及支撑阻力均为机械候选，必须结合波动率、利率/美元、流动性、资金或杠杆等独立证据。
