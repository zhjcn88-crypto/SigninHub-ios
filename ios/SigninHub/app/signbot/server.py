"""HTTP 服务：REST API + 内置 Web 控制台。

运行：python -m signbot.server    （在 backend/ 目录下）
"""
import json
import hmac
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime

from . import db, runner, notify
from .config import load_config, save_config, DATA_DIR
from .provider import build_providers, readiness

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

# 与 android/app/build.gradle.kts 的 versionName 保持一致：改版本时两处一起改。
# 前端设置页「高级」里会显示这个号，用来确认装的/连的是哪一版。
APP_VERSION = "2.63"

# 视为「仅本机可访问」的监听地址（启动横幅据此决定是否打安全提示）
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "")
# 请求来自这些地址时视为本机控制台
_LOOPBACK_CLIENTS = ("127.0.0.1", "::1")


def _mask(v: str) -> str:
    if not isinstance(v, str):
        return v
    if len(v) <= 6:
        return "***"
    return v[:3] + "***" + v[-2:]


def _crypto_brief():
    """(crypto_ok, creds_lost) —— 电脑端凭据加密状态，供前端横幅判断。

    BUGS-v2.63-291 P2-2：以前只看 `vault.failures()`（解密失败名单），
    加密降级（DPAPI 不可用 / vault.key 建不出来 → 明文落盘）完全看不见，
    于是前端横幅一直显示「加密正常」。现在两个方向都报：
      · 解密失败 → creds_lost，横幅提示「系统密钥已变更，请重新填写凭据」；
      · 加密失败 → crypto_ok=False，横幅提示「本机凭据未加密」。
    BUGS-v2.63-293 P3：`encrypt_failures()` 是「保存时的副作用」，点一次「知道了」
    （`reset_failures()`）或重启进程后就空了，于是永久报 crypto_ok=true。
    这里再加一道**主动能力探测** `vault.can_encrypt()`：不看历史记录，现场试一次。
    """
    try:
        from . import vault
        lost = vault.failures()
        enc = vault.encrypt_failures()
        try:
            can = vault.can_encrypt()
        except Exception:
            can = False
        return (len(lost) == 0 and len(enc) == 0 and can), lost
    except Exception:
        # vault 模块自己挂了 = 这轮一定没有加密。宁可横幅多弹一次，
        # 也不能在凭据可能明文落盘时告诉前端「一切正常」。
        return False, []


# ---------------------------------------------------------------------------
# 口令爆破限速（BUGS-v2.61 的 M7）
#
# 原来 `_check_auth` 是纯比较，没有任何失败计数 —— 局域网里跑个 hydra 就能
# 离线爆破 api_token（默认还是空串）。这里按来源 IP 记失败次数：
#   - 60 秒窗口内失败 5 次 → 锁 60 秒；再犯翻倍（60/120/240…上限 30 分钟）；
#   - 成功一次立即清零，避免正常用户手抖输错被长期拖住。
# 状态只在内存里（重启清零）—— 这是有意的：落盘反而给了攻击者一个
# 「重启服务就重置计数器」以外的另一种玩法（改文件）。
# ---------------------------------------------------------------------------
_AUTH_FAIL_WINDOW = 60.0
_AUTH_FAIL_LIMIT = 5
_AUTH_LOCK_MAX = 1800.0
_auth_state = {}          # {ip: [fail_times(list), lock_until(float), strikes(int)]}
_auth_lock = threading.Lock()


def _throttled(ip: str) -> bool:
    """该来源是否仍在锁定窗口内。"""
    now = time.time()
    with _auth_lock:
        st = _auth_state.get(ip)
        if not st:
            return False
        return st["lock_until"] > now


def _note_attempt(ip: str, ok: bool) -> None:
    """记一次口令尝试结果。"""
    now = time.time()
    with _auth_lock:
        st = _auth_state.setdefault(ip, {"fails": [], "lock_until": 0.0, "strikes": 0})
        if ok:
            st["fails"] = []
            st["lock_until"] = 0.0
            st["strikes"] = 0
            return
        st["fails"] = [t for t in st["fails"] if now - t < _AUTH_FAIL_WINDOW]
        st["fails"].append(now)
        if len(st["fails"]) >= _AUTH_FAIL_LIMIT:
            st["strikes"] += 1
            wait = min(_AUTH_FAIL_WINDOW * (2 ** (st["strikes"] - 1)), _AUTH_LOCK_MAX)
            st["lock_until"] = now + wait
            st["fails"] = []
        # 表太大时清掉过期项，防止被随机源地址刷爆内存
        if len(_auth_state) > 4096:
            for k in [k for k, v in _auth_state.items() if v["lock_until"] < now and not v["fails"]]:
                _auth_state.pop(k, None)


# 安全响应头（BUGS-v2.61 的 M6）。
# 后端原来的响应只有 Server/Date/Content-Type/Content-Length —— 没有 CSP，
# 于是任何一处 XSS（哪怕是用户自己填的 webhook 名回显）都能直接执行脚本，
# 而脚本能读 localStorage 里的 api_token 再打到局域网其它设备。
# 页面里没有内联 <script>、没有 <style> 标签、没有 onclick=（实测），
# 所以 script-src 可以收到 'self'；style 属性有 17 处，style-src 必须留 'unsafe-inline'。
SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
     "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
     "frame-ancestors 'none'; frame-src 'none'; base-uri 'none'; form-action 'self'; "
     "object-src 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
)


# ---------------------------------------------------------------------------
# 打上游的只读接口：最小间隔 + 结果缓存（BUGS-v2.61 的 M8 / N1）
#
# `/api/wb_assets?refresh=1`、`/api/growth`、`/api/growth/daily`、`/api/heat`
# 这几个 GET 都会**带着用户的 accessToken 打上游**：wb_assets 会去查资源包、
# growth 会去查成长值、heat 会去查热力图，doctor 还会对配置里的代理地址做
# 3 秒 TCP 探测。原来它们挂在「空口令即放行」的只读分支上，同网段任何人
# 循环 GET 就能：① 把用户的额度刷成上游限流；② 拿 doctor 当内网端口扫描器。
#
# 两道闸：
#   1. 同一个 key 在 `_UPSTREAM_MIN_INTERVAL` 内只真正打一次上游，其余请求
#      直接拿缓存 —— 刷新按钮连点、SW 重放、恶意循环都变成读内存。
#   2. 每个来源 IP 对打上游接口有每分钟次数上限，超了 429。
# 缓存只在内存里，进程重启即失效，不会让「刚签完到」的状态长期陈旧。
# ---------------------------------------------------------------------------
_UPSTREAM_MIN_INTERVAL = 60.0     # 同一 key 两次真打上游的最小间隔（秒）
_UPSTREAM_HITS_PER_MIN = 20       # 每个 IP 每分钟最多触发几次上游查询
_upstream_cache = {}              # {key: (ts, payload)}
_upstream_hits = {}               # {ip: [timestamps]}
_upstream_lock = threading.Lock()


def _upstream_budget(ip: str) -> bool:
    """该 IP 这一分钟还有没有触发上游查询的额度。"""
    now = time.time()
    with _upstream_lock:
        hits = [t for t in _upstream_hits.get(ip, []) if now - t < 60.0]
        if len(hits) >= _UPSTREAM_HITS_PER_MIN:
            _upstream_hits[ip] = hits
            return False
        hits.append(now)
        _upstream_hits[ip] = hits
        if len(_upstream_hits) > 4096:
            for k in [k for k, v in _upstream_hits.items() if not v or now - v[-1] > 60.0]:
                _upstream_hits.pop(k, None)
        return True


def _cached_upstream(key: str, fn, ttl: float = _UPSTREAM_MIN_INTERVAL):
    """
    带最小间隔的上游查询。`ttl` 内重复调用直接返回上次结果。

    `fn()` 抛异常时**不写缓存** —— 否则一次网络抖动会让接口在接下来一分钟里
    一直返回同一个错误。
    """
    now = time.time()
    with _upstream_lock:
        hit = _upstream_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _upstream_lock:
        _upstream_cache[key] = (now, val)
    return val


# provider key → 它的直连配置块名（proxy 等连接参数都在这里）
_PROV_BLOCK = {
    "workbuddy": "wb",
    "traesolo": "trae",
    "qoder": "qoder",
    "minimax": "minimax",
    "huawei": "huawei",
}


def _prov_block(key: str) -> str:
    return _PROV_BLOCK.get(key, key)


def _proxy_of(key: str, pcfg: dict) -> str:
    """取某 provider 的 proxy 字段（可能不存在 / 可能是非 dict，一律安全降级成 ""）。"""
    try:
        blk = (pcfg or {}).get(_prov_block(key))
        if not isinstance(blk, dict):
            return ""
        return str(blk.get("proxy") or "").strip()
    except Exception:
        return ""


def _proxy_check(key: str, pcfg: dict) -> str:
    """
    代理可达性预检：返回 "" 表示没问题，否则返回一句人话原因。

    BUGS-v2.40 的 P1-2：上一轮测试把 `qoder.proxy` 写成了
    `http://127.0.0.1:19999` 没还原，Qoder 每次签到都报
    「网络请求失败: <urlopen error [WinError 10061] ...>」——
    看起来像网络问题，其实是「填了一个不存在的代理」。这里提前说清楚。

    ⚠️ 这个函数会被 `/api/status`、`/api/doctor` 每次调用，**任何异常都必须
    自己吞掉**：探测失败时如果 raise，整个状态接口会 500，前端就「连接失败」，
    比不检查更糟（V2.41 自测就踩了这个 —— 原来 except 分支里 `str(...).get()`
    直接抛 AttributeError）。
    """
    proxy = _proxy_of(key, pcfg)
    if not proxy:
        return ""
    try:
        u = urlparse(proxy if "://" in proxy else "http://" + proxy)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host:
            return "代理地址无法解析：%s" % proxy
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect((host, port))
        finally:
            s.close()
        return ""
    except Exception as e:
        return "代理不可达（%s）：%s。签到会报「网络错误」，请先修代理或清空 proxy 字段。" % (proxy, e)


_PROXY_CACHE = {}          # {(key, proxy): (ts, reason)}
_PROXY_TTL = 60.0          # 秒
_PROXY_LOCK = threading.Lock()


def _proxy_check_cached(key: str, pcfg: dict, probe: bool = True) -> str:
    """
    带 60 秒缓存的 `_proxy_check`。

    `/api/status` 每 25 秒被前端轮询一次，如果每次都做 3 秒 TCP 探测，
    一个代理不通就会把整个状态接口拖到 3 秒以上（下拉刷新的手感全毁）。
    缓存键带上 proxy 字符串本身，改了地址立刻重新探测。

    `probe=False`（`/api/status` 用）：只读缓存，没有就在后台线程里探一次 ——
    状态接口必须立刻返回，绝不能在请求线程里等 TCP 超时。
    """
    proxy = _proxy_of(key, pcfg)
    if not proxy:
        return ""
    ck = (key, proxy)
    now = time.time()
    with _PROXY_LOCK:
        hit = _PROXY_CACHE.get(ck)
        fresh = bool(hit) and (now - hit[0] < _PROXY_TTL)
        if fresh:
            return hit[1]
        if not probe:
            # 不阻塞：先返回上一次的结论（可能已过期，也可能还没有 → ""），
            # 后台补测。状态接口每 25 秒被轮询一次，绝不能在这里等 TCP 超时。
            stale = hit[1] if hit else ""
            threading.Thread(
                target=lambda: _proxy_check_cached(key, pcfg, True),
                daemon=True, name="proxyprobe").start()
            return stale
    reason = _proxy_check(key, pcfg)
    with _PROXY_LOCK:
        _PROXY_CACHE[ck] = (now, reason)
    return reason


# 「重置凭据」要清空的字段（用户 m21385）。
#
# 只清凭据本身，**保留** region / mode / proxy / host —— 那些是「怎么连」的偏好，
# 不是「我是谁」的凭据。清掉 region 会让国际版账号下次连到国内域名，症状是
# 401 而不是「未配置」，排查起来很绕。
RESET_FIELDS = {
    "workbuddy": {"wb": ("accessToken", "refreshToken", "uid", "uin", "enterpriseId", "domain")},
    "traesolo": {"trae": ("token", "session", "deviceId")},
    "qoder": {"qoder": ("token", "refreshToken", "uid", "machineId")},
    "minimax": {"minimax": ("accessToken", "refreshToken", "userId", "deviceId", "uuid")},
    "huawei": {"huawei": ("cookie", "portalCookie")},
}


def _masked_config(cfg: dict) -> dict:
    """返回给前端的配置：遮蔽 Cookie/Token 等敏感信息。"""
    c = json.loads(json.dumps(cfg))
    for key, p in (c.get("providers") or {}).items():
        h = p.get("http", {})
        hdrs = h.get("headers", {})
        for hk in list(hdrs.keys()):
            if hk.lower() in ("cookie", "authorization", "token", "x-token"):
                hdrs[hk] = _mask(hdrs[hk]) if hdrs[hk] else ""
        # 各直连模式专属块里的凭据同样遮蔽（前端靠 placeholder 提示「已保存」）
        for blk, fields in (
            ("qoder", ("token", "refreshToken")),
            ("minimax", ("accessToken", "refreshToken")),
            ("wb", ("accessToken", "refreshToken")),
            ("trae", ("token", "session")),
            ("huawei", ("cookie", "portalCookie")),
        ):
            sub = p.get(blk)
            if not isinstance(sub, dict):
                continue
            for f in fields:
                if sub.get(f):
                    sub[f] = _mask(str(sub[f]))
    srv = c.get("server", {})
    raw_token = (srv.get("api_token") or "")
    srv["api_token_set"] = bool(raw_token.strip())
    srv["api_token"] = _mask(raw_token) if raw_token else ""
    # P3（BUGS-v2.40）：电脑端也透出凭据加密状态，与 Android 侧口径一致 ——
    # 前端凭据失效横幅（#credsAlert）读的就是这两个字段。
    srv["crypto_ok"], srv["creds_lost"] = _crypto_brief()
    ntf = c.get("notify")
    if isinstance(ntf, dict) and ntf.get("secret"):
        ntf["secret"] = _mask(ntf["secret"])
    return c


def _assets_for_card(cfg):
    """首屏只读缓存，不打上游。没查过就 pending。"""
    try:
        from . import wb_assets
        wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
        return wb_assets.cached_only(wb_cfg)
    except Exception:
        return {"ok": False, "pending": True}


def _notify_brief(cfg):
    n = cfg.get("notify") or {}
    try:
        from . import notify as nf
        kind = nf.detect_kind(n.get("webhook_url") or "", n.get("kind") or "wecom")
    except Exception:
        kind = n.get("kind") or "wecom"
    url = (n.get("webhook_url") or "").strip()
    return {"enabled": bool(n.get("enabled")), "kind": kind,
            "configured": bool(url), "has_url": bool(url)}


class Handler(BaseHTTPRequestHandler):
    # L3（BUGS-v2.61）：原来 server_version 拼了 APP_VERSION，每个响应都回
    # `Server: SigninHub/2.61 Python/3.12.8` —— 等于主动告诉扫描器「这是
    # SigninHub，版本 2.61」，配套的漏洞清单可以照着打。版本号只在 /api/ping
    # 里给前端用，HTTP 头里不需要。
    server_version = "SigninHub"
    sys_version = ""

    def version_string(self) -> str:
        # 基类实现是 `server_version + " " + sys_version`，sys_version 置空后会
        # 留下一个尾随空格（`Server: SigninHub `）。这里直接返回纯名字。
        return self.server_version

    # L5（BUGS-v2.61）：默认 protocol_version 是 HTTP/1.0，每个请求都要重新
    # 建 TCP 连接（前端首屏要拉 index + css + js + 10 张 logo，等于 13 次握手）。
    # 所有响应都带 Content-Length（含 send_error），可以安全开 keep-alive。
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------
    #
    # 安全模型（BUGS-v2.25 的 P0-1 / P0-2 / P1-1 / P2-2）：
    #
    # 原来只有「源 IP 是不是 127.0.0.1」这一道判断，但浏览器在本机发起的请求
    # 源 IP 天然就是 127.0.0.1 —— 于是「任意恶意网页的 JS 读本机后端」这个场景
    # 完全没被挡住。加上响应带 `Access-Control-Allow-Origin: *`，跨站 fetch 能
    # 直接读到响应体，refreshToken 就被顺走了。
    #
    # 现在改成三层：
    #   ① Host 白名单（挡 DNS rebinding：evil.example 解析到 127.0.0.1）
    #   ② Origin/Referer 白名单（挡跨站 CSRF：evil.example 页面发起的 POST）
    #   ③ CORS 只回显白名单 Origin，绝不发 `*`
    #
    # 注意 ①②③ 都不能单独用 —— 缺一个就漏一种攻击面。

    def _private_host(self, host: str) -> bool:
        """
        主机名是否属于「本机 / 局域网」—— **只给 Host 头校验用**。

        V2.27 修正（用户 m18499「这一版完全用不了」）：原来只放行
        `{127.0.0.1, localhost, ::1, _lan_ip()}`，两个真实场景被误杀：

        ① 手机 App 的 WebView 页面源是 `https://appassets.androidplatform.net`
           （WebViewAssetLoader 的虚拟域），它的 Origin 就是这个域名 ——
           白名单里没有 → 手机连电脑端时所有写操作 403、CORS 不回显 → 全站不可用。
        ② `_lan_ip()` 只返回一个地址，但多网卡机器（WiFi + 有线 + Hyper-V/VMware
           虚拟网卡）上手机连的那个 IP 未必是它选中的那个 → Host 校验 403。

        所以 Host 侧改成「私网地址一律可信」：RFC1918 三段 + 回环 + 本机名 + appassets。
        DNS rebinding 仍然挡得住 —— 攻击者用的域名（evil.example）既不是 IP
        也不在白名单里，解析后 Host 头仍是域名本身。

        **不要把这里放宽的结论用到 Origin 上**（见 `_origin_ok`）：
        Host 是客户端自己填的，Origin 是浏览器强制的；Host 放宽只影响
        「能不能连上」，Origin 放宽会直接变成「任意同网段网页都能读写接口」。
        """
        h = (host or "").lower().strip().strip(".")
        if not h:
            return False
        if h in ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"):
            return True
        if h == "appassets.androidplatform.net":
            return True
        try:
            if h in _lan_ips():
                return True
        except Exception:
            pass
        # RFC1918 / 链路本地：10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16
        try:
            parts = h.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                a, b = int(parts[0]), int(parts[1])
                if a == 10 or a == 127:
                    return True
                if a == 172 and 16 <= b <= 31:
                    return True
                if a == 192 and b == 168:
                    return True
                if a == 169 and b == 254:
                    return True
        except Exception:
            pass
        return False

    def _origin_ok(self, origin: str) -> bool:
        """
        Origin 是否属于「本机控制台 / App 自带页面 / 本机自己的局域网地址」。

        **这里是安全边界，不能像 Host 那样按私网整段放行**（BUGS-v2.40 的 P0）：
        v2.27 把 `_origin_ok` 也接到了 `_private_host`，于是同一 WiFi 下任意设备
        （访客手机、被入侵的 IoT）在自己私网 IP 上托管一个页面，一句 fetch 就能
        `GET /api/local_creds/qoder` 读走 dt-/drt- 明文，或 `POST /api/config`
        把 proxy 改成攻击者代理劫持后续所有 Authorization: Bearer。

        现在只认三类：
          ① 回环：`http://127.0.0.1:<port>` / `http://localhost:<port>`；
          ② App 自带页面：`https://appassets.androidplatform.net`；
          ③ **本机自己的局域网地址**（`_lan_ips()` 里的 IP）+ 本服务端口 ——
             用户直接在手机浏览器敲 `http://192.168.x.x:8787/` 打开控制台仍可用，
             而攻击者页面在**别的** IP 上，永远落不进这一类。

        另外 `server.allow_lan_console=true` 时可显式追加 `server.lan_origins`
        列出的完整 Origin（给「反代 / 域名访问」这类特殊部署留出口）。
        """
        if not origin:
            return False
        try:
            u = urlparse(origin)
        except Exception:
            return False
        if u.scheme not in ("http", "https"):
            return False
        host = (u.hostname or "").lower().strip(".")
        if not host:
            return False
        if host in ("127.0.0.1", "localhost", "::1") or host == "appassets.androidplatform.net":
            return True
        # 本机自己的局域网 IP：要求端口就是本服务端口，避免「同机器上另一个
        # 端口的页面」借同 IP 拿到可信身份。
        try:
            if host in _lan_ips() and u.port == self._server_port():
                return True
        except Exception:
            pass
        # 显式白名单（默认关闭）
        try:
            srv = load_config()["server"]
            if srv.get("allow_lan_console"):
                allowed = [str(x).strip().lower() for x in (srv.get("lan_origins") or [])]
                if origin.lower().rstrip("/") in allowed:
                    return True
        except Exception:
            pass
        return False

    def _server_port(self) -> int:
        try:
            return int(self.server.server_address[1])
        except Exception:
            return 0

    def _req_origin(self) -> str:
        """取本次请求的来源：优先 Origin，缺失时回退 Referer 的 scheme://host[:port]。"""
        o = (self.headers.get("Origin") or "").strip()
        if o:
            return o
        ref = (self.headers.get("Referer") or "").strip()
        if not ref:
            return ""
        try:
            u = urlparse(ref)
            if not u.scheme or not u.netloc:
                return ""
            return f"{u.scheme}://{u.netloc}"
        except Exception:
            return ""

    def _cors_headers(self):
        """只回显白名单 Origin。跨站请求拿不到 Allow-Origin，浏览器就不会把响应交给 JS。"""
        o = (self.headers.get("Origin") or "").strip()
        if o and self._origin_ok(o):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _host_ok(self) -> bool:
        """
        Host 头白名单（P1-1 DNS rebinding）。

        后端原来只按 `urlparse(self.path).path` 路由，从不看 Host。攻击者把
        evil.example 的 DNS 先指向自己的服务器（让浏览器加载恶意页面），TTL 归零
        后再解析到 127.0.0.1 —— 此时浏览器认为 `http://evil.example:8787` 与页面
        同源，同源策略与 CORS 全部失效，可直接读写所有接口。
        """
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            # HTTP/1.0 客户端可能不带 Host；这类请求只可能来自本机工具，仍校验源 IP
            client = (self.client_address[0] if self.client_address else "") or ""
            return client in _LOOPBACK_CLIENTS
        host = raw
        if host.startswith("["):  # [::1]:8787
            host = host.split("]")[0] + "]"
        elif ":" in host:
            host = host.rsplit(":", 1)[0]
        return self._private_host(host)

    def _security_headers(self):
        """M6：每个响应都带上安全头（见模块级 SECURITY_HEADERS 的说明）。"""
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)

    def _send_json(self, obj, code=200, *, head_only=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self._security_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not (self._head_only if head_only is None else head_only):
            self.wfile.write(data)

    def _send_file(self, path, ctype, *, head_only=None):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self._security_headers()
        self.send_header("Content-Length", str(len(data)))
        # 静态资源一律走缓存校验：每次回源确认，避免改了 app.js/css 前端还拿旧缓存
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not (self._head_only if head_only is None else head_only):
            self.wfile.write(data)

    def _is_loopback(self) -> bool:
        client = (self.client_address[0] if self.client_address else "") or ""
        return client in _LOOPBACK_CLIENTS

    def _redact_logs(self, logs):
        """
        把 `detail` 裁掉（BUGS-v2.25 的 P2-2，BUGS-v2.61 的 L11）。

        `detail` 里是账号指纹（`{"source":"vault:cn","account":"6f4620daaef19822"}`）。
        原来只在「非本机来源」时裁，本机保留原样 —— 但本机来源不等于可信：
        任意浏览器页面（哪怕不是控制台）发起的请求源 IP 就是 127.0.0.1。
        审计里 L11 的建议就是去掉这个短路，本机也裁。
        """
        out = []
        for row in (logs or []):
            if isinstance(row, dict):
                r = dict(row)
                if r.get("detail"):
                    r["detail"] = ""
                out.append(r)
            else:
                out.append(row)
        return out

    def _check_auth(self) -> bool:
        """
        口令校验（BUGS-v2.61 的 H4 / M7）。

        ## H4：空口令不再是「只读全放行」

        原来没设口令时，`GET/HEAD/OPTIONS` 一律 `return True` —— 默认监听
        0.0.0.0 + 默认空口令，等于同网段任何人 `curl http://<ip>:8787/api/status`
        就能拿到全部账号状态、`/api/logs` 拿到签到明细、`/api/config` 拿到配置
        （含掩码凭据与代理地址），而且 `/api/wb_assets?refresh=1` 会**真的带着
        用户的 accessToken 打上游**，`/api/growth` 会真的去领成长值。

        现在空口令时：
          - 只读接口仅对**本机来源**放行（手机 App 想连就必须先设口令 ——
            启动横幅与设置页都写了这一点，这是有意的取舍：局域网裸奔的代价
            比「多填一个口令」大得多）；
          - 写操作仍然要「本机来源 + 本机控制台 Origin」。

        ## M7：恒定时间比较 + 失败限速 + 不再支持 ?token=

        - `hmac.compare_digest` 替代 `==`：`==` 在首个不同字节就返回，攻击者能按
          响应时间一位一位试出口令。
        - 失败计数按来源 IP 记，5 次/分钟以上指数退避，超限直接 429，
          并且不再往下走业务逻辑 —— 原来可以无限次尝试。
        - 去掉 `?token=` 回退：URL 会进浏览器历史、Referer、代理日志、Shell
          history，一个能被日志记下来的口令等于泄露。只认 `X-API-Token` 头。
        """
        cfg = load_config()
        token = (cfg["server"].get("api_token") or "").strip()
        client = (self.client_address[0] if self.client_address else "") or ""

        if not token:
            # 空口令：只读接口仅本机可用；写操作要本机 + 本机控制台来源
            if self.command in ("GET", "HEAD", "OPTIONS"):
                return self._is_loopback()
            return self._is_loopback() and self._csrf_ok()

        if _throttled(client):
            return False

        given = self.headers.get("X-API-Token", "") or ""
        ok = bool(given) and hmac.compare_digest(given, token)
        _note_attempt(client, ok)
        return ok

    def _csrf_ok(self) -> bool:
        """
        写操作的来源校验（P0-2 跨站请求伪造）。

        浏览器跨站 POST 一定会带 Origin（旧浏览器带 Referer）。这里只接受
        「本机控制台」的来源；两个头都没有的请求（curl / 手机原生 / 电脑端脚本）
        放行 —— 它们不受浏览器同源策略影响，本来就不构成 CSRF 面。
        """
        o = self._req_origin()
        if not o:
            return True
        return self._origin_ok(o)

    # 请求体上限（P2-1）：原来 Content-Length 多大就读多大，
    # 一个 `Content-Length: 999999999` 就能让服务端尝试分配近 1GB。
    MAX_BODY = 1_048_576
    _body_too_large = False

    def _read_json(self):
        self._body_too_large = False
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        if length > self.MAX_BODY:
            # 不读、不分配。标记出来让调用方回 413，而不是假装「body 是空的」。
            self._body_too_large = True
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    # ---------- routing ----------
    # L4（BUGS-v2.61）：原来没有 do_HEAD，BaseHTTPRequestHandler 对未实现的
    # 方法直接回 501 —— 而 HEAD 是监控探活 / 缓存校验的常用方法，501 会让
    # 外部工具把它当成「服务不可用」。这里复用 GET 的路由，只丢掉 body。
    _head_only = False

    def do_HEAD(self):
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_OPTIONS(self):
        # 预检不再无条件放行：只有白名单 Origin 才会拿到 Allow-* 头。
        # 204 不能带 Content-Length（HTTP/1.1 下带 body 长度会让 keep-alive 错位），
        # 所以这里手写响应头，不走 _send_json。
        self.send_response(204)
        self._cors_headers()
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return self._send_json({"error": "bad host"}, 403)
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        # 静态资源
        if path == "/" or path == "/index.html":
            return self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self._send_file(os.path.join(WEB_DIR, "manifest.webmanifest"),
                                   "application/manifest+json; charset=utf-8")
        if path == "/sw.js":
            return self._send_file(os.path.join(WEB_DIR, "sw.js"),
                                   "application/javascript; charset=utf-8")
        if path == "/favicon.ico":
            return self._send_file(os.path.join(WEB_DIR, "static", "icon-192.png"), "image/png")
        if path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
            if ".." in rel or rel.startswith(("/", "\\")):
                return self.send_error(400)
            static_root = os.path.realpath(os.path.join(WEB_DIR, "static"))
            full = os.path.realpath(os.path.join(static_root, rel))
            # 二次校验：Windows 下 os.path.join 遇到绝对路径/盘符会丢弃前缀，
            # 只有 realpath 归一化后仍在 static 目录内才允许读取
            if full != static_root and not full.startswith(static_root + os.sep):
                return self.send_error(400)
            ctype = (
                "text/css; charset=utf-8" if rel.endswith(".css") else
                "application/javascript; charset=utf-8" if rel.endswith(".js") else
                "image/png" if rel.endswith(".png") else
                "image/svg+xml" if rel.endswith(".svg") else
                "image/x-icon" if rel.endswith(".ico") else
                "image/webp" if rel.endswith(".webp") else
                "application/octet-stream"
            )
            return self._send_file(full, ctype)

        # 空口令 + 非本机：直接说清楚为什么被拒，别让用户对着 401 猜
        if not self._check_auth():
            cfg_srv = load_config()["server"]
            if not (cfg_srv.get("api_token") or "").strip():
                # 没配口令：只可能是「非本机在读」或「写操作来源不合法」
                if self._is_loopback():
                    return self._send_json({"error": "forbidden origin"}, 403)
                return self._send_json(
                    {"error": "unauthorized",
                     "hint": "后端未设置访问口令，只读接口仅本机可用。"
                             "请在电脑端「设置 - 访问口令」里设一个口令，手机端填同一个。"}, 401)
            client = (self.client_address[0] if self.client_address else "") or ""
            if _throttled(client):
                return self._send_json(
                    {"error": "too many failed attempts",
                     "hint": "口令错误次数过多，请等一会儿再试。"}, 429)
            return self._send_json({"error": "unauthorized"}, 401)

        cfg = load_config()
        if path == "/api/ping":
            # version 保持旧值只为兼容老前端（app.js 用 appVersion || version 取值），
            # 真正要展示的版本号在 appVersion 里；两个字段现在都不再是硬编码的 "1.0"。
            return self._send_json({"ok": True, "version": APP_VERSION,
                                    "appVersion": APP_VERSION, "local": False})

        if path == "/api/status":
            off = cfg["scheduler"].get("timezone_offset_hours", 8)
            day = db.today_str(off)
            summary = db.day_summary(day)
            providers = build_providers(cfg)
            # light=1：跳过 qoder / minimax 的上游体检（各 ~250ms），只按本地凭据判就绪。
            # 下拉刷新走这条，避免为了刷个余额等 0.5 秒的「就绪」判定。
            light = (q.get("light") or [""])[0] in ("1", "true")
            rows = []
            for key, p in providers.items():
                # 带 retired 标记的 provider 不再出现在 /api/status 里，
                # 前端列表与「一键全部签到」都会自然跳过它。
                if (cfg["providers"].get(key) or {}).get("retired"):
                    continue
                rec = summary.get(key)
                rd = readiness(key, cfg["providers"][key], str(DATA_DIR), light=light)
                # P1-2（BUGS-v2.40）：代理填了却不可达时，签到只会报「网络错误」，
                # 用户想不到是代理。这里把原因并进 issues 让配置卡直接显示出来。
                # /api/status 用非阻塞版：状态接口不能被 TCP 超时拖慢（实测首次
                # 阻塞探测会把 /api/status 从 5ms 拖到 4300ms）。
                pr = _proxy_check_cached(key, cfg["providers"][key], probe=False)
                row_issues = list(rd["issues"])
                if pr:
                    row_issues.append(pr)
                msg = rec["message"] if rec else "今日尚未执行"
                # 未配置时不要把历史失败原因当成当前状态展示
                if rec and rec["status"] == "fail" and not rd["ready"]:
                    msg = "未配置：请到「设置 → 签到配置」填写后重试"
                row = {
                    "key": key, "label": p.label,
                    "enabled": cfg["providers"][key].get("enabled", True),
                    "mode": cfg["providers"][key].get("mode", "http"),
                    "dry_run": p.is_dry_run(),
                    "ready": rd["ready"],
                    "issues": row_issues,
                    "status": rec["status"] if rec else "idle",
                    "message": msg,
                    "points": rec["points"] if rec else "",
                    "total_points": (rec.get("total_points", -1) if rec else -1),
                    "used_points": (rec.get("used_points", -1) if rec else -1),
                    # 多账号明细：首页同一服务有多个账号时堆叠成多张卡片（用户 m08264）。
                    # 电脑端后端只跑单账号，所以这里永远只有一条（account_key=default）；
                    # 手机端由 LocalApi.status() 按 HistoryStore 的 account_key 出多条。
                    "accounts": ([{
                        "account_key": "default",
                        # 与手机端一致：头卡也标「账号 1」，多账号只靠编号区分（用户 m08666）
                        "label": "账号 1",
                        "status": rec["status"],
                        "message": rec["message"],
                        "points": rec["points"],
                        "total_points": rec.get("total_points", -1),
                        "used_points": rec.get("used_points", -1),
                    }] if rec else []),
                    "streak_days": (rec.get("streak_days", -1) if rec else -1),
                    "time": rec["created_at"] if rec else "",
                }
                # 积分接口与签到同域（/chat/snap-manager/v1/statistics/plugin），
                # 用同一份 cookie —— 会话可用即余额可读，不再需要额外的门户 cookie。
                # 字段名保留 portal_ok 是为了不动前端判据。
                #
                # **电脑端只判会话名，与手机端 hwUsable 不是同一判据**（BUGS-v2.48 P3-5）：
                # 手机端 = has_session && HwVerified.get() != false（hwProbe 的持久化结论），
                # 电脑端没有 HwVerified 这套东西（Python 侧只有 today_done 标记）。
                # 华为对未登录访客也发匿名 J_SESSION_ID，所以同一份匿名 Cookie 会在这里
                # 得到 portal_ok=True、在手机端得到 False。别把两者当成同一个判据。
                if key == "huawei":
                    from . import huawei as _hwm
                    _ck = ((cfg["providers"][key].get("huawei") or {}).get("cookie") or "").strip()
                    row["portal_ok"] = _hwm.has_session(_ck)
                rows.append(row)
            week = db.recent_days(7, off)
            # 顶部「连续 N 天」优先用上游账号的真实连登天数，没有才回落到本地记录统计
            real_streak = max([r["streak_days"] for r in rows if r.get("streak_days", -1) > 0] or [0])
            crypto_ok, creds_lost = _crypto_brief()
            return self._send_json({
                "day": day, "providers": rows,
                "scheduler": cfg["scheduler"],
                "stats": db.stats(),
                "week": week,
                "streak": real_streak or db.compute_streak(week),
                "assets": _assets_for_card(cfg),
                "notify": _notify_brief(cfg),
                # P3（BUGS-v2.40）：电脑端也透出凭据加密状态，前端横幅据此显示
                "crypto_ok": crypto_ok,
                "creds_lost": creds_lost,
            })

        if path == "/api/logs":
            key = (q.get("provider") or [""])[0]
            # limit 直接 int() 时，?limit=abc 会抛 ValueError 打崩整个请求（连接直接断）
            try:
                limit = int((q.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            limit = max(1, min(limit, 500))
            if key:
                logs = db.provider_history(key, limit)
            else:
                logs = db.recent_logs(limit)
            return self._send_json({"logs": self._redact_logs(logs)})

        if path == "/api/config":
            return self._send_json(_masked_config(cfg))

        if path == "/api/doctor":
            # N1（BUGS-v2.61）：这个接口会对配置里的每个 proxy 做 3 秒 TCP 探测 ——
            # 未授权可触发时就是一个现成的内网端口扫描器。除了 _check_auth 收紧，
            # 这里再加一层：结果缓存 60 秒 + 每 IP 每分钟次数上限。
            ip = (self.client_address[0] if self.client_address else "") or ""
            if not _upstream_budget(ip):
                return self._send_json(
                    {"error": "请求过于频繁，请稍后再试（每分钟上限 %d 次）"
                              % _UPSTREAM_HITS_PER_MIN}, 429)

            def _doctor():
                providers = build_providers(cfg)
                out = []
                for key, p in providers.items():
                    if (cfg["providers"].get(key) or {}).get("retired"):
                        continue  # 已下线的 provider 不参与体检
                    rd = readiness(key, cfg["providers"][key], str(DATA_DIR))
                    # P1-2 加固（BUGS-v2.40）：代理填了但不可达时，签到只会报
                    # 「网络错误: [WinError 10061]」，用户根本想不到是代理的问题。
                    # 这里对每个非空 proxy 做一次 3 秒 TCP 探测，把原因说清楚。
                    pr = _proxy_check_cached(key, cfg["providers"][key])
                    issues = list(rd["issues"])
                    if pr:
                        issues.append(pr)
                    out.append({
                        "key": key, "label": p.label,
                        "enabled": cfg["providers"][key].get("enabled", True),
                        # ready 只表示「凭据齐不齐」——代理不通是连接问题，不是凭据问题，
                        # 混进 ready 会让卡片在代理抖动时反复变灰。代理问题放 issues + proxy_ok。
                        "mode": rd["mode"], "ready": rd["ready"], "issues": issues,
                        "proxy": _proxy_of(key, cfg["providers"][key]),
                        "proxy_ok": (pr == ""),
                    })
                return {"providers": out}

            return self._send_json(_cached_upstream("doctor", _doctor))

        if path == "/api/notify_logs":
            try:
                limit = int((q.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(limit, 200))
            return self._send_json({"logs": db.recent_notify_logs(limit)})

        if path == "/api/trae_probe":
            from . import trae as tj
            _cfg = load_config()
            _tr = (_cfg.get("providers", {}).get("traesolo", {}) or {}).get("trae") or {}
            return self._send_json(tj.probe(_tr))

        # 读取本机客户端登录态（只读，供前端「读取本机凭据」按钮回填）
        lm = re.match(r"^/api/local_creds/([a-z0-9_]+)$", path)
        if lm:
            # 这个接口会把本机 Qoder / MiniMax 客户端的明文 accessToken 回传给调用方。
            # 它是「本机控制台」功能：默认监听 0.0.0.0 且 api_token 常为空，
            # 不加限制时同网段任何人 GET 一下就能拿到你的上游凭据。
            #
            # BUGS-v2.25 的 P0-1：只判「源 IP 是不是 127.0.0.1」是不够的 —— 浏览器在
            # 本机发起的请求源 IP 天然就是 127.0.0.1，加上原来响应带
            # `Access-Control-Allow-Origin: *`，任意恶意网页的 JS 一句 fetch 就能读走
            # refreshToken。现在必须同时满足：
            #   ① 源 IP 是本机；② 请求来源（Origin/Referer）是本机控制台；
            #   ③ 若设了口令，口令必须正确（_check_auth 已在上游判过）。
            # 不强制要求口令：本机用户在没设口令时也要能点「读取本机凭据」，
            # 而 ② 已经把「恶意网页」这条路堵死（跨站 fetch 一定带 Origin）。
            if not self._is_loopback():
                return self._send_json(
                    {"ok": False,
                     "error": "读取本机凭据仅限本机访问：请在这台电脑上打开 http://127.0.0.1:%d/"
                              % self.server.server_address[1]}, 403)
            if not self._csrf_ok():
                return self._send_json({"ok": False, "error": "forbidden origin"}, 403)
            lkey = lm.group(1)
            try:
                if lkey == "qoder":
                    from . import qoder as m
                    d = m.read_local_credentials("cn")
                elif lkey == "minimax":
                    from . import minimax as m
                    d = m.read_local_credentials()
                else:
                    return self._send_json({"ok": False, "error": f"unknown key {lkey}"}, 404)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 200)
            if d.get("error"):
                return self._send_json({"ok": False, "error": d["error"]}, 200)
            return self._send_json({"ok": True, "creds": d})

        # Qoder / MiniMax 体检（只读，不签到）
        pm = re.match(r"^/api/probe/([a-z0-9_]+)$", path)
        if pm:
            key = pm.group(1)
            pcfg = (cfg.get("providers", {}) or {}).get(key) or {}
            try:
                if key == "qoder":
                    from . import qoder as m
                    return self._send_json(m.probe(pcfg.get("qoder") or {}))
                if key == "minimax":
                    from . import minimax as m
                    return self._send_json(m.probe(pcfg.get("minimax") or {}))
                if key == "huawei":
                    from . import huawei as m
                    return self._send_json(m.probe(pcfg.get("huawei") or {}))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 200)
            return self._send_json({"error": f"unknown probe {key}"}, 404)

        if path == "/api/stats":
            # 按服务统计（全量日志），用于记录页增强
            with db._conn() as c:
                rows = c.execute(
                    "SELECT provider, status, COUNT(*) n FROM sign_logs GROUP BY provider, status"
                ).fetchall()
                days = c.execute(
                    "SELECT day, provider, status FROM sign_logs ORDER BY day DESC LIMIT 2000"
                ).fetchall()
            per = {}
            for r in rows:
                per.setdefault(r["provider"], {}).setdefault(r["status"], r["n"])
            # 本月完成天数（每个服务当天有 success/already 记 1 天）
            month = db.today_str(cfg["scheduler"].get("timezone_offset_hours", 8))[:7]
            month_days = {}
            seen = set()
            for d in days:
                if not str(d["day"]).startswith(month):
                    continue
                if d["status"] in ("success", "already"):
                    k = (d["provider"], d["day"])
                    if k not in seen:
                        seen.add(k)
                        month_days[d["provider"]] = month_days.get(d["provider"], 0) + 1
            return self._send_json({"by_provider": per, "month_done_days": month_days,
                                    "month": month, "totals": db.stats()})

        # ── 新增：积分资产 / 成长中心 ──────────────────────────────────
        # M8（BUGS-v2.61）：以下四个 GET 都会带用户的 accessToken 真打上游，
        # 必须限频 + 缓存，不能当成普通只读接口。
        if path == "/api/wb_assets":
            from . import wb_assets
            ip = (self.client_address[0] if self.client_address else "") or ""
            force = (q.get("refresh") or [""])[0] in ("1", "true")
            wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            if not force:
                return self._send_json(wb_assets.card(wb_cfg, use_cache=True))
            if not _upstream_budget(ip):
                return self._send_json(
                    {"ok": False, "error": "刷新过于频繁，请稍后再试（每分钟上限 %d 次）"
                                            % _UPSTREAM_HITS_PER_MIN}, 429)
            return self._send_json(_cached_upstream(
                "wb_assets", lambda: wb_assets.card(wb_cfg, use_cache=False)))

        if path == "/api/wb_accounts":
            # 成长中心的「账号切换」：电脑端只有一份 wb 凭据（config.json 里那一个），
            # 没有多账号列表，所以恒返 1 条。前端拿到 count==1 会提示「再登录一个就能切换」。
            wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            tok = str(wb_cfg.get("accessToken") or "").strip()
            if not tok:
                return self._send_json({"ok": True, "count": 0, "current": "", "list": []})
            uid = str(wb_cfg.get("uid") or "").strip()
            return self._send_json({"ok": True, "count": 1, "current": "default",
                                    "list": [{"id": "default", "name": "主账号",
                                              "uid": uid, "current": True}]})

        if path == "/api/growth":
            from . import wb_growth
            ip = (self.client_address[0] if self.client_address else "") or ""
            wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            if not _upstream_budget(ip):
                return self._send_json(
                    {"ok": False, "error": "请求过于频繁，请稍后再试（每分钟上限 %d 次）"
                                            % _UPSTREAM_HITS_PER_MIN}, 429)
            return self._send_json(_cached_upstream(
                "growth", lambda: wb_growth.overview(wb_cfg)))

        if path == "/api/growth/daily":
            from . import wb_growth
            from . import workbuddy as wbm
            ip = (self.client_address[0] if self.client_address else "") or ""
            wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            if not _upstream_budget(ip):
                return self._send_json(
                    {"ok": False, "error": "请求过于频繁，请稍后再试（每分钟上限 %d 次）"
                                            % _UPSTREAM_HITS_PER_MIN,
                     "rows": []}, 429)
            cred, _problems = wbm.resolve_credential(wb_cfg)
            if cred is None:
                return self._send_json({"ok": False, "error": "未配置 WorkBuddy 凭据", "rows": []})

            def _daily():
                try:
                    return {"ok": True, "detail": wb_growth.get_streak(cred)}
                except Exception as e:
                    return {"ok": False, "error": str(e), "rows": []}

            return self._send_json(_cached_upstream("growth_daily", _daily))

        if path == "/api/auto_status":
            # 这个接口只在安卓端由 LocalApi 实现（读 WorkManager 排期 + 上次运行记录）。
            # 电脑端跑的是 signbot.scheduler 线程，没有 WorkManager，这里返回同样结构，
            # 免得前端请求落到 404、把「读取失败」写进诊断卡。
            sc = cfg["scheduler"]
            last = db.recent_notify_logs(1)
            ran = bool(last)
            # 前端用 new Date(last_at) 渲染，这里把 ISO 时间换成毫秒时间戳
            last_at = None
            if ran:
                try:
                    last_at = int(datetime.fromisoformat(last[0]["created_at"]).timestamp() * 1000)
                except Exception:
                    last_at = None
            # 五家的就绪明细（用户 m21921）：电脑端也要给同一份结构，
            # 否则前端在电脑上打开诊断卡只能看到 WorkBuddy / Trae 两行。
            provs = []
            for _k, _lab in (
                ("workbuddy", "WorkBuddy"), ("traesolo", "Trae Solo"),
                ("qoder", "Qoder CN"), ("minimax", "MiniMax Code"), ("huawei", "华为码道"),
            ):
                _p = cfg["providers"].get(_k, {})
                _r = readiness(_k, _p, str(DATA_DIR))
                provs.append({
                    "key": _k, "label": _lab,
                    "ready": bool(_r.get("ready")),
                    "detail": ("就绪" if _r.get("ready") else "未就绪（会跳过）")
                              + ("" if not _r.get("issues") else "：" + str(_r["issues"][0])[:60]),
                })
            return self._send_json({
                "ok": True,
                "enabled": bool(sc.get("enabled")),
                "time": (sc.get("times") or ["08:00"])[0],
                "jitter_min": sc.get("jitter_min", 0),
                "ran": ran,
                "last_at": last_at,
                "last_title": (last[0].get("title") if ran else "") or "",
                "last_body": (last[0].get("body") if ran else "") or "",
                "queued": False,
                "queued_state": "电脑端常驻线程",
                "queued_attempts": 0,
                "wb_ready": readiness("workbuddy", cfg["providers"].get("workbuddy", {}), str(DATA_DIR)).get("ready", False),
                "trae_ready": readiness("traesolo", cfg["providers"].get("traesolo", {}), str(DATA_DIR)).get("ready", False),
                "trae_has_session": False,
                "trae_has_token": False,
                "trae_device_id": "",
                "providers": provs,
                "platform": "desktop",
            })

        if path == "/api/heat":
            from . import wb_growth
            from . import workbuddy as wbm
            ip = (self.client_address[0] if self.client_address else "") or ""
            wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            if not _upstream_budget(ip):
                return self._send_json(
                    {"ok": False, "error": "请求过于频繁，请稍后再试（每分钟上限 %d 次）"
                                            % _UPSTREAM_HITS_PER_MIN}, 429)
            cred, _problems = wbm.resolve_credential(wb_cfg)
            if cred is None:
                return self._send_json({"ok": False, "error": "未配置 WorkBuddy 凭据"})

            def _heat():
                try:
                    d = wb_growth.get_heatmap(cred)
                    cells = []
                    for c in (d.get("cells") or []):
                        if isinstance(c, dict) and c.get("date"):
                            cells.append({"date": c.get("date"), "score": int(c.get("score") or 0)})
                    return {"ok": True, "cells": cells, "today": d.get("today") or {}}
                except Exception as e:
                    return {"ok": False, "error": str(e)}

            return self._send_json(_cached_upstream("heat", _heat))

        if path == "/api/refresh_totals":
            # P1-2（BUGS-v1.63）：这个接口会并发打四家上游并写库，属于副作用操作。
            # 以前挂在 do_GET，浏览器预取 / SW 重放 / 地址栏直接敲一下都会真跑一轮。
            # 现在只允许 POST，GET 明确回 405，让前端一眼看出是方法错了。
            return self._send_json({"error": "method not allowed", "expect": "POST"}, 405)

        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._send_json({"error": "bad host"}, 403)
        parsed = urlparse(self.path)
        path = parsed.path
        # 写操作三道闸：口令 → 来源（Origin/Referer）→ 具体业务。
        # 原来只有口令这一道，空口令时退化成「源 IP 是本机」，而浏览器本机请求的
        # 源 IP 就是 127.0.0.1 —— 任意网页都能 POST /api/config 把 proxy 改成
        # 攻击者的代理，之后签到请求（含 Authorization: Bearer）全部过中间人。
        if not self._check_auth():
            # 区分「来源不对」与「没权限」：排查时一眼能看出是哪道闸拦的
            if self._is_loopback() and not self._csrf_ok():
                return self._send_json({"error": "forbidden origin"}, 403)
            return self._send_json({"error": "unauthorized"}, 401)
        if not self._csrf_ok():
            return self._send_json({"error": "forbidden origin"}, 403)

        if path == "/api/refresh_totals":
            # 强制重查两个平台的「当前持有积分」。Trae 走带 session 续期的入口，
            # 否则 8 小时 JWT 一过期就永远查不到（前端会一直显示「积分稍后刷新」）。
            from . import workbuddy as wbm
            from . import trae as trm
            from concurrent.futures import ThreadPoolExecutor
            totals = {}
            wb_cfg = (cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            trae_cfg = (cfg.get("providers", {}).get("traesolo", {}) or {}).get("trae") or {}

            def _wb_part():
                got = {}
                try:
                    cred, _p = wbm.resolve_credential(wb_cfg)
                    if cred is None:
                        return got
                    # 积分与签到状态是两个独立接口（实测 369ms / 279ms），并行取
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="wbpart") as p:
                        f_v = p.submit(wbm._held_credits, cred)
                        f_s = p.submit(wbm.fetch_status, cred)
                        try:
                            v = f_v.result()
                            if v is not None and v >= 0:
                                got["workbuddy"] = int(v)
                        except Exception:
                            pass
                        # 一并刷签到状态：只刷积分的话「今日已签到 / 连续 N 天」还是旧文案
                        try:
                            st = f_s.result()
                            stk = int(st.get("streak_days", -1) or -1)
                            if stk >= 0:
                                got["streak"] = stk
                        except Exception:
                            pass
                except Exception:
                    pass
                # 累计已用：WorkBuddy 从 assets 口径取（used_total 是逐包累加，与官网一致）
                try:
                    from . import wb_assets as wba
                    u = wba.card(wb_cfg or {}).get("used_total")
                    if isinstance(u, (int, float)) and u >= 0:
                        got["workbuddy_used"] = int(u)
                except Exception:
                    pass
                return got

            def _trae_part():
                got = {}
                try:
                    base = trm._base(trae_cfg)
                    tok = (trae_cfg.get("token") or "").strip()
                    dev = (trae_cfg.get("deviceId") or "").strip()
                    sess = (trae_cfg.get("session") or "").strip()
                    if not tok and sess:
                        tok = trm.exchange_session(sess, base) or ""
                        if tok:
                            trm._persist_token(tok)
                    if tok and dev:
                        # 一次请求同时拿到「剩余」与「累计已用」，不重复打上游
                        got_sums = trm.usage_sums(tok, dev, base)
                        if got_sums:
                            got["traesolo"] = int(got_sums[0])
                            got["traesolo_used"] = int(got_sums[1])
                except Exception:
                    pass
                return got

            def _qoder_part():
                got = {}
                try:
                    from . import qoder as qdm
                    qcfg = (cfg.get("providers", {}).get("qoder", {}) or {}).get("qoder") or {}
                    info, _e = qdm.fetch_usage(qcfg)
                    if info:
                        v = info.get("total")
                        if isinstance(v, int) and v >= 0:
                            got["qoder"] = int(v)
                        u = info.get("used")
                        if isinstance(u, int) and u >= 0:
                            got["qoder_used"] = int(u)
                except Exception:
                    pass
                return got

            def _minimax_part():
                got = {}
                try:
                    from . import minimax as mmm
                    mcfg = (cfg.get("providers", {}).get("minimax", {}) or {}).get("minimax") or {}
                    r = mmm._resolve(mcfg)
                    if not r.get("token"):
                        return got
                    info, _e = mmm.fetch_credits(r, mcfg)
                    if info:
                        v = info.get("total")
                        if isinstance(v, int) and v >= 0:
                            got["minimax"] = int(v)
                        u = info.get("consumed")
                        if isinstance(u, (int, float)) and u >= 0:
                            got["minimax_used"] = int(u)
                except Exception:
                    pass
                return got

            def _huawei_part():
                got = {}
                try:
                    from . import huawei as hwm
                    hcfg = (cfg.get("providers", {}).get("huawei", {}) or {}).get("huawei") or {}
                    v = hwm.held_credits(hcfg)
                    if isinstance(v, int) and v >= 0:
                        got["huawei"] = v
                except Exception:
                    pass
                return got

            # 五家互不依赖，全部并行：原来串行 ~1.2s（WB 0.65 + Trae 0.5），
            # 并行后 ≈ 最慢那家；Qoder/MiniMax 各 ~0.3s，合进去几乎不增加总时长。
            with ThreadPoolExecutor(max_workers=5, thread_name_prefix="reftot") as pool:
                f_wb = pool.submit(_wb_part)
                f_tr = pool.submit(_trae_part)
                f_qd = pool.submit(_qoder_part)
                f_mm = pool.submit(_minimax_part)
                f_hw = pool.submit(_huawei_part)
                for f in (f_wb, f_tr, f_qd, f_mm, f_hw):
                    try:
                        totals.update(f.result())
                    except Exception:
                        pass
            # 关键：新读数必须落库。首页卡片与 Token 健康的 total_points 读的是
            # 当天签到记录（见 /api/status 的 rows.append），只回传不写库的话
            # 页面纹丝不动 —— 这正是「点了刷新只有资产卡变」的原因。
            try:
                off = cfg["scheduler"].get("timezone_offset_hours", 8)
                day = db.today_str(off)
                tot_map = {k: v for k, v in totals.items() if isinstance(v, int)}
                stk = tot_map.pop("streak", None)
                # *_used 是同一次上游查询里顺带拿到的「累计已用」，单独落库
                used_map = {}
                for k in ("workbuddy", "traesolo", "qoder", "minimax", "huawei"):
                    u = tot_map.pop(k + "_used", None)
                    if isinstance(u, int) and u >= 0:
                        used_map[k] = u
                for k, v in tot_map.items():
                    db.update_totals(k, day, total=v, used=used_map.get(k, -1))
                for k, u in used_map.items():
                    if k not in tot_map:
                        db.update_totals(k, day, used=u)
                if stk is not None:
                    db.update_totals("workbuddy", day, streak=int(stk))
            except Exception:
                pass
            return self._send_json({"ok": True, "totals": totals})

        if path == "/api/checkin/all":
            res = runner.run_all(trigger="manual")
            return self._send_json(res)

        # 单服务「试签到」：真正执行但不写签到记录，用于配置验证
        tm = re.match(r"^/api/test/([a-z0-9_]+)$", path)
        if tm:
            key = tm.group(1)
            cfg = load_config()
            providers = build_providers(cfg)
            if key not in providers:
                return self._send_json({"error": f"unknown provider {key}"}, 404)
            if (cfg["providers"].get(key) or {}).get("retired"):
                return self._send_json({"ok": False, "retired": True,
                                        "message": "该服务已下线"})
            # 走 runner.run_test：它会给同一个 key 上锁，避免手动试签到撞上
            # 定时任务/一键签到时把同一家上游接口打两遍（风控源头）。
            try:
                res = runner.run_test(key)
            except KeyError:
                return self._send_json({"error": f"unknown provider {key}"}, 404)
            return self._send_json(res)

        m = re.match(r"^/api/checkin/([a-z0-9_]+)$", path)
        if m:
            key = m.group(1)
            # 已下线的服务不给签到（BUGS-v2.63-290 P3-3）：与 /api/test/<key> 同一口径，
            # 否则界面上看不到它、单卡却仍能打上游并写一条失败记录。
            if ((load_config().get("providers") or {}).get(key) or {}).get("retired"):
                return self._send_json({"ok": False, "retired": True,
                                        "message": "该服务已下线"})
            try:
                res = runner.run_one(key)
            except KeyError:
                return self._send_json({"error": f"unknown provider {key}"}, 404)
            return self._send_json(res)

        # ── 新增：成长中心写操作 ──────────────────────────────────────
        if path == "/api/growth/room":
            from . import wb_growth
            cfg2 = load_config()
            wb_cfg = (cfg2.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            res = wb_growth.run_tasks(wb_cfg)
            done = sum(1 for r in res.get("results", []) if r.get("ok"))
            return self._send_json({"ok": res.get("ok", False), "error": res.get("error", ""),
                                    "done": done, "total": len(res.get("results", [])),
                                    "results": res.get("results", [])})

        if path == "/api/growth/claim":
            from . import wb_growth
            cfg2 = load_config()
            wb_cfg = (cfg2.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            res = wb_growth.claim_all(wb_cfg)
            claimed = sum(1 for r in res.get("results", []) if r.get("ok"))
            return self._send_json({"ok": res.get("ok", False), "error": res.get("error", ""),
                                    "claimed": claimed, "total": len(res.get("results", [])),
                                    "results": res.get("results", [])})

        if path == "/api/growth/daily/run":
            from . import wb_growth
            cfg2 = load_config()
            wb_cfg = (cfg2.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {}
            res = wb_growth.run_daily(wb_cfg)
            done = sum(1 for r in res.get("results", []) if r.get("ok"))
            return self._send_json({"ok": res.get("ok", False), "error": res.get("error", ""),
                                    "done": done, "total": len(res.get("results", [])),
                                    "results": res.get("results", [])})

        if path == "/api/config":
            body = self._read_json()
            if self._body_too_large:
                return self._send_json({"error": "请求体过大（上限 1MB）"}, 413)
            if not isinstance(body, dict) or not body:
                return self._send_json({"error": "bad body"}, 400)
            # 深度合并（跳过被遮蔽的敏感值，避免用 "DOU***IE" 覆盖真实 Cookie）
            def merge(dst, src):
                for k, v in src.items():
                    if isinstance(v, str) and "***" in v:
                        continue  # 掩码值不回写
                    if isinstance(v, dict) and isinstance(dst.get(k), dict):
                        merge(dst[k], v)
                    else:
                        dst[k] = v
            # update_config 把「读 → 合并 → 写」整段持锁：token 续期线程也在
            # 改同一个文件，先读后写的裸流程会把对方刚写的字段整段覆盖回去。
            from .config import update_config
            update_config(lambda cur: merge(cur, body))
            return self._send_json({"ok": True})

        if path == "/api/reset_creds":
            # 用户 m21385：登录后采集的凭据没有办法清掉重新登录。
            #
            # 前端一直用「掩码占位」表达「已保存」（`dt-***lU`），而保存时跳过含 *** 的值 ——
            # 于是清空输入框再点保存等于「什么都不改」，凭据永远清不掉。
            # 这里给一个显式动作：只清凭据字段，保留 region / mode / proxy / host。
            body = self._read_json()
            if self._body_too_large:
                return self._send_json({"error": "请求体过大（上限 1MB）"}, 413)
            key = (body or {}).get("key") or ""
            spec = RESET_FIELDS.get(key)
            if not spec:
                return self._send_json({"error": "unknown provider: %s" % key}, 400)

            cleared = []

            def _reset(cur):
                p = (cur.get("providers") or {}).get(key)
                if not isinstance(p, dict):
                    return
                for blk, fields in spec.items():
                    sub = p.get(blk)
                    if not isinstance(sub, dict):
                        continue
                    for f in fields:
                        if sub.get(f):
                            cleared.append("%s.%s" % (blk, f))
                        sub[f] = ""

            from .config import update_config
            update_config(_reset)
            # 用户主动清空 → 登记到 vault，落盘守卫不再把旧密文还原回来
            try:
                from . import vault as _vault
                _paths = [(key, blk, f) for blk, fields in spec.items() for f in fields]
                _vault.mark_cleared(_paths)
            except Exception:
                pass
            # 今日「已签」本地标记也要清：换了账号还留着旧标记会直接短路成 already。
            if key == "huawei":
                try:
                    from . import huawei as hwm
                    hwm.clear_today_done()
                except Exception:
                    pass
            return self._send_json({"ok": True, "key": key, "cleared": cleared})

        if path == "/api/crypto_reset":
            # P3（BUGS-v2.40）：前端凭据失效横幅的「知道了」按钮会打这个接口，
            # 但后端原来没有路由 → 远程模式下 404 被 catch 吞掉，横幅永远消不掉。
            # Android 侧 LocalApi.kt 早已实现同名动作，这里对齐口径。
            try:
                from . import vault
                vault.reset_failures()
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 200)
            crypto_ok, creds_lost = _crypto_brief()
            return self._send_json({"ok": True, "crypto_ok": crypto_ok, "creds_lost": creds_lost})

        if path == "/api/notify_logs":
            return self._send_json({"error": "method not allowed"}, 405)

        if path == "/api/trae_login":
            # 打开可视浏览器扫码登录，成功后把 token 存进 profile。
            # 注意：这里以前无条件回 ok:true，而 _trae_login.py 根本不在仓库里，
            # Popen 抛的 FileNotFoundError 被 except: pass 吞掉 —— 前端于是显示
            # 「登录窗口已在电脑上打开」，用户对着桌面找一个永远不会出现的窗口。
            import threading

            script = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "_trae_login.py")
            if not os.path.exists(script):
                return self._send_json({
                    "ok": False,
                    "missing_script": True,
                    "message": "没有可视登录脚本（_trae_login.py 不存在）。"
                               "请改用：设置 → Trae 直连 → 粘贴 Cloud-IDE-Token，"
                               "或点「扫码/网页登录」用内置浏览器登一次。",
                })

            def _worker():
                try:
                    subprocess.Popen([sys.executable, script],
                                     cwd=os.path.dirname(script))
                except Exception as e:
                    print(f"[trae_login] 启动失败: {e}")

            threading.Thread(target=_worker, daemon=True).start()
            return self._send_json({"ok": True,
                                    "message": "已打开 Trae 登录页（手机号+验证码最快），登录后回来点「检查凭据」"})

        if path == "/api/trae_probe":
            from . import trae as tj
            cfg = load_config()
            return self._send_json(tj.probe((cfg.get("providers", {}).get("traesolo", {}) or {}).get("trae") or {}))

        if path == "/api/test_notify":
            cfg = load_config()
            res = notify.push_detail(cfg, "签到中心 测试通知",
                                     "如果你收到这条消息，说明通知配置成功。\n"
                                     "每天签到完成后会自动推送一次日报。")
            return self._send_json({"ok": res["ok"], "error": res["error"],
                                    "kind": res.get("kind", "")})

        return self._send_json({"error": "not found"}, 404)


def _lan_ips() -> set:
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


def _lan_ip() -> str:
    """启动横幅用的「首选局域网 IP」（保持单值，仅用于打印提示）。"""
    for ip in sorted(_lan_ips()):
        if not ip.startswith("127."):
            return ip
    return "127.0.0.1"


def _ensure_api_token() -> tuple:
    """
    空口令自动补一个随机口令（BUGS-v2.61 的 H4）。

    默认监听 0.0.0.0 + 默认空口令 = 同网段任何人只读全开（`/api/status` 能看到
    所有账号、`/api/wb_assets?refresh=1` 会真的带用户 accessToken 打上游）。
    但「默认只绑 127.0.0.1」会把主功能打死 —— 这个后端的核心用途就是给手机 App
    连。所以改成：首次启动自动生成 24 字节随机口令写进 config.json，并在启动
    横幅里打印一次，用户照抄进手机 App 即可。

    返回 `(token, generated)`。生成失败（磁盘不可写）时返回空串，此时只读接口
    退回「仅本机可用」，不会静默放开。
    """
    from .config import update_config
    cfg = load_config()
    token = (cfg["server"].get("api_token") or "").strip()
    if token:
        return token, False
    new = secrets.token_urlsafe(24)

    def _set(cur):
        cur.setdefault("server", {})["api_token"] = new

    try:
        update_config(_set)
    except Exception:
        return "", False
    return new, True


def serve(host: str = None, port: int = None):
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    token, token_generated = _ensure_api_token()
    cfg = load_config()
    host = host or cfg["server"].get("host", "0.0.0.0")
    port = port or cfg["server"].get("port", 8787)
    db.init_db()
    from .scheduler import Scheduler
    sched = Scheduler()
    sched.start()
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"[SigninHub] 端口 {port} 无法绑定：{e}")
        print("[SigninHub] 可能已有一个实例在运行，或端口被占用。修改 backend/data/config.json 里的 server.port 后重试。")
        raise SystemExit(1)
    lan = _lan_ip()
    print("=" * 50)
    print("  SigninHub 已启动")
    print(f"  本机控制台:   http://127.0.0.1:{port}/")
    print(f"  手机访问(局域网): http://{lan}:{port}/   <- 手机 App 填这个")
    print(f"  数据目录:     {DATA_DIR}")
    if token_generated:
        print("  " + "-" * 46)
        print("  已自动生成访问口令（首次启动，只打印这一次）：")
        print(f"      {token}")
        print("  手机端「设置 - 后端地址」里填上面这个口令。")
        print("  也可以在电脑端「设置 - 访问口令」里改成自己好记的。")
        print("  " + "-" * 46)
    elif not token:
        print("  " + "!" * 46)
        print("  [安全提示] 未设置 API 口令且无法自动生成（数据目录不可写）：")
        print("  只读接口已限制为仅本机可用，手机端将无法连接。")
        print("  请手动在 backend/data/config.json 的 server.api_token 里填一个口令。")
        print("  " + "!" * 46)
    print("  按 Ctrl+C 停止服务")
    print("=" * 50)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[SigninHub] 正在退出...")
        httpd.shutdown()


if __name__ == "__main__":
    serve()
