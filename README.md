# earnings-iv-crush-strategy-skill

基于波动率暴涨与坍塌（Volatility Rush & Crush）机制的美股财报季卖方看跌期权（Cash Secured Put / Short OTM Put）量化评估与策略构建 Skill。

## 核心策略逻辑

财报公布前，市场避险与博弈需求将推高期权隐含波动率（IV Rush），期权权利金大幅膨胀；财报公布后，业绩不确定性迅速落地，隐含波动率断崖式下跌（IV Crush）。

本策略核心在于**赚取波动率由高位回落的确定性溢价**，而非赌方向。通过严苛的五道量化门槛筛选高确定性标的，构建低于预期下界的深度虚值 Put，并在次日开盘波动率坍塌时快速止盈离场。

## 主要功能

1. **本周财报扫描**：拉取本周即将发布财报的美股名单，按市值与时段粗筛，剔除已公布场次，输出候选池后逐级收窄，再对前几家做完整评估。
2. **绝对透明的数据核验**：强制抓取最新客观数据并附带公开信源 URL，严禁捏造与臆测。
3. **五道量化准入过滤（Go / No-Go）**：
   - 流动性与市值门槛（市值 > $2B，股价 $15 - $500）
   - 基本面盈利与分红门槛（近 5 季至少 4 季盈利且无恶化，优先派息）
   - 估值与机构评级门槛（PE < 100，机构评级 Buy / Hold）
   - 形态与历史跳空排除（首选箱体震荡，剔除破位、暴涨、高频击穿 IV 及高危概念）
   - 波动率溢价门槛（确认显著 IV Rush）
4. **量化建仓方案构建**：
   - 选取财报当周到期合约
   - 双重防护定位法：1SD 预期波动下界（LEM = 现价 − EM）再外推 1-2 档 Strike，与日线强支撑位共振
   - 年化 ROI ≥ 60% 门槛
5. **四级离场与应急预案**：
   - 第一顺位：开盘快速止盈 80% - 90% 权利金
   - 第二顺位：开盘 60 分钟（10:30 EST）平仓规则
   - 第三顺位：周五到期日前保本/微利离场
   - 第四顺位：极端黑天鹅被动接盘转车轮策略（The Wheel Strategy）

## 目录结构

```
earnings-iv-crush-strategy-skill/
├── SKILL.md                      # 核心技能指令与决策约束
├── README.md                     # 技能说明文档
├── .gitignore                    # Git 忽略文件
├── scripts/
│   ├── earnings_calendar.py      # 本周财报日历扫描与粗筛
│   └── option_math.py            # 期权数学与盈亏比计算
└── references/
    └── data-sources.md           # 权威数据源清单与取数口径
```

## 环境准备与依赖

两个脚本都只依赖 Python 3 标准库（`argparse`、`math`、`urllib`、`json`、`zoneinfo`），无需安装第三方包，也不需要虚拟环境。`earnings_calendar.py` 需要联网访问 Nasdaq 公开接口。

```bash
# 直接运行即可
python3 scripts/option_math.py --price 82.40 --em 4.36
python3 scripts/earnings_calendar.py
```

## 本周扫描脚本用法

一条命令做完五道硬门槛（市值、股价区间、当周合约是否存在、买盘与持仓量、点差、年化 ROI），输出"能做的名单 + 淘汰原因归类"。

```bash
# 默认：今天（美东）到本周五，市值 ≥ 20 亿美元，已公布场次自动剔除
python3 scripts/earnings_calendar.py

# 整周（含已过去的交易日与已公布场次，用于复盘）
python3 scripts/earnings_calendar.py --full-week

# 只看名单，不查期权链（快，几秒出结果）
python3 scripts/earnings_calendar.py --no-chain

# 把年化门槛提到 80%、持仓量要求提到 500
python3 scripts/earnings_calendar.py --min-roi 80 --min-oi 500

# 指定区间，市值门槛提到 100 亿美元
python3 scripts/earnings_calendar.py --from 2026-10-26 --to 2026-10-30 --min-cap 10e9

# 淘汰名单全列，或输出 JSON
python3 scripts/earnings_calendar.py --verbose
python3 scripts/earnings_calendar.py --json
```

输出分三块：通过硬门槛的名单（按年化 ROI 排序，含 EM、LEM、目标行权价、权利金、持仓量、点差）、淘汰原因归类（市值不足、股价超区间、无当周合约、期权链缺失、流动性不足、点差过宽、收益不足、数据缺失）、数据源链接。

典型量级：财报季一周原始两三百家，过完硬门槛通常只剩 0-5 家。跑一次约 30-70 秒（并发查期权链）。

## 辅助计算脚本用法

计算 Expected Move 区间、行权价阶梯、年化收益率及盈亏平衡点：

```bash
# 基础波动区间与行权价阶梯测算
python scripts/option_math.py --price 82.40 --em 4.36

# 精确测算指定行权价与权利金的年化 ROI、盈亏平衡点
python scripts/option_math.py --price 82.40 --em 4.36 --premium 0.72 --dte 4 --strike 77

# 结合历史跳空击穿率校验
python scripts/option_math.py --price 82.40 --em 4.36 --premium 0.72 --dte 4 --strike 77 --beats 2 --quarters 12
```

## 输出规范

结论直接在对话窗口给出，不落盘：不生成 Markdown 文件、不创建输出目录、不写入 `~/outputs/` 或任何路径。

只有用户明确要求存档时，才写文件到 `~/outputs/earnings-iv-crush-strategy-skill/<yyyy-mm-dd-标的代码-分析报告>.md`。
