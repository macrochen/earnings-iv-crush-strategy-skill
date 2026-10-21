#!/usr/bin/env python3
"""财报卖方期权（Cash Secured Put）核心计算器。

计算内容：
  1. Expected Move 上/下界（EM / UEM / LEM）
  2. 行权价阶梯：LEM 下方若干档位，各自的安全垫与统计胜率
  3. 年化 ROI 与是否达到 60% 门槛
  4. 盈亏平衡点、单手现金担保金额、最大亏损
  5. 历史跳空超越率（Beat the Implied Move Ratio）

用法示例：
  python3 option_math.py --price 82.40 --em 4.36 --premium 0.72 --dte 4
  python3 option_math.py --price 82.40 --em 4.36 --premium 0.72 --dte 4 --strike 77
  python3 option_math.py --price 82.40 --em 4.36 --premium 0.72 --dte 4 --beats 3 --quarters 12

说明：
  - EM 视为 1 个标准差，用正态分布估算到期时股价低于行权价的概率。
  - 期权链实际 IV 偏斜会让真实概率偏离正态假设，结论只作方向性参考。
"""

import argparse
import math


def norm_cdf(x: float) -> float:
    """标准正态分布累积分布函数。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def annualized_roi(premium: float, strike: float, dte: int) -> float:
    """现金担保卖 Put 的年化收益率（百分比）。"""
    if strike <= 0 or dte <= 0:
        return 0.0
    return (premium / strike) * (365.0 / dte) * 100.0


def build_strikes(lem: float, step: float, levels: int) -> list:
    """从 LEM 向下取整到 step 的整数倍，再逐档下移。"""
    base = math.floor(lem / step) * step
    return [round(base - i * step, 4) for i in range(levels)]


def main() -> None:
    p = argparse.ArgumentParser(description="财报 IV Crush 卖方 Put 计算器")
    p.add_argument("--price", type=float, required=True, help="当前股价")
    p.add_argument("--em", type=float, required=True, help="ATM Straddle 隐含的 1SD 预期波动（美元）")
    p.add_argument("--premium", type=float, default=None, help="计划卖出 Put 的权利金（美元/股）")
    p.add_argument("--dte", type=int, default=None, help="剩余到期天数（日历日）")
    p.add_argument("--strike", type=float, default=None, help="指定行权价，用于精确计算 ROI")
    p.add_argument("--step", type=float, default=1.0, help="行权价档位间距，默认 1 美元")
    p.add_argument("--levels", type=int, default=3, help="LEM 下方展示几档，默认 3")
    p.add_argument("--beats", type=int, default=None, help="历史财报击穿隐含波动次数")
    p.add_argument("--quarters", type=int, default=None, help="统计的季度总数")
    p.add_argument("--target-roi", type=float, default=60.0, help="年化 ROI 门槛，默认 60%%")
    args = p.parse_args()

    price, em = args.price, args.em
    uem = price + em
    lem = price - em

    print("=" * 62)
    print("预期波动区间（1 个标准差）")
    print("=" * 62)
    print(f"  现价            {price:.2f}")
    print(f"  EM              ±{em:.2f}  ({em / price * 100:.2f}% of price)")
    print(f"  上界 UEM        {uem:.2f}")
    print(f"  下界 LEM        {lem:.2f}   <-- 行权价从这里再往下外推 1-2 档")

    print()
    print("=" * 62)
    print(f"行权价阶梯（LEM 下方，档距 {args.step:g}）")
    print("=" * 62)
    print(f"  {'Strike':>8}  {'距现价':>8}  {'安全垫':>8}  {'统计胜率':>8}")
    for k in build_strikes(lem, args.step, args.levels):
        cushion = (price - k) / price * 100.0
        # 股价低于行权价的概率 = N((K - S) / sigma)
        p_assign = norm_cdf((k - price) / em) * 100.0
        print(f"  {k:>8.2f}  {cushion:>7.2f}%  {cushion:>7.2f}%  {100 - p_assign:>7.1f}%")

    if args.premium is not None and args.dte is not None:
        strikes = [args.strike] if args.strike else build_strikes(lem, args.step, args.levels)
        print()
        print("=" * 62)
        print(f"收益与风险（权利金 {args.premium:.2f}，剩余 {args.dte} 天）")
        print("=" * 62)
        for k in strikes:
            roi = annualized_roi(args.premium, k, args.dte)
            be = k - args.premium
            cash = k * 100
            max_loss = (be - 0.0) * 100
            verdict = "通过" if roi >= args.target_roi else "不达标，放弃"
            print(f"  行权价 {k:.2f}")
            print(f"    年化 ROI        {roi:.1f}%   [{verdict}，门槛 {args.target_roi:g}%]")
            print(f"    盈亏平衡点      {be:.2f}")
            print(f"    单手现金担保    {cash:,.0f} 美元")
            print(f"    最大亏损        {max_loss:,.0f} 美元（股价归零的极端情形）")
            print(f"    持有期收益      {args.premium / k * 100:.2f}%（到期作废）")
            print(f"    80% 止盈价      {args.premium * 0.20:.2f}")
            print(f"    90% 止盈价      {args.premium * 0.10:.2f}")

    if args.beats is not None and args.quarters:
        ratio = args.beats / args.quarters * 100.0
        print()
        print("=" * 62)
        print("历史跳空超越率（Beat the Implied Move Ratio）")
        print("=" * 62)
        print(f"  {args.beats} / {args.quarters} 个季度击穿隐含波动 = {ratio:.1f}%")
        if ratio > 50:
            print("  结论：超过 50%，期权市场经常低估其破坏力，坚决剔除。")
        else:
            print("  结论：未超过 50%，可进入后续校验。")

    print()


if __name__ == "__main__":
    main()
