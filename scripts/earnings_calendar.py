#!/usr/bin/env python3
"""本周财报扫描 + 期权链硬门槛过滤。

流程：
  1. 拉 Nasdaq 财报日历，得到本周即将发布财报的公司
  2. 市值、时段粗筛，剔除已经公布完的场次
  3. 对剩余公司拉当周到期的期权链，用真实报价算：
       平值跨式隐含波动（EM）、预期下界（LEM）、
       LEM 下方第 1/2 档卖 Put 的年化 ROI、买盘与持仓量、点差
  4. 不达标的当场淘汰并按原因归类，达标的按年化 ROI 排序

目的：把几十上百家里明显不合格的（市值小、股价不在区间、没期权、
没买盘、点差宽、权利金不够）在第一步就剔掉，只把精力留给真正值得
逐只跑五道门槛的少数几只。

数据源：
  日历   https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD
  期权链 https://api.nasdaq.com/api/quote/<SYM>/option-chain?...

用法：
  python3 earnings_calendar.py                      # 本周剩余交易日，全流程
  python3 earnings_calendar.py --full-week          # 整周（含已公布，复盘用）
  python3 earnings_calendar.py --no-chain           # 只看名单，不查期权链（快）
  python3 earnings_calendar.py --min-roi 80         # 把年化门槛提到 80%
  python3 earnings_calendar.py --verbose            # 淘汰名单全列
  python3 earnings_calendar.py --json               # 结构化输出
"""

import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CAL_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
CHAIN_URL = (
    "https://api.nasdaq.com/api/quote/{s}/option-chain"
    "?assetclass=stocks&limit=400&fromdate={e}&todate={e}"
    "&excode=oprac&callput=callput&money=all&type=all"
)

ET = ZoneInfo("America/New_York")
TIME_LABEL = {"time-pre-market": "盘前", "time-after-hours": "盘后", "time-not-supplied": "未定"}

# 淘汰分类，按"先便宜后贵"的校验顺序排列，输出也按这个顺序
CAT_ORDER = ["市值不足", "股价超区间", "无当周合约", "期权链缺失",
             "流动性不足", "点差过宽", "收益不足", "数据缺失"]


def fetch_json(url: str, retries: int = 2) -> dict:
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001 网络异常一律重试
            last = e
    raise RuntimeError(str(last))


def num(s) -> float:
    if s in (None, "", "--", "N/A", "n/a"):
        return 0.0
    t = str(s).replace("$", "").replace(",", "").replace("%", "").strip()
    if t in ("", "--", "N/A"):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return 0.0


def report_cutoff(day: date, label: str) -> datetime:
    """该场财报公布完毕的美东时间点。盘前按 09:30，其余按 16:05。"""
    h, m = (9, 30) if label == "盘前" else (16, 5)
    return datetime.combine(day, datetime.min.time().replace(hour=h, minute=m), tzinfo=ET)


def week_bounds(today: date):
    mon = today - timedelta(days=today.weekday())
    return mon, mon + timedelta(days=4)


def date_range(start: date, end: date):
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            yield cur
        cur += timedelta(days=1)


def expiry_for(day: date) -> date:
    """包含该财报日的当周周五。"""
    return day + timedelta(days=4 - day.weekday())


def reject(r: dict, cat: str, reason: str) -> dict:
    r["verdict"], r["cat"], r["reason"] = "淘汰", cat, reason
    return r


def nearest_expiry(symbol: str) -> str:
    """数据源里该股最近的到期日，用于区分"没期权"和"数据源没有周合约"。"""
    try:
        d = fetch_json(
            "https://api.nasdaq.com/api/quote/{s}/option-chain"
            "?assetclass=stocks&limit=5&excode=oprac&callput=callput&money=at&type=all".format(s=symbol)
        )["data"]
        for it in (d.get("table") or {}).get("rows") or []:
            g = it.get("expirygroup")
            if g:
                return g
    except Exception:
        pass
    return ""


def evaluate(row: dict, today: date, args) -> dict:
    """拉期权链，算 EM/LEM/ROI/流动性，返回带 verdict 与原因的字典。"""
    r = dict(row)
    r.update({"price": 0.0, "em": 0.0, "lem": 0.0, "expiry": "", "dte": 0,
              "strike1": 0.0, "prem1": 0.0, "roi1": 0.0, "oi1": 0.0, "spread1": 0.0,
              "strike2": 0.0, "prem2": 0.0, "roi2": 0.0,
              "verdict": "", "cat": "", "reason": ""})

    exp = expiry_for(date.fromisoformat(row["date"]))
    r["expiry"] = exp.isoformat()
    r["dte"] = max((exp - today).days, 1)

    try:
        data = fetch_json(CHAIN_URL.format(s=row["symbol"], e=exp.isoformat()))["data"]
    except Exception:
        return reject(r, "数据缺失", "期权链拉取失败")

    m = re.search(r"\$([\d,]+\.?\d*)", data.get("lastTrade") or "")
    px = num(m.group(1)) if m else 0.0
    r["price"] = px
    if px <= 0:
        return reject(r, "数据缺失", "取不到最新股价")

    # 第一道：股价区间，用真实报价判断，比市值更直接
    if not (args.min_price <= px <= args.max_price):
        return reject(r, "股价超区间",
                      f"${px:.2f} 不在 ${args.min_price:.0f}-{args.max_price:.0f} 区间")

    quotes = []
    for it in (data.get("table") or {}).get("rows") or []:
        k = num(it.get("strike"))
        if k <= 0:
            continue
        quotes.append({
            "k": k,
            "cb": num(it.get("c_Bid")), "ca": num(it.get("c_Ask")),
            "pb": num(it.get("p_Bid")), "pa": num(it.get("p_Ask")),
            "poi": num(it.get("p_Openinterest")),
        })
    quotes.sort(key=lambda x: x["k"])

    # 第二道：有没有可交易的期权
    if len(quotes) < 5:
        alt = nearest_expiry(row["symbol"])
        if alt:
            return reject(r, "无当周合约",
                          f"没有 {exp.isoformat()} 到期的周合约，最近到期 {alt}")
        return reject(r, "期权链缺失", f"当周到期仅 {len(quotes)} 档合约，无期权或太冷门")

    atm = min(quotes, key=lambda x: abs(x["k"] - px))
    if atm["cb"] <= 0 or atm["pb"] <= 0:
        return reject(r, "流动性不足", "平值合约无有效买盘")
    em = (atm["cb"] + atm["ca"]) / 2 + (atm["pb"] + atm["pa"]) / 2
    if em <= 0:
        return reject(r, "期权链缺失", "平值跨式无报价")

    lem = px - em
    r.update({"em": em, "lem": lem})
    below = [q for q in quotes if q["k"] <= lem]
    if not below:
        return reject(r, "期权链缺失", "LEM 下方没有行权价，档位不足")

    k1 = below[-1]
    k2 = below[-2] if len(below) >= 2 else None

    def roi(q):
        return q["pb"] / q["k"] * 365 / r["dte"] * 100 if q["pb"] > 0 else 0.0

    def spread(q):
        if q["pb"] <= 0 or q["pa"] <= 0:
            return 9.99
        mid = (q["pb"] + q["pa"]) / 2
        return (q["pa"] - q["pb"]) / mid if mid > 0 else 9.99

    r.update({"strike1": k1["k"], "prem1": k1["pb"], "roi1": roi(k1),
              "oi1": k1["poi"], "spread1": spread(k1)})
    if k2:
        r.update({"strike2": k2["k"], "prem2": k2["pb"], "roi2": roi(k2)})

    # 第三道：卖得出去吗
    if k1["pb"] <= 0:
        return reject(r, "流动性不足", f"行权价 {k1['k']:g} 无买盘")
    if k1["poi"] < args.min_oi:
        return reject(r, "流动性不足",
                      f"行权价 {k1['k']:g} 持仓量 {k1['poi']:.0f} < {args.min_oi:g}")

    # 第四道：点差能接受吗（卖方按买价成交，点差宽则平仓代价高）
    if r["spread1"] > args.max_spread:
        return reject(r, "点差过宽",
                      f"行权价 {k1['k']:g} 点差 {r['spread1'] * 100:.0f}% > {args.max_spread * 100:.0f}%")

    # 第五道：权利金够不够
    best = max(r["roi1"], r["roi2"])
    if best < args.min_roi:
        return reject(r, "收益不足", f"年化 ROI 最高 {best:.0f}% < {args.min_roi:.0f}%")

    r["verdict"], r["cat"], r["reason"] = "通过", "", ""
    return r


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

    now_et = datetime.now(ET)
    today = now_et.date()
    mon, fri = week_bounds(today)
    if args.full_week:
        args.include_passed = True

    start = date.fromisoformat(args.start) if args.start else (mon if args.full_week else today)
    end = date.fromisoformat(args.end) if args.end else fri
    if end < start:
        print("结束日期早于起始日期。", file=sys.stderr)
        sys.exit(2)

    # 第一步：日历 + 市值/时段粗筛
    raw_total, rows, cap_rejects, failed_days = 0, [], [], []
    for d in date_range(start, end):
        try:
            payload = fetch_json(CAL_URL.format(d=d.isoformat()))
        except RuntimeError as e:
            failed_days.append(d.isoformat())
            print(f"[警告] {d} 日历拉取失败：{e}", file=sys.stderr)
            continue
        for it in (payload.get("data") or {}).get("rows") or []:
            raw_total += 1
            cap = num(it.get("marketCap"))
            if cap < args.min_cap or (args.max_cap and cap > args.max_cap):
                cap_rejects.append({"symbol": it.get("symbol", ""), "marketCap": cap,
                                    "name": it.get("name", "")})
                continue
            label = TIME_LABEL.get(it.get("time", ""), "未定")
            if not args.include_passed and now_et >= report_cutoff(d, label):
                continue
            rows.append({
                "date": d.isoformat(), "symbol": it.get("symbol", ""),
                "name": it.get("name", ""), "time": label, "marketCap": cap,
                "epsForecast": num(it.get("epsForecast")),
                "lastYearEPS": num(it.get("lastYearEPS")),
            })

    seen, uniq = set(), []
    for r in sorted(rows, key=lambda x: (x["date"], -x["marketCap"])):
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        uniq.append(r)
    uniq.sort(key=lambda x: -x["marketCap"])
    if args.check_limit:
        uniq = uniq[: args.check_limit]

    # 第二步：期权链过滤
    if args.no_chain:
        evaluated = [dict(r, verdict="未查", cat="", reason="跳过期权链检查") for r in uniq]
    elif uniq:
        print(f"正在核查 {len(uniq)} 家的期权链……", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            evaluated = list(ex.map(lambda r: evaluate(r, today, args), uniq))
    else:
        evaluated = []

    passed = sorted([r for r in evaluated if r["verdict"] == "通过"],
                    key=lambda r: -max(r.get("roi1", 0), r.get("roi2", 0)))
    rejected = [r for r in evaluated if r["verdict"] == "淘汰"]
    if args.limit:
        passed = passed[: args.limit]

    if args.json:
        print(json.dumps({"range": [start.isoformat(), end.isoformat()],
                          "calendar_rows": raw_total,
                          "cap_rejected": len(cap_rejects),
                          "checked": len(evaluated),
                          "passed": passed,
                          "rejected": rejected}, ensure_ascii=False, indent=2))
        return

    # 第三步：输出
    print("=" * 84)
    print(f"财报扫描  {start} ~ {end}（美东）   市值 ≥ {args.min_cap / 1e8:.0f} 亿美元   "
          f"股价 ${args.min_price:.0f}-{args.max_price:.0f}")
    print(f"当前美东 {now_et:%Y-%m-%d %H:%M}"
          + ("，含已公布场次" if args.include_passed else "，已剔除公布完毕的场次"))
    print("=" * 84)
    if failed_days:
        print(f"[警告] 以下日期拉取失败，结果不完整：{', '.join(failed_days)}")
    print(f"日历原始 {raw_total} 家 → 市值过关 {len(uniq)} 家 → "
          + (f"期权链达标 {len(passed)} 家" if not args.no_chain else "未查期权链"))

    if args.no_chain:
        print()
        for r in uniq:
            eps = f"{r['epsForecast']:.2f}/{r['lastYearEPS']:.2f}" if r["lastYearEPS"] else "—"
            print(f"  {r['symbol']:<7}{r['time']:<5}{r['marketCap'] / 1e8:>10,.0f}  "
                  f"{eps:<14}{r['name'][:36]}")
        print("\n信源：Nasdaq 财报日历 " + CAL_URL.format(d="YYYY-MM-DD"))
        return

    print()
    if passed:
        print("【通过硬门槛 · 按年化 ROI 排序】")
        for r in passed:
            print(f"  {r['symbol']:<6}{r['time']:<5}{r['date'][5:]}  市值 {r['marketCap'] / 1e8:,.0f}亿  "
                  f"${r['price']:.2f}  EM {r['em']:.2f}({r['em'] / r['price'] * 100:.1f}%)  "
                  f"LEM {r['lem']:.2f}  到期 {r['expiry'][5:]}")
            print(f"        K={r['strike1']:g} 权利金 {r['prem1']:.2f} ROI {r['roi1']:.0f}% "
                  f"OI {r['oi1']:.0f} 点差 {r['spread1'] * 100:.0f}%"
                  + (f"   |   K={r['strike2']:g} 权利金 {r['prem2']:.2f} ROI {r['roi2']:.0f}%"
                     if r.get("strike2") else ""))
    else:
        print("【通过硬门槛】无。本周没有同时满足流动性、点差与年化 ROI 门槛的标的。")

    print()
    print("【淘汰原因】")
    if cap_rejects:
        print(f"  · 市值不足 {args.min_cap / 1e8:.0f} 亿美元：{len(cap_rejects)} 家"
              + ("" if args.verbose else "（--verbose 列出）"))
        if args.verbose:
            for r in sorted(cap_rejects, key=lambda x: -x["marketCap"])[:80]:
                print(f"      {r['symbol']:<6}{r['marketCap'] / 1e8:>8,.1f}亿  {r['name'][:30]}")
    for cat in CAT_ORDER:
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
    print("信源：Nasdaq 财报日历 " + CAL_URL.format(d="YYYY-MM-DD"))
    print("      Nasdaq 期权链 " + CHAIN_URL.format(s="<SYM>", e="<到期日>"))
    print("提醒：ROI 按当前买价与到期日估算，进入深评后仍需用 option_math.py 复核，")
    print("      并逐只跑五道门槛（基本面、估值、机构评级、历史跳空、IV Rush）。")


if __name__ == "__main__":
    main()
