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

详细设计见 [系统架构](docs/greenfield-eight-market-architecture.md)，来源覆盖和局限见 [来源审计](docs/source-audit.md)。

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

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

## 使用边界

技术结构和预测用于研究与每日市场判断，不是自动交易指令。波浪、Rule of 7、斐波那契及支撑阻力均为机械候选，必须结合波动率、利率/美元、流动性、资金或杠杆等独立证据。
