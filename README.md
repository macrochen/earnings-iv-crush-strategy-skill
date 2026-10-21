# earnings-iv-crush-strategy-skill

基于波动率暴涨与坍塌（Volatility Rush & Crush）机制的美股财报季卖方看跌期权（Cash Secured Put / Short OTM Put）量化评估与策略构建 Skill。

## 核心策略逻辑

财报公布前，市场避险与博弈需求将推高期权隐含波动率（IV Rush），期权权利金大幅膨胀；财报公布后，业绩不确定性迅速落地，隐含波动率断崖式下跌（IV Crush）。

本策略核心在于**赚取波动率由高位回落的确定性溢价**，而非赌方向。通过严苛的五道量化门槛筛选高确定性标的，构建低于预期下界的深度虚值 Put，并在次日开盘波动率坍塌时快速止盈离场。

## 主要功能

1. **绝对透明的数据核验**：强制抓取最新客观数据并附带公开信源 URL，严禁捏造与臆测。
2. **五道量化准入过滤（Go / No-Go）**：
   - 流动性与市值门槛（市值 > $2B，股价 $15 - $250）
   - 基本面盈利与分红门槛（近 5 季至少 4 季盈利且无恶化，优先派息）
   - 估值与机构评级门槛（PE < 100，机构评级 Buy / Hold）
   - 形态与历史跳空排除（首选箱体震荡，剔除破位、暴涨、高频击穿 IV 及高危概念）
   - 波动率溢价门槛（确认显著 IV Rush）
3. **量化建仓方案构建**：
   - 选取财报当周到期合约
   - 双重防护定位法：1SD 预期波动下界（LEM = 现价 − EM）再外推 1-2 档 Strike，与日线强支撑位共振
   - 年化 ROI ≥ 60% 门槛
4. **四级离场与应急预案**：
   - 第一顺位：开盘快速止盈 80% - 90% 权利金
   - 第二顺位：开盘 60 分钟（10:30 EST）平仓规则
   - 第三顺位：周五到期日前保本/微利离场
   - 第四顺位：极端黑天鹅被动接盘转车轮策略（The Wheel Strategy）

## 目录结构

```
earnings-iv-crush-strategy-skill/
├── SKILL.md                 # 核心技能指令与决策约束
├── README.md                # 技能说明文档
├── .gitignore               # Git 忽略文件
├── scripts/
│   └── option_math.py       # 核心期权数学与盈亏比计算脚本
└── references/
    └── data-sources.md      # 权威数据源清单与取数口径
```

## 环境准备与依赖

本 Skill 内置计算脚本 `scripts/option_math.py` 仅依赖 Python 3 标准库（`argparse`, `math`）。

按照安全规范，建议在当前 Skill 目录下配置并激活独立虚拟环境：

```bash
# 1. 进入 Skill 目录
cd ~/.agents/skills/earnings-iv-crush-strategy-skill

# 2. 创建本地虚拟环境
python3 -m venv .venv

# 3. 激活虚拟环境 (macOS/Linux)
source .venv/bin/activate
```

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

所有分析报告统一按照标准 Markdown 格式输出到 `~/outputs/` 目录：
```
~/outputs/earnings-iv-crush-strategy-skill/<yyyy-mm-dd-标的代码-分析报告>.md
```
