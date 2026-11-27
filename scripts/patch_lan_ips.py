#!/usr/bin/env python3
"""给 server.py 的 _lan_ips() 加进程内缓存 + 快速失败。

## 为什么必须改

`_lan_ips()` 会被 `_private_host()`（每个请求的 Host 校验）和
`_origin_ok()`（每个写操作的来源校验）调用，**每个 HTTP 请求都走一遍**。

原实现每次调用都执行两个可能长时间阻塞的操作：

  1. `socket.getaddrinfo(socket.gethostname(), ...)`
     macOS 上会走 mDNS/Bonjour 解析设备名，CI runner 里常常等满 DNS 超时才返回；
     iOS 沙箱里同理。
  2. `s.connect(("8.8.8.8", 80))`
     没有默认路由 / 出口被墙时，UDP connect 之后的行为取决于内核，可能长时间挂住。

实测后果：GitHub Actions 的 macos runner 上，后端起来后
`POST /api/config`（带跨站 Origin）会卡到客户端 8 秒超时，
`/api/ping` 也会被后续请求拖住 —— 页面在真机上同样会表现为「点了没反应」。

## 怎么改

  · 结果按进程缓存（TTL 300 秒），绝大多数请求直接读内存；
  · 首次探测限制在 0.2 秒内完成，超时就用「只有 127.0.0.1」的保守结果；
  · 探测放到后台线程，绝不阻塞请求处理线程。

安全性不变：`_lan_ips()` 只用于「Host 是否属于本机」和「Origin 是否为本机」的
判定；缓存的是本机自己的地址集合，降低的只是调用开销，不是校验强度。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET_REL = "signbot/server.py"

OLD = '''def _lan_ips() -> set:
    """
    本机所有 IPv4 地址（含回环）。

    原来只有 `_lan_ip()` 返回**一个**地址（靠 `connect(("8.8.8.8", 80))` 猜出口网卡），
    多网卡机器（WiFi + 有线 + Hyper-V/VMware 虚拟网卡）上手机连的那个 IP
    未必是它选中的那个 → Host 校验 403，整个后端不可用（BUGS-v2.40 的 P2-2）。

    这里枚举全部地址而不是退回单 IP：单 IP 写法会让多网卡 403 立刻回来，
    而「枚举 + 只用于 Host/本机 Origin 判定」既能修功能又不开安全口子。
    """
    out = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip:
                out.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            out.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return out
'''

NEW = '''_LAN_CACHE = {"ts": 0.0, "ips": {"127.0.0.1"}}
_LAN_CACHE_TTL = 300.0          # 5 分钟刷新一次足够（地址变动是低频事件）
_LAN_PROBE_BUDGET = 0.2         # 单次探测最多 0.2 秒，超时就先用保守结果
_LAN_LOCK = threading.Lock()
_LAN_PROBING = {"on": False}


def _probe_lan_ips() -> set:
    """真正去问系统要本机地址。只允许在后台线程调用。"""
    out = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip:
                out.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(_LAN_PROBE_BUDGET)
            s.connect(("8.8.8.8", 80))
            out.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return out


def _refresh_lan_ips_async() -> None:
    """后台刷新，绝不阻塞请求线程。"""
    with _LAN_LOCK:
        if _LAN_PROBING["on"]:
            return
        _LAN_PROBING["on"] = True

    def _work():
        try:
            ips = _probe_lan_ips()
            with _LAN_LOCK:
                _LAN_CACHE["ips"] = ips
                _LAN_CACHE["ts"] = time.time()
        finally:
            with _LAN_LOCK:
                _LAN_PROBING["on"] = False

    try:
        threading.Thread(target=_work, name="lan-probe", daemon=True).start()
    except Exception:
        with _LAN_LOCK:
            _LAN_PROBING["on"] = False


def _lan_ips() -> set:
    """本机所有 IPv4 地址（含回环）。**带缓存，不阻塞请求路径。**

    iOS 移植补丁：原实现每次调用都做 getaddrinfo(gethostname) 与
    connect(8.8.8.8)，在 macOS / iOS / 受限网络下会挂住几十秒，
    而这个函数每个 HTTP 请求都会被 _private_host() 与 _origin_ok() 调到。
    现在改成：读缓存 → 过期就丢给后台线程刷新 → 立刻返回旧值。
    """
    now = time.time()
    with _LAN_LOCK:
        ips = set(_LAN_CACHE["ips"])
        fresh = (now - _LAN_CACHE["ts"]) < _LAN_CACHE_TTL
    if not fresh:
        _refresh_lan_ips_async()
    # 保证回环永远在集合里（_refresh 失败时也不会误判本机请求）
    ips.add("127.0.0.1")
    return ips


def warm_lan_ips() -> None:
    """启动时预热：让首个真实请求不必等探测。

    在 serve() 里、绑定端口之后调用。只丢后台线程，不阻塞启动。
    """
    with _LAN_LOCK:
        stale = (time.time() - _LAN_CACHE["ts"]) >= _LAN_CACHE_TTL
    if stale:
        _refresh_lan_ips_async()
'''


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("app")
    target = root / TARGET_REL
    if not target.is_file():
        print(f"[FAIL] 找不到 {target}")
        return 1

    text = target.read_text(encoding="utf-8")

    if "_LAN_CACHE_TTL" not in text:
        if OLD not in text:
            print("[FAIL] 未找到原始 _lan_ips 实现，上游可能已改动")
            m = re.search(r"def _lan_ips\(\).*?(?=\ndef )", text, re.S)
            if m:
                print("实际实现：")
                print(m.group(0)[:800])
            return 1
        text = text.replace(OLD, NEW, 1)
        print(f"[ok] {TARGET_REL}: _lan_ips 已加缓存 + 异步探测")
    else:
        print("[skip] _lan_ips 缓存补丁已存在")

    # ---- 把 warm_lan_ips() 挂进 serve()：启动即预热，首个请求不等探测
    if "warm_lan_ips()" not in text.split("def warm_lan_ips")[-1]:
        anchor = "    db.init_db()\n"
        if anchor in text:
            text = text.replace(
                anchor,
                anchor + "    # iOS 移植补丁：启动即预热本机地址缓存，\n"
                         "    # 免得首个请求还要等 getaddrinfo 探测。\n"
                         "    warm_lan_ips()\n",
                1,
            )
            print("[ok] serve(): 已注入 warm_lan_ips()")
        else:
            print("[warn] 未找到 db.init_db() 锚点，跳过预热注入")

    target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
