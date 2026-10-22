# 数据源与取数口径

评估财报卖方期权时必须逐项取数并附链接。本文件是取数清单，不是结论。

## 财报日历（批量扫描用）

- 接口（脚本默认走这里）：`https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD`，返回当日全部公司，含代码、名称、市值、盘前/盘后时段、EPS 预期与去年同期 EPS。需要带 User-Agent，否则被拒。
- 网页备用：Nasdaq Earnings Calendar `https://www.nasdaq.com/market-activity/earnings`、Yahoo Finance Calendar `https://finance.yahoo.com/calendar/earnings`。

取数口径：

- 时段必须区分盘前/盘后。"未定"（time-not-supplied）的场次按盘后处理，并在报告里标注不确定。
- 判断一场财报是否还来得及交易：盘前财报以当日 09:30（美东）为界，盘后财报以 16:05 为界，过了就视为已公布。脚本默认按这个规则剔除，`--include-passed` 可以关掉。

## 期权链（硬门槛过滤用）

- 主用：`https://api.nasdaq.com/api/quote/<SYM>/option-chain?assetclass=stocks&limit=400&fromdate=<到期日>&todate=<到期日>&excode=oprac&callput=callput&money=all&type=all`。返回每档行权价的看涨/看跌买价、卖价、成交量、持仓量，以及 `lastTrade` 里的最新股价。
- 交叉核对：CBOE 延迟报价 `https://cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json`（会 307 跳转，需跟随重定向）。返回全部到期日的合约，带 `iv`、`delta`、`open_interest`、`volume`，适合核对"到底有没有周合约"和读 IV。
- 平值跨式算 EM：取最接近现价的行权价，看涨与看跌中间价相加。EM 是 1 个标准差，不是最大跌幅。
- 卖方权利金按买价（bid）估，不按中间价。点差 =（卖价 − 买价）/ 中间价，超过 30% 的不要碰。

取数口径：

- **很多中大盘只有月合约，没有周合约**（例如 CBRE 在数据源里只有 11 月 20 日之后的到期日）。这类标的做不了本周的 IV Crush，但**不等于没有期权**。报告里要把"无当周合约"和"期权流动性差"分开讲。
- 判断"没有周合约"前，最好用 CBOE 那个接口核对一次，避免把数据缺口说成事实。
- Yahoo 的批量报价接口现在常返回 429，不要依赖它。

## 行情与财报日期

- Yahoo Finance 个股页：股价、市值、P/E、股息率、财报日期。链接形如 `https://finance.yahoo.com/quote/<TICKER>/`
- Nasdaq Earnings Calendar：官方口径的财报日期与 BMO/AMC 时段。`https://www.nasdaq.com/market-activity/earnings`
- 公司投资者关系页（Investor Relations）：最权威的日期确认，以公司新闻稿为准。
- TradingView：日线图看形态（箱体、破位、抛物线），以及密集成交区/支撑位。`https://www.tradingview.com/symbols/<EXCHANGE>-<TICKER>/`

取数口径：

- 财报日期要区分"公司已公告"与"第三方预估"。第三方预估日期必须标注为预估。
- BMO/AMC 判断错误的后果是建仓时点错一天，务必以公司公告为准。

## 基本面

- 季度净利润：Yahoo Finance 的 Financials → Quarterly Income Statement，或 StockAnalysis `https://stockanalysis.com/stocks/<ticker>/financials/?p=quarterly`
- 分析师评级与目标价：Yahoo Finance Analysis 页、MarketBeat、TipRanks。`https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/price-target/`
- 股息：公司 IR 页的股息公告，或 Yahoo Finance 的 Dividend 栏。

取数口径：

- 净利润看 GAAP 口径的 Net Income，不看 Adjusted EBITDA。转亏的季度要指出是哪一季。
- 分析师评级看综合平均（Buy/Hold/Sell 家数），不看单一机构。

## 波动率与期权链

- Market Chameleon：IV、IV Rank/Percentile、财报前后 IV 对比。`https://marketchameleon.com/Overview/<TICKER>/IV/`
- Barchart 期权链：`https://www.barchart.com/stocks/quotes/<TICKER>/options`
- OptionCharts / OptionStrat：图形化看 IV Crush 历史。
- Earnings-Watcher / Option-Van：直接给出 Expected Move 与历史财报跳空统计。

取数口径：

- EM 优先取"当周到期（含财报日）ATM Straddle 价格"，或数据源直接给出的 Expected Move 数值。两个来源不一致时，取更保守（更大）的那个。
- LEM = 现价 − EM。EM 是 1 个标准差，不是最大跌幅。
- IV Rush 的判定：财报当周 IV 明显高于近 3 个月常态（一般高出 30% 以上），且 IV Rank 处于高位。

## 历史跳空

- Market Chameleon 的 Earnings 页（历史财报后 1 日涨跌幅与隐含波动对比）。
- Earnings-Watcher 的"Beat the Implied Move"统计。
- 拿不到现成统计时，用过去 4-16 个季度的实际次日涨跌幅与当次隐含波动逐一比对，自行计算比例，并注明是自行计算。

## 计算工具

`scripts/option_math.py` 负责 LEM、行权价阶梯、年化 ROI、盈亏平衡点、统计胜率。

```bash
python3 scripts/option_math.py --price 82.40 --em 4.36 --premium 0.72 --dte 4 --strike 77
```

统计胜率按正态分布估算（EM 当 1 个标准差）。真实期权链有偏斜，实际概率通常比正态估算略差，所以结论只能当方向性参考，不能当保证。

## 常见取数坑

- 股价用的是盘前/盘后价还是收盘价，要说明。用收盘价做基准，并在报告里写清基准时点。
- 市值单位是十亿还是百万，别写错量级。
- P/E 为负（亏损）时不要写"P/E 很低"，那是亏损导致的无效值。
- 拆股、并股后的历史涨跌幅要做复权处理，否则跳空统计会失真。
- 期权链报价有买卖价差，卖方按买价（bid）估算权利金，不要按中间价。
