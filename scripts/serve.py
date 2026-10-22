#!/usr/bin/env python3
"""财报期权扫描 · 本地 Web 服务（只用 Python 标准库）。

启动：
    python3 scripts/serve.py            # 默认 http://127.0.0.1:8765
    python3 scripts/serve.py --port 9000 --open

为什么不用 Flask：技能要能在任何装了 python3 的机器上直接跑，不能要求 pip install。
一个 ThreadingHTTPServer 足够。

接口：
    GET  /                      Dashboard 页面
    GET  /api/meta              默认参数、当前美东时间、本周区间
    POST /api/scan   {params}   启动抓取任务（有缓存则秒回），返回 job id
    GET  /api/progress?job=     任务进度
    POST /api/filter {job,params}  用已有快照重算门槛（毫秒级，不联网）
    GET  /api/detail?job=&symbol=  单只标的的期权链阶梯
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_engine as eng  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")
CACHE = os.path.join(ROOT, ".cache", "snapshot.json")
CACHE_TTL = 1800  # 秒；超过这个时间重新抓，避免用半天前的报价下单

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".ico": "image/x-icon"}

JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 6


# ---------------------------------------------------------------- 任务管理

def _job_new(job_id: str, params: dict, key: str) -> dict:
    job = {
        "id": job_id, "key": key, "params": params,
        "phase": "queued", "done": 0, "total": 0, "current": "",
        "error": "", "finished": False, "cached": False,
        "startedAt": time.time(), "elapsed": 0.0,
        "snapshot": None, "result": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
        # 只留最近几个，避免长时间运行时内存堆积
        for old in sorted(JOBS.values(), key=lambda j: j["startedAt"])[:-MAX_JOBS]:
            JOBS.pop(old["id"], None)
    return job


def _cache_key(start, end, params: dict) -> str:
    return "|".join([start.isoformat(), end.isoformat(),
                     f"{params['min_cap']:.0f}", f"{params.get('max_cap') or 0:.0f}",
                     "1" if params.get("include_passed") else "0"])


def _cache_load(key: str):
    try:
        with open(CACHE, encoding="utf-8") as f:
            blob = json.load(f)
        if blob.get("key") != key:
            return None
        if time.time() - blob.get("savedAt", 0) > CACHE_TTL:
            return None
        return blob.get("snapshot")
    except Exception:  # noqa: BLE001
        return None


def _cache_save(key: str, snapshot: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": key, "savedAt": time.time(), "snapshot": snapshot},
                      f, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 快照写缓存失败：{e}", file=sys.stderr)


def _run_scan(job: dict, start, end, params: dict, now_et) -> None:
    def progress(phase, done, total, current):
        job.update(phase=phase, done=done, total=total, current=current)

    try:
        job["phase"] = "calendar"
        snap = eng.build_snapshot(start, end, params, now_et, progress=progress)
        job["snapshot"] = snap
        job["result"] = eng.run(snap, params)
        _cache_save(job["key"], snap)
        job["phase"] = "done"
    except Exception as e:  # noqa: BLE001
        job["error"] = f"{e}"
        job["phase"] = "error"
        traceback.print_exc()
    finally:
        job["finished"] = True
        job["elapsed"] = round(time.time() - job["startedAt"], 1)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "EarningsIVCrush/1.0"

    def log_message(self, fmt, *args):  # 安静一点，只报错误
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # --- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _file(self, rel: str):
        path = os.path.normpath(os.path.join(WEB, rel.lstrip("/")))
        if not path.startswith(WEB) or not os.path.isfile(path):
            return self._err("not found", 404)
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- 路由
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._file("index.html")
        if p == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if p == "/api/meta":
            now = datetime.now(eng.ET)
            mon, fri = eng.week_bounds(now.date())
            return self._json({
                "defaults": eng.DEFAULTS,
                "nowEt": now.strftime("%Y-%m-%d %H:%M"),
                "today": now.date().isoformat(),
                "week": [mon.isoformat(), fri.isoformat()],
                "catOrder": eng.CAT_ORDER,
                "catGate": eng.CAT_GATE,
                "sources": {"calendar": eng.CAL_URL.format(d="YYYY-MM-DD"),
                            "chain": eng.CHAIN_URL.format(s="<SYM>", e="<到期日>")},
            })
        if p == "/api/progress":
            job = JOBS.get((q.get("job") or [""])[0])
            if not job:
                return self._err("任务不存在或已过期", 404)
            return self._json({
                "phase": job["phase"], "done": job["done"], "total": job["total"],
                "current": job["current"], "error": job["error"],
                "finished": job["finished"], "cached": job["cached"],
                "elapsed": round(time.time() - job["startedAt"], 1),
                "startedAt": job["startedAt"],
            })
        if p == "/api/result":
            job = JOBS.get((q.get("job") or [""])[0])
            if not job:
                return self._err("任务不存在或已过期", 404)
            if job["error"]:
                return self._err(job["error"], 500)
            if not job["result"]:
                return self._err("任务尚未完成", 409)
            return self._json(job["result"])
        if p == "/api/detail":
            job = JOBS.get((q.get("job") or [""])[0])
            if not job or not job["snapshot"]:
                return self._err("任务不存在或未完成", 404)
            return self._json(eng.detail(job["snapshot"], (q.get("symbol") or [""])[0]))

        return self._file(p)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()

        if u.path == "/api/scan":
            params = dict(eng.DEFAULTS, **(body.get("params") or {}))
            try:
                start, end, _, now_et = eng.resolve_window(params)
            except ValueError as e:
                return self._err(str(e))
            key = _cache_key(start, end, params)
            job_id = f"{int(time.time() * 1000)}-{abs(hash(key)) % 10000}"
            job = _job_new(job_id, params, key)

            if not body.get("refresh"):
                snap = _cache_load(key)
                if snap:
                    job.update(snapshot=snap, result=eng.run(snap, params),
                               phase="done", finished=True, cached=True,
                               elapsed=0.0, total=len(snap.get("rows") or []),
                               done=len(snap.get("rows") or []))
                    return self._json({"job": job_id, "cached": True})

            t = threading.Thread(target=_run_scan, args=(job, start, end, params, now_et),
                                 daemon=True)
            t.start()
            return self._json({"job": job_id, "cached": False})

        if u.path == "/api/filter":
            job = JOBS.get(body.get("job") or "")
            if not job or not job["snapshot"]:
                return self._err("任务不存在或未完成", 404)
            params = dict(eng.DEFAULTS, **(body.get("params") or {}))
            job["params"] = params
            return self._json(eng.run(job["snapshot"], params))

        return self._err("not found", 404)


def main() -> None:
    ap = argparse.ArgumentParser(description="财报期权扫描 Dashboard")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    if not os.path.isfile(os.path.join(WEB, "index.html")):
        print(f"找不到页面：{os.path.join(WEB, 'index.html')}", file=sys.stderr)
        sys.exit(1)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = f"http://{args.host}:{args.port}"
    print(f"财报期权扫描 Dashboard 已启动：{url}")
    print("按 Ctrl+C 停止。")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
