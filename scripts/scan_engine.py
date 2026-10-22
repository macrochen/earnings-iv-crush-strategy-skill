#!/usr/bin/env python3
"""财报期权扫描引擎：网络抓取 + 纯函数过滤，供 CLI 与 Web 服务共用。

设计要点：
  - 抓取（慢，几十秒）与过滤（快，毫秒）彻底分离。抓一次原始快照，之后改任何
    门槛参数都只重跑 analyze()，不需要再联网。这是 Dashboard 能"调参秒出"的前提。
  - analyze() 是纯函数，不联网、不读文件，给定 (行数据, 参数) 必然得到同一结果。
  - 淘汰必须带证据：除了一句原因，还给出 gap（实测值 / 门槛值），前端才能画出
    "差多少"。名企为什么不能做，答案就在这个字段里。

数据源：
  日历   https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD
  期权链 https://api.nasdaq.com/api/quote/<SYM>/option-chain?...
"""

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
NEAREST_URL = (
    "https://api.nasdaq.com/api/quote/{s}/option-chain"
    "?assetclass=stocks&limit=5&excode=oprac&callput=callput&money=at&type=all"
)

ET = ZoneInfo("America/New_York")
TIME_LABEL = {"time-pre-market": "盘前", "time-after-hours": "盘后", "time-not-supplied": "未定"}

# 淘汰分类，按"先便宜后贵"的校验顺序排列
CAT_ORDER = ["市值不足", "股价超区间", "无当周合约", "到期太近", "期权链缺失",
             "流动性不足", "点差过宽", "收益不足", "数据缺失"]

# 门槛所在的"第几道"，前端用来显示"卡在第 N 关"。9 = 不是门槛问题（数据/结构）
CAT_GATE = {"市值不足": 1, "股价超区间": 2, "无当周合约": 3, "到期太近": 3,
            "期权链缺失": 3, "流动性不足": 4, "点差过宽": 5, "收益不足": 6,
            "数据缺失": 9}

DEFAULTS = {
    "start": "",            # 空 = 今天（美东）
    "end": "",              # 空 = 本周五
    "include_passed": False,
    "min_cap": 2e9,
    "max_cap": 0.0,
    "min_price": 15.0,
    "max_price": 500.0,
    "min_roi": 60.0,
    "min_oi": 100.0,
    "max_spread": 0.30,
    "min_dte": 1.0,
    "workers": 8,
}


# ---------------------------------------------------------------- 基础工具

def fetch_json(url: str, retries: int = 2, timeout: int = 25) -> dict:
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
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


def resolve_window(params: dict, now: datetime = None):
    """把 start/end/include_passed 解析成具体日期，并给出显示用的元信息。"""
    now = now or datetime.now(ET)
    today = now.date()
    mon, fri = week_bounds(today)
    p = dict(DEFAULTS, **(params or {}))
    include_passed = bool(p["include_passed"])
    start = date.fromisoformat(p["start"]) if p["start"] else (mon if include_passed else today)
    end = date.fromisoformat(p["end"]) if p["end"] else fri
    if end < start:
        raise ValueError("结束日期早于起始日期")
    return start, end, include_passed, now


# ---------------------------------------------------------------- 抓取层

def fetch_calendar(start: date, end: date, params: dict, now: datetime):
    """拉日历，做市值粗筛与已公布剔除。返回行、市值淘汰项、统计。"""
    include_passed = bool(params.get("include_passed"))
    min_cap = params.get("min_cap", DEFAULTS["min_cap"])
    max_cap = params.get("max_cap", 0) or 0

    raw_total, rows, cap_rejected, failed_days = 0, [], [], []
    for d in date_range(start, end):
        try:
            payload = fetch_json(CAL_URL.format(d=d.isoformat()))
        except RuntimeError as e:  # noqa: BLE001
            failed_days.append({"date": d.isoformat(), "error": str(e)})
            continue
        for it in (payload.get("data") or {}).get("rows") or []:
            raw_total += 1
            cap = num(it.get("marketCap"))
            if cap < min_cap or (max_cap and cap > max_cap):
                cap_rejected.append({
                    "symbol": it.get("symbol", ""), "name": it.get("name", ""),
                    "marketCap": cap, "date": d.isoformat(),
                    "time": TIME_LABEL.get(it.get("time", ""), "未定"),
                })
                continue
            label = TIME_LABEL.get(it.get("time", ""), "未定")
            if not include_passed and now >= report_cutoff(d, label):
                continue
            rows.append({
                "date": d.isoformat(), "symbol": it.get("symbol", ""),
                "name": it.get("name", ""), "time": label, "marketCap": cap,
                "epsForecast": num(it.get("epsForecast")),
                "lastYearEPS": num(it.get("lastYearEPS")),
                "expiry": expiry_for(d).isoformat(),
                "dte": max((expiry_for(d) - now.date()).days, 1),
            })

    # 同一代码一周内可能出现在多个日期，保留市值最大的一条
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda x: (x["date"], -x["marketCap"])):
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        uniq.append(r)
    uniq.sort(key=lambda x: -x["marketCap"])
    return {"rows": uniq, "cap_rejected": cap_rejected,
            "raw_total": raw_total, "failed_days": failed_days}


def nearest_expiry(symbol: str) -> str:
    """数据源里该股最近的到期日，用于区分"没期权"和"数据源没有周合约"。"""
    try:
        d = fetch_json(NEAREST_URL.format(s=symbol))["data"]
        for it in (d.get("table") or {}).get("rows") or []:
            g = it.get("expirygroup")
            if g:
                return g
    except Exception:  # noqa: BLE001
        pass
    return ""


def fetch_chain(symbol: str, expiry_iso: str) -> dict:
    """拉单只标的的当周到期期权链，压成紧凑结构。"""
    out = {"price": 0.0, "strikes": [], "alt": "", "error": ""}
    try:
        data = fetch_json(CHAIN_URL.format(s=symbol, e=expiry_iso)).get("data") or {}
    except Exception:  # noqa: BLE001
        out["error"] = "期权链拉取失败"
        return out

    m = re.search(r"\$([\d,]+\.?\d*)", data.get("lastTrade") or "")
    px = num(m.group(1)) if m else 0.0
    out["price"] = px
    if px <= 0:
        out["error"] = "取不到最新股价"
        return out

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

    if len(quotes) < 5:
        out["alt"] = nearest_expiry(symbol)
        out["strikes"] = quotes
        return out

    # 只保留现价 45%~108% 区间，够算 LEM 与阶梯图，体积可控
    out["strikes"] = [q for q in quotes if px * 0.45 <= q["k"] <= px * 1.08][:160]
    return out


def build_snapshot(start: date, end: date, params: dict, now: datetime = None,
                   progress=None, check_limit: int = 0) -> dict:
    """完整抓取一次，返回可缓存的原始快照（不含任何门槛判断）。"""
    now = now or datetime.now(ET)
    params = dict(DEFAULTS, **(params or {}))

    if progress:
        progress("calendar", 0, 1, "拉取财报日历")
    cal = fetch_calendar(start, end, params, now)
    rows = cal["rows"]
    if check_limit:
        rows = rows[:check_limit]
    total = len(rows)

    if progress:
        progress("chain", 0, total, "开始核查期权链")

    chains, done = {}, 0
    if rows:
        workers = max(1, min(int(params.get("workers") or 8), 16))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_chain, r["symbol"], r["expiry"]): r for r in rows}
            for f in as_completed(futs):
                r = futs[f]
                try:
                    chains[r["symbol"]] = f.result()
                except Exception:  # noqa: BLE001
                    chains[r["symbol"]] = {"price": 0.0, "strikes": [],
                                           "alt": "", "error": "期权链拉取失败"}
                done += 1
                if progress:
                    progress("chain", done, total, r["symbol"])

    for r in rows:
        r["chain"] = chains.get(r["symbol"]) or {"price": 0.0, "strikes": [],
                                                 "alt": "", "error": "无数据"}

    return {
        "meta": {
            "start": start.isoformat(), "end": end.isoformat(),
            "nowEt": now.strftime("%Y-%m-%d %H:%M"),
            "fetchedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
            "params": params,
        },
        "rows": rows,
        "cap_rejected": cal["cap_rejected"],
        "raw_total": cal["raw_total"],
        "failed_days": cal["failed_days"],
    }


# ---------------------------------------------------------------- 过滤层（纯函数）

def _reject(r: dict, cat: str, reason: str, gap: dict = None) -> dict:
    r["verdict"], r["cat"], r["reason"] = "淘汰", cat, reason
    r["gate"] = CAT_GATE.get(cat, 0)
    r["gap"] = gap or {}
    return r


def _roi(q: dict, dte: int) -> float:
    return q["pb"] / q["k"] * 365 / dte * 100 if q["pb"] > 0 else 0.0


def _spread(q: dict) -> float:
    if q["pb"] <= 0 or q["pa"] <= 0:
        return 9.99
    mid = (q["pb"] + q["pa"]) / 2
    return (q["pa"] - q["pb"]) / mid if mid > 0 else 9.99


def analyze(row: dict, params: dict) -> dict:
    """对单只标的跑六道门槛。纯函数，不联网。"""
    p = dict(DEFAULTS, **(params or {}))
    ch = row.get("chain") or {}
    r = {
        "symbol": row.get("symbol", ""), "name": row.get("name", ""),
        "date": row.get("date", ""), "time": row.get("time", ""),
        "marketCap": row.get("marketCap", 0.0),
        "epsForecast": row.get("epsForecast", 0.0),
        "lastYearEPS": row.get("lastYearEPS", 0.0),
        "expiry": row.get("expiry", ""), "dte": row.get("dte", 0),
        "price": ch.get("price", 0.0), "em": 0.0, "emPct": 0.0, "lem": 0.0,
        "strike1": 0.0, "prem1": 0.0, "roi1": 0.0, "oi1": 0.0, "spread1": 0.0,
        "strike2": 0.0, "prem2": 0.0, "roi2": 0.0, "oi2": 0.0, "spread2": 0.0,
        "cushion": 0.0, "ret": 0.0, "dteWarn": False,
        "breakeven": 0.0, "cash": 0.0, "score": 0.0,
        "highlights": [], "verdict": "", "cat": "", "reason": "", "gate": 0, "gap": {},
    }

    if ch.get("error"):
        return _reject(r, "数据缺失", ch["error"])

    px = ch.get("price", 0.0)
    if px <= 0:
        return _reject(r, "数据缺失", "取不到最新股价")

    # 第二道：股价区间（市值已在日历层粗筛，算第一道）
    if not (p["min_price"] <= px <= p["max_price"]):
        side = "high" if px > p["max_price"] else "low"
        return _reject(r, "股价超区间",
                       f"${px:.2f} 不在 ${p['min_price']:.0f}-{p['max_price']:.0f} 区间",
                       {"metric": "股价", "value": px,
                        "threshold": p["max_price"] if side == "high" else p["min_price"],
                        "dir": side, "unit": "$"})

    qs = sorted(ch.get("strikes") or [], key=lambda q: q["k"])

    # 第三道：有没有可交易的当周合约
    if len(qs) < 5:
        if ch.get("alt"):
            return _reject(r, "无当周合约",
                           f"没有 {row.get('expiry', '')} 到期的周合约，最近到期 {ch['alt']}")
        return _reject(r, "期权链缺失", f"当周到期仅 {len(qs)} 档合约，无期权或太冷门")

    # 剩余天数太少时年化 ROI 会被 365/DTE 放大，容易看成假机会
    dte_now = max(int(r["dte"] or 1), 1)
    if dte_now < p["min_dte"]:
        return _reject(r, "到期太近",
                       f"距到期仅 {dte_now} 天，年化 ROI 按 {365 / dte_now:.0f} 倍放大，参考价值低",
                       {"metric": "剩余天数", "value": dte_now,
                        "threshold": p["min_dte"], "dir": "low", "unit": "天"})

    atm = min(qs, key=lambda q: abs(q["k"] - px))
    if atm["cb"] <= 0 or atm["pb"] <= 0:
        return _reject(r, "流动性不足", "平值合约无有效买盘")
    em = (atm["cb"] + atm["ca"]) / 2 + (atm["pb"] + atm["pa"]) / 2
    if em <= 0:
        return _reject(r, "期权链缺失", "平值跨式无报价")

    lem = px - em
    r.update({"em": em, "emPct": em / px * 100, "lem": lem})
    below = [q for q in qs if q["k"] <= lem]
    if not below:
        return _reject(r, "期权链缺失", "LEM 下方无行权价，档位不足")

    k1 = below[-1]
    k2 = below[-2] if len(below) >= 2 else None
    dte = max(int(r["dte"] or 1), 1)

    r.update({"strike1": k1["k"], "prem1": k1["pb"], "roi1": _roi(k1, dte),
              "oi1": k1["poi"], "spread1": _spread(k1),
              "cushion": (px - k1["k"]) / px * 100,
              "ret": k1["pb"] / k1["k"] * 100,
              "dteWarn": int(r["dte"] or 1) <= 2,
              "breakeven": k1["k"] - k1["pb"], "cash": k1["k"] * 100})
    if k2:
        r.update({"strike2": k2["k"], "prem2": k2["pb"], "roi2": _roi(k2, dte),
                  "oi2": k2["poi"], "spread2": _spread(k2)})

    # 第四道：卖得出去吗
    if k1["pb"] <= 0:
        return _reject(r, "流动性不足", f"行权价 {k1['k']:g} 无买盘")
    if k1["poi"] < p["min_oi"]:
        return _reject(r, "流动性不足",
                       f"行权价 {k1['k']:g} 持仓量 {k1['poi']:.0f} < {p['min_oi']:g}",
                       {"metric": "持仓量", "value": k1["poi"],
                        "threshold": p["min_oi"], "dir": "low", "unit": "张"})

    # 第五道：点差能接受吗（卖方按买价成交，点差宽则平仓代价高）
    if r["spread1"] > p["max_spread"]:
        return _reject(r, "点差过宽",
                       f"行权价 {k1['k']:g} 点差 {r['spread1'] * 100:.1f}% > {p['max_spread'] * 100:.0f}%",
                       {"metric": "点差", "value": r["spread1"] * 100,
                        "threshold": p["max_spread"] * 100, "dir": "high", "unit": "%"})

    # 第六道：权利金够不够
    best = max(r["roi1"], r["roi2"])
    if best < p["min_roi"]:
        return _reject(r, "收益不足", f"年化 ROI 最高 {best:.0f}% < {p['min_roi']:.0f}%",
                       {"metric": "年化ROI", "value": best,
                        "threshold": p["min_roi"], "dir": "low", "unit": "%"})

    r["verdict"], r["cat"], r["reason"] = "通过", "", ""
    r["gate"] = 0
    r["score"] = _score(r)
    r["highlights"] = _highlights(r, p)
    return r


def _score(r: dict) -> float:
    """0-100 的综合分，权重透明：收益 35、安全垫 25、流动性 20、点差 15、结构 5。"""
    s = 0.0
    s += min(r["roi1"] / 150, 1) * 35
    s += min(r["cushion"] / 12, 1) * 25
    s += min(r["oi1"] / 1500, 1) * 20
    s += max(0.0, 1 - r["spread1"] / 0.30) * 15
    s += 5 if r["strike2"] else 0
    return round(s, 1)


def _highlights(r: dict, p: dict) -> list:
    """生成"为什么适合"的人话理由，前端直接展示，不让模型现编。"""
    out = []
    out.append(f"年化 {r['roi1']:.0f}%，是 {p['min_roi']:.0f}% 门槛的 "
               f"{r['roi1'] / p['min_roi']:.1f} 倍")
    out.append(f"卖 K={r['strike1']:g}，位于预期下界 LEM {r['lem']:.2f} 下方 "
               f"{r['lem'] - r['strike1']:.2f} 美元，安全垫 {r['cushion']:.1f}%")
    out.append(f"预期波动 ±{r['em']:.2f}（{r['emPct']:.1f}%），"
               f"财报 {r['date'][5:]} {r['time']}，到期 {r['expiry'][5:]}，"
               f"吃的是财报后 IV 塌陷")
    out.append(f"持仓 {r['oi1']:.0f} 张、点差 {r['spread1'] * 100:.0f}%，进出场代价可控")
    out.append(f"绝对收益 {r['ret']:.2f}%（{r['prem1']:.2f} / {r['strike1']:g}），"
               f"剩余 {r['dte']} 天" + ("；年化按天数放大，看绝对收益更实在"
                                       if r["dte"] <= 2 else ""))
    if r["lastYearEPS"]:
        delta = (r["epsForecast"] - r["lastYearEPS"]) / abs(r["lastYearEPS"]) * 100
        out.append(f"EPS 预期 {r['epsForecast']:.2f} vs 去年 {r['lastYearEPS']:.2f}"
                   f"（{delta:+.0f}%）")
    return out


def run(snapshot: dict, params: dict) -> dict:
    """在快照上重算全部门槛。改参数只走这一步，毫秒级。"""
    p = dict(DEFAULTS, **(params or {}))
    min_cap = p["min_cap"]
    max_cap = p["max_cap"] or 0

    # 市值不足的在快照里只有轻量信息，直接按门槛归入淘汰，保证总数对得上
    cap_rej = []
    for c in snapshot.get("cap_rejected") or []:
        if c["marketCap"] < min_cap or (max_cap and c["marketCap"] > max_cap):
            cap_rej.append({
                "symbol": c["symbol"], "name": c["name"], "date": c["date"],
                "time": c["time"], "marketCap": c["marketCap"],
                "price": 0.0, "verdict": "淘汰", "cat": "市值不足", "gate": 1,
                "reason": f"市值 {c['marketCap'] / 1e8:,.0f} 亿 < 门槛 {min_cap / 1e8:,.0f} 亿",
                "gap": {"metric": "市值", "value": c["marketCap"] / 1e8,
                        "threshold": min_cap / 1e8, "dir": "low", "unit": "亿"},
                "highlights": [],
            })

    results = [analyze(row, p) for row in snapshot.get("rows") or []]
    results += cap_rej

    passed = sorted([r for r in results if r["verdict"] == "通过"],
                    key=lambda r: (-r["score"], -r["roi1"]))
    rejected = [r for r in results if r["verdict"] != "通过"]
    # 淘汰按"卡在第几关"排，同关内按市值从大到小——名企浮到最上面
    rejected.sort(key=lambda r: (r["gate"], -r["marketCap"]))

    by_cat = {}
    for r in rejected:
        by_cat[r["cat"]] = by_cat.get(r["cat"], 0) + 1

    checked = len(snapshot.get("rows") or [])
    return {
        "meta": snapshot["meta"],
        "stats": {
            "calendarRows": snapshot.get("raw_total", 0),
            "capRejected": len(cap_rej),
            "checked": checked,
            "passed": len(passed),
            "rejected": len(rejected),
            "total": len(results),
            "byCat": by_cat,
            "catOrder": CAT_ORDER,
            "failedDays": snapshot.get("failed_days") or [],
        },
        "params": p,
        "passed": passed,
        "rejected": rejected,
    }


def detail(snapshot: dict, symbol: str) -> dict:
    """单只标的的期权链阶梯，供前端抽屉展示。"""
    row = next((r for r in snapshot.get("rows") or [] if r["symbol"] == symbol), None)
    if not row:
        return {"error": "快照里没有这只标的"}
    ch = row.get("chain") or {}
    px = ch.get("price", 0.0)
    dte = max(int(row.get("dte") or 1), 1)
    ladder = []
    for q in ch.get("strikes") or []:
        if q["k"] > px:
            continue
        ladder.append({
            "k": q["k"], "dist": (px - q["k"]) / px * 100 if px else 0,
            "bid": q["pb"], "ask": q["pa"], "oi": q["poi"],
            "roi": _roi(q, dte), "spread": _spread(q) * 100,
        })
    ladder.sort(key=lambda x: -x["k"])
    return {"symbol": symbol, "price": px, "expiry": row.get("expiry"),
            "dte": dte, "error": ch.get("error", ""), "alt": ch.get("alt", ""),
            "ladder": ladder[:40]}


if __name__ == "__main__":
    # 自检：不联网，只验证纯函数部分
    demo = {"symbol": "TEST", "name": "Demo Inc", "date": "2026-10-22", "time": "盘后",
            "marketCap": 5e10, "epsForecast": 1.2, "lastYearEPS": 1.0,
            "expiry": "2026-10-23", "dte": 2,
            "chain": {"price": 82.4, "alt": "", "error": "",
                      "strikes": [{"k": 82, "cb": 2.2, "ca": 2.4, "pb": 2.1, "pa": 2.3, "poi": 900},
                                  {"k": 78, "cb": 0.9, "ca": 1.0, "pb": 0.8, "pa": 0.9, "poi": 800},
                                  {"k": 77, "cb": 0.8, "ca": 0.9, "pb": 0.72, "pa": 0.8, "poi": 1200},
                                  {"k": 76, "cb": 0.7, "ca": 0.8, "pb": 0.6, "pa": 0.7, "poi": 400},
                                  {"k": 75, "cb": 0.6, "ca": 0.7, "pb": 0.5, "pa": 0.6, "poi": 300}]}}
    out = analyze(demo, DEFAULTS)
    print(json.dumps({k: out[k] for k in
                      ("symbol", "price", "em", "lem", "strike1", "roi1", "score",
                       "verdict", "cat", "reason")}, ensure_ascii=False, indent=2))
    print("highlights:", *out["highlights"], sep="\n  ")
