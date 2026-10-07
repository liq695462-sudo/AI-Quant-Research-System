# 项目架构

## 数据层

- `market_data_store.py`：建立 SQLite 数据库并维护股票池、日线和 30 分钟行情。
- `backtest_formulas.py`：提供行情下载、缓存、指标与四池条件函数。

## 研究层

- `light_daily_screener.py`：通过 pywencai 生成四池候选并输出报告。
- `lightweight_bbi_backtester.py`：对 BBI/均线逻辑进行轻量历史检验。
- `daily_backtest_runner.py`：串联日线公式、分钟验证和知识快照。

## 知识层

- `build_local_knowledge_index.py`：解析本地文本、Word 与 PDF，生成结构化索引。
- `ingest_trading_articles.py`：识别市场风险、资金主线、行业词和交易纪律。
- `kb_memory_bridge.py`：把外部知识导出压缩为选股与复盘可读取的快照。

## 资讯层

- `news_agent/financebot.py`：聚合 RSS、抽取正文、调用 DeepSeek 生成摘要，并可推送到 ServerChan。

资讯层只提供研究背景；交易候选由规则与回测层独立产生。
