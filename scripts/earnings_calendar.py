#!/usr/bin/env python3
"""本周财报扫描 + 期权链硬门槛过滤（命令行）。

真正的逻辑在 scan_engine.py，这里只负责解析参数、打印报表。
Web 版（serve.py）共用同一个引擎，两边结果必然一致。

流程：
  1. 拉 Nasdaq 财报日历，得到本周即将发布财报的公司
  2. 市值、时段粗筛，剔除已经公布完的场次
  3. 对剩余公司拉当周到期的期权链，用真实报价算：
       平值跨式隐含波动（EM）、预期下界（LEM）、
       LEM 下方第 1/2 档卖 Put 的年化 ROI、买盘与持仓量、点差
  4. 不达标的当场淘汰并按原因归类，达标的按综合分排序

用法：
  python3 earnings_calendar.py                      # 本周剩余交易日，全流程
  python3 earnings_calendar.py --full-week          # 整周（含已公布，复盘用）
  python3 earnings_calendar.py --no-chain           # 只看名单，不查期权链（快）
  python3 earnings_calendar.py --min-roi 80         # 把年化门槛提到 80%
  python3 earnings_calendar.py --verbose            # 淘汰名单全列
  python3 earnings_calendar.py --json               # 结构化输出

需要看图表、拖参数反复试，用 Web 版：python3 serve.py
"""

import argparse
import json
import sys

import scan_engine as eng


def main() -> None:
    ap = argparse.ArgumentParser(description="本周财报扫描 + 期权链过滤")
    ap.add_argument("--from", dest="start", help="起始日期 YYYY-MM-DD（默认今天，美东）")
    ap.add_argument("--to", dest="end", help="结束日期 YYYY-MM-DD（默认本周五）")
    ap.add_argument("--full-week", action="store_true", help="整周（含已过去与已公布，复盘用）")
    ap.add_argument("--min-cap", type=float, default=2e9, help="最小市值，默认 20 亿美元")
    ap.add_argument("--max-cap", type=float, default=0, help="最大市值，0 不限")
    ap.add_argument("--min-price", type=float, default=15.0, help="最低股价，默认 15")
    ap.add_argument("--max-price", type=float, default=500.0, help="最高股价，默认 500")
    ap.add_argument("--min-roi", type=float, default=60.0, help="年化 ROI 门槛，默认 60%%")
    ap.add_argument("--min-oi", type=float, default=100, help="目标行权价最小持仓量，默认 100")
    ap.add_argument("--max-spread", type=float, default=0.30,
                    help="最大买卖价差（占中间价比例），默认 0.30")
    ap.add_argument("--no-chain", action="store_true", help="只出名单，不查期权链")
    ap.add_argument("--check-limit", type=int, default=0, help="最多查多少家期权链，0 全部")
    ap.add_argument("--workers", type=int, default=8, help="并发数，默认 8")
    ap.add_argument("--limit", type=int, default=0, help="最终只输出前 N 家")
    ap.add_argument("--verbose", action="store_true", help="淘汰名单全部列出")
    ap.add_argument("--include-passed", action="store_true", help="连已公布完的也列出来")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    params = {
        "start": args.start or "", "end": args.end or "",
        "include_passed": bool(args.full_week or args.include_passed),
        "min_cap": args.min_cap, "max_cap": args.max_cap,
        "min_price": args.min_price, "max_price": args.max_price,
        "min_roi": args.min_roi, "min_oi": args.min_oi,
        "max_spread": args.max_spread, "workers": args.workers,
    }

    try:
        start, end, include_passed, now_et = eng.resolve_window(params)
    except ValueError as e:
        print(f"{e}。", file=sys.stderr)
        sys.exit(2)

    # 第一步：日历 + 市值/时段粗筛
    snap = eng.build_snapshot(start, end, params, now_et, check_limit=args.check_limit)
    uniq = snap["rows"]

    if args.no_chain:
        print("=" * 84)
        print(f"财报名单  {start} ~ {end}（美东）   市值 ≥ {args.min_cap / 1e8:.0f} 亿美元")
        print(f"当前美东 {now_et:%Y-%m-%d %H:%M}"
              + ("，含已公布场次" if include_passed else "，已剔除公布完毕的场次"))
        print("=" * 84)
        print(f"日历原始 {snap['raw_total']} 家 → 市值过关 {len(uniq)} 家（未查期权链）\n")
        for r in uniq:
            eps = f"{r['epsForecast']:.2f}/{r['lastYearEPS']:.2f}" if r["lastYearEPS"] else "—"
            print(f"  {r['symbol']:<7}{r['time']:<5}{r['marketCap'] / 1e8:>10,.0f}  "
                  f"{eps:<14}{r['name'][:36]}")
        print("\n信源：Nasdaq 财报日历 " + eng.CAL_URL.format(d="YYYY-MM-DD"))
        return

    if not args.json:
        print(f"正在核查 {len(uniq)} 家的期权链……", file=sys.stderr)

    res = eng.run(snap, params)
    passed, rejected, stats = res["passed"], res["rejected"], res["stats"]
    if args.limit:
        passed = passed[: args.limit]

    if args.json:
        print(json.dumps({"range": [start.isoformat(), end.isoformat()],
                          "calendar_rows": stats["calendarRows"],
                          "cap_rejected": stats["capRejected"],
                          "checked": stats["checked"],
                          "passed": passed,
                          "rejected": rejected}, ensure_ascii=False, indent=2))
        return

    # 第二步：输出
    print("=" * 84)
    print(f"财报扫描  {start} ~ {end}（美东）   市值 ≥ {args.min_cap / 1e8:.0f} 亿美元   "
          f"股价 ${args.min_price:.0f}-{args.max_price:.0f}")
    print(f"当前美东 {now_et:%Y-%m-%d %H:%M}"
          + ("，含已公布场次" if include_passed else "，已剔除公布完毕的场次"))
    print("=" * 84)
    if snap["failed_days"]:
        print("[警告] 以下日期拉取失败，结果不完整："
              + ", ".join(d["date"] for d in snap["failed_days"]))
    print(f"日历原始 {stats['calendarRows']} 家 → 市值过关 {stats['checked']} 家 → "
          f"期权链达标 {stats['passed']} 家")

    print()
    if passed:
        print("【通过硬门槛 · 按综合分排序】")
        for r in passed:
            print(f"  {r['symbol']:<6}{r['time']:<5}{r['date'][5:]}  市值 {r['marketCap'] / 1e8:,.0f}亿  "
                  f"${r['price']:.2f}  EM {r['em']:.2f}({r['emPct']:.1f}%)  "
                  f"LEM {r['lem']:.2f}  到期 {r['expiry'][5:]}  分 {r['score']:.0f}")
            print(f"        K={r['strike1']:g} 权利金 {r['prem1']:.2f} ROI {r['roi1']:.0f}% "
                  f"OI {r['oi1']:.0f} 点差 {r['spread1'] * 100:.0f}%"
                  + (f"   |   K={r['strike2']:g} 权利金 {r['prem2']:.2f} ROI {r['roi2']:.0f}%"
                     if r.get("strike2") else ""))
    else:
        print("【通过硬门槛】无。本周没有同时满足流动性、点差与年化 ROI 门槛的标的。")

    print()
    print("【淘汰原因】")
    for cat in eng.CAT_ORDER:
        items = [r for r in rejected if r["cat"] == cat]
        if not items:
            continue
        print(f"  · {cat}：{len(items)} 家")
        show = items if args.verbose else items[:6]
        for r in show:
            print(f"      {r['symbol']:<6}{r['reason']}")
        if not args.verbose and len(items) > len(show):
            print(f"      …… 另有 {len(items) - len(show)} 家（--verbose 全列）")

    print()
    print("信源：Nasdaq 财报日历 " + eng.CAL_URL.format(d="YYYY-MM-DD"))
    print("      Nasdaq 期权链 " + eng.CHAIN_URL.format(s="<SYM>", e="<到期日>"))
    print("提醒：ROI 按当前买价与到期日估算，进入深评后仍需用 option_math.py 复核，")
    print("      并逐只跑五道门槛（基本面、估值、机构评级、历史跳空、IV Rush）。")
    print("要看图表或反复调参，用 Web 版：python3 serve.py")


if __name__ == "__main__":
    main()
