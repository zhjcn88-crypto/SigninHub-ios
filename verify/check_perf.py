#!/usr/bin/env python3
"""请求路径性能检查：确认每个 HTTP 请求不会因「探测本机地址」而阻塞。

## 为什么要单列这一项

`_lan_ips()` 被 `_private_host()`（每个请求的 Host 校验）和 `_origin_ok()`
（每个写操作的来源校验）调用 —— 也就是说**每个请求都走一遍**。

上游的实现每次调用都做：
    socket.getaddrinfo(socket.gethostname(), ...)   # macOS/iOS 上会走 mDNS，慢
    s.connect(("8.8.8.8", 80))                     # 无默认路由时会挂

在 GitHub Actions 的 macOS runner 上实测：后端起来后每个请求要卡 2–3 秒，
客户端 8 秒超时；真机上表现为「点了没反应」。
`scripts/patch_lan_ips.py` 给它加了缓存 + 后台探测，这里验证补丁确实生效。
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from _common import APP, Report, TMP, require

r = Report("请求路径性能（_lan_ips 缓存）")

APP_DIR = APP / "app"
if not require(APP_DIR / "signbot" / "server.py"):
    sys.exit(1)

# 补丁存在性（最便宜的先查）
srv_src = (APP_DIR / "signbot" / "server.py").read_text(encoding="utf-8")
r.check("server.py 已打 _lan_ips 缓存补丁", "_LAN_CACHE_TTL" in srv_src)
r.check("serve() 已注入 warm_lan_ips()", "warm_lan_ips()" in srv_src)
if "_LAN_CACHE_TTL" not in srv_src:
    sys.exit(r.finish())

DATA = TMP / "perf_data"
shutil.rmtree(DATA, ignore_errors=True)
os.environ["SIGNBOT_DATA"] = str(DATA)
sys.path.insert(0, str(APP_DIR))

# 在 import 之前把「服务端探测本机地址」这条路变慢：
# 只针对 getaddrinfo(gethostname) 与 connect(8.8.8.8)，
# 客户端解析 127.0.0.1 字面量不受影响（否则会把结论污染成假失败）。
HOSTNAME = socket.gethostname()
SLOW_HITS: list[tuple] = []
_real_gai = socket.getaddrinfo
_real_connect = socket.socket.connect


def slow_gai(host, *a, **kw):
    if isinstance(host, str) and host == HOSTNAME:
        SLOW_HITS.append(("gai", host))
        time.sleep(2.0)
    return _real_gai(host, *a, **kw)


def slow_connect(self, addr):
    if addr[:1] == ("8.8.8.8",):
        SLOW_HITS.append(("connect", addr))
        time.sleep(2.0)
        raise OSError("no route")
    return _real_connect(self, addr)


socket.getaddrinfo = slow_gai
socket.socket.connect = slow_connect

from signbot import server as S  # noqa: E402

t0 = time.time()
ips = S._lan_ips()
dt = time.time() - t0
r.check(f"_lan_ips() 首次调用不阻塞（{dt*1000:.0f}ms）", dt < 0.05, f"{dt*1000:.0f}ms")
r.check("回环始终在结果里", "127.0.0.1" in ips)

import _ios_boot  # noqa: E402
_ios_boot.prepare_server()

PORT = 18992
BASE = f"http://127.0.0.1:{PORT}"
threading.Thread(target=lambda: S.serve(host="127.0.0.1", port=PORT), daemon=True).start()


def wait_ready(timeout=40):
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            with urllib.request.urlopen(f"{BASE}/api/ping", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


r.check("后端就绪", wait_ready())
time.sleep(4.5)          # 等后台探测把两个 2 秒跑完
SLOW_HITS.clear()


def timed_post(origin, payload=b'{"scheduler":{"times":["11:00"]}}'):
    t0 = time.time()
    req = urllib.request.Request(
        f"{BASE}/api/config", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = f"ERR:{e}"
    return code, (time.time() - t0) * 1000


for i in range(3):
    code, ms = timed_post("https://evil.example")
    r.check(f"跨站 POST #{i+1} 被拒且 <300ms（{code} {ms:.0f}ms）",
            code in (401, 403) and ms < 300, f"{code} {ms:.0f}ms")

code, ms = timed_post(BASE)
r.check(f"本机 POST 通过且 <300ms（{code} {ms:.0f}ms）", code == 200 and ms < 300,
        f"{code} {ms:.0f}ms")

r.check("请求线程未触发慢探测", len(SLOW_HITS) == 0, str(SLOW_HITS))

t0 = time.time()
with urllib.request.urlopen(f"{BASE}/", timeout=6) as resp:
    n = len(resp.read())
ms = (time.time() - t0) * 1000
r.check(f"首屏 / 返回 {n} 字节且 <300ms（{ms:.0f}ms）", n > 1000 and ms < 300,
        f"{ms:.0f}ms")

sys.exit(r.finish())
