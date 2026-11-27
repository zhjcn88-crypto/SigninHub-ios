"""Trae Solo 每日签到（协议参考 star620/TraeTools）。

关键差异（相比之前用 x-cloudide-token 的写法）：
  1. 认证头是 Authorization: Cloud-IDE-JWT <token>，不是 x-cloudide-token
  2. 必须带 x-device-id（16 位数字 Aha 设备号），否则风控返回 9074「参与用户太多」
  3. 必须带 X-User-Region: cn
  4. token 来自 www.trae.cn 的 localStorage['Cloud-IDE-Token']
  5. 备用：X-Cloudide-Session cookie 可换新 token（/cloudide/api/v3/common/GetUserToken）

接口：
  POST /trae/api/v2/ug/checkin_credits/status   {}  -> {enable, checked_in, credits, extra_credits, code, message}
  POST /trae/api/v2/ug/checkin_credits/claim    {}  -> 同上
  POST /cloudide/api/v3/common/GetUserToken     Cookie: X-Cloudide-Session=<s> -> {Result:{Token}}
  POST /trae/api/v2/pay/user_current_entitlement_list  {require_usage,full_data} -> usage_summary 汇总
  POST /trae/api/v2/pay/ide_user_ent_usage      {require_usage} -> user_entitlement_pack_list 逐包

业务码（实测口径，参考 L0NE-6/Trae-AutoCheckin）：
  0       成功
  9004    本设备今日已签到（不是失败！）
  9095    同上，另一个变体
  9074    设备校验未通过。三个来源按可能性排序：
          ① 设备头不全 —— 桌面端 ob(e) 一次注入 5 个头（x-device-id + x-device-type
             + x-os-version + x-app-version + x-device-brand），只发一个会被判成
             非官方客户端。v2.7 已补齐。
          ② 多账号同时打 claim —— 上游按时间窗口排队，并发会互相挤进 9074。
             v2.7 已改成串行 + 12~25 秒错峰。
          ③ x-device-id 确实没在该账号下注册过 —— 需要该账号自己的真实机器号。
  20405   续期要设备证明（ECDSA）
  20101   refreshToken 已失效/被轮换
  10101   refreshToken 与客户端不匹配
  20403   token 与设备不匹配（token 不是这台机器签发的）
"""
import json
import os
import re
import urllib.error
import urllib.request

from . import netguard

BASE_CN = "https://api.trae.cn"
BASE_INTL = "https://api.trae.ai"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
TOKEN_PATH = "/cloudide/api/v3/common/GetUserToken"
USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"
ENTITLE_PATH = "/trae/api/v2/pay/user_current_entitlement_list"
TIMEOUT = 25
# 桌面客户端版本号，用于 x-app-version 头（本机 E:\Trae CN 的 main.js 里 IdeVersion）
IDE_VERSION = "1.107.1"
# 签到/领取请求体：桌面端就是 {"req_source":1}（Solo 版是 2）。
# 空请求体 {} 也会被拒成 9074 —— 同类实测项目明确记录过。
CHECKIN_BODY = b'{"req_source":1}'
# v2.18：本机客户端 package_type=SOLO，ii() 为真时官方发 req_source=2。
# 两种口径各试一次的成本只是一次 HTTP，比让用户反复点重试划算。
CHECKIN_BODY_SOLO = b'{"req_source":2}'

# v2.18：4 个设备头的取值来源（main.js 的 commonParams，h = getSystemInformation()）：
#   x-device-brand = device_model  → 真机 "82RF"（Lenovo 机型码，**不是产品名 "trae"**）
#   x-device-type  = os_name       → 真机 "windows"（全小写，**不是 "Windows"**）
#   x-os-version   = os_version    → 真机 "Windows 10 Pro for Workstations"
#                                    （"10.0.19045" 是 os_release，v2.7 选错字段）
#   x-app-version  = appVersion    → "1.107.1"（本来就对）
OS_NAME = "windows"
OS_VERSION = "Windows 10 Pro for Workstations"
DEVICE_MODEL = "82RF"

# 这两个码都表示「本设备今日已签到」，必须当 already 处理，否则每天误报失败
ALREADY_CODES = (9004, 9095)
# 设备号/凭据相关的硬失败，用来给用户人话提示
DEVICE_CODES = (9074,)
AUTH_HARD_CODES = (20405, 20101, 10101, 20403)
# 网络层异常的哨兵前缀：_post 捕获后返回 status=0 + 该前缀，避免异常抛穿调用栈
NETERR_PREFIX = "__NETERR__:"

PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SigninHub", "trae-profile")
DEVICE_RE = re.compile(r"iCubeAuthInfo://icube-dc:(\d{8,})")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}")


def _base(cfg):
    host = (cfg.get("host") or "").strip().rstrip("/")
    if host:
        return host
    region = (cfg.get("region") or "cn").strip().lower()
    return BASE_INTL if region == "global" else BASE_CN


def _post(url, headers, body=b"{}"):
    """返回 (status, text)。

    网络层异常（DNS 失败、超时、连接被重置）统一返回 status=0 且 text 以
    NETERR_PREFIX 开头，由调用方用 _is_net_error() 判定，避免异常抛穿 run()。

    BUGS-v2.63-288 P3：出站统一走 netguard.open_checked()。这里的 url 可能来自
    `cfg["host"]`（用户可填自定义 host），所以需要目标校验；以前直接 urlopen
    会默认跟随 302，绕过 netguard 的逐跳复检。
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        return netguard.open_checked(req, timeout=TIMEOUT, limit=20000)
    except netguard.BlockedTarget as e:
        # 目标被安全策略拒绝：当成明确的网络层失败上报，不要伪装成 HTTP 码。
        return 0, NETERR_PREFIX + str(e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, NETERR_PREFIX + str(e)


def _is_net_error(status, text) -> bool:
    return status == 0 and isinstance(text, str) and text.startswith(NETERR_PREFIX)


def discover_device_id():
    """从 Trae 客户端 storage.json 读 Aha 设备号（16 位数字）。"""
    appdata = os.environ.get("APPDATA", "")
    for name in ("TRAE SOLO CN", "TRAE CN", "TRAE", "Trae CN", "TRAE SOLO", "Trae"):
        p = os.path.join(appdata, name, "User", "globalStorage", "storage.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8-sig", errors="ignore") as f:
                m = DEVICE_RE.search(f.read())
            if m:
                return m.group(1), name
        except Exception:
            continue
    return None, None


def read_token_from_profile():
    """从我们自己的 Playwright profile 取 token（先看 token.txt，再扫 leveldb）。"""
    tf = os.path.join(PROFILE_DIR, "token.txt")
    if os.path.isfile(tf):
        try:
            with open(tf, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) > 40:
                return t
        except Exception:
            pass
    ld = os.path.join(PROFILE_DIR, "Default", "Local Storage", "leveldb")
    if not os.path.isdir(ld):
        return None
    try:
        files = sorted(
            (os.path.join(ld, f) for f in os.listdir(ld)),
            key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0,
            reverse=True,
        )[:8]
    except Exception:
        return None
    for p in files:
        try:
            with open(p, "rb") as f:
                b = f.read(8 * 1024 * 1024)
        except Exception:
            continue
        hits = JWT_RE.findall(b.decode("latin-1", "ignore"))
        if hits:
            hits.sort(key=len, reverse=True)
            return hits[0]
    return None


def _session_headers(token, device_id, region="cn"):
    # 余额查询（ide_user_ent_usage）走**网页口径**：浏览器 UA + Origin/Referer
    # 才拿得到完整的 user_entitlement_pack_list（TraeCheckin/1.0 只对签到接口够用）。
    # 这条路径本来就该像网页端，所以多带这几个头是合理的。
    #
    # 设备头取值与 _checkin_headers 完全一致（v2.18 修正，见常量处注释）。
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-User-Region": region,
        "x-device-id": device_id,
        "x-device-type": OS_NAME,
        "x-os-version": OS_VERSION,
        "x-app-version": IDE_VERSION,
        "x-device-brand": DEVICE_MODEL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://work.trae.cn",
        "Referer": "https://work.trae.cn/",
    }


def _checkin_headers(token, device_id, region="cn"):
    """签到/领取专用头。两次教训，别再来第三次：

    【v2.7 实测成功】12 个头（Content-Type / Accept / Authorization / X-User-Region /
      5 个设备头 / User-Agent / Origin / Referer）。用户 m12822 确认
      「trea 终于可以多账号登录了」—— 这是**唯一被实测确认能签成**的组合。

    【v2.18 实测失败】为了「只发官方 main.js 逆向出的 7 个头」把 Accept /
      X-User-Region / Origin / Referer / **User-Agent** 全删了。删 UA 是致命的：
      OkHttp / requests 会自动补一个默认 UA，比缺任何设备头都更刺眼的爬虫特征。
      结果 9074 照旧 —— 而官方错误码枚举（www.trae.cn 的 JS chunk 32486.*.js）里
      9074 就是 `ErrWorkRiskControlBanned`（风控封禁），完全对得上「被判成非官方客户端」。

    所以 v2.19 回到 v2.7 的 12 头，只保留 v2.18 里**唯一正确**的那部分改进：
    4 个设备头的取值（main.js 的 commonParams，h = getSystemInformation()）：
        x-device-brand = device_model   （真机 "82RF"，Lenovo 机型码；v2.7 错写 "trae"）
        x-device-type  = os_name        （真机 "windows"，小写；v2.7 错写 "Windows"）
        x-os-version   = os_version     （真机 "Windows 10 Pro for Workstations"；
                                        v2.7 错写 "10.0.19045" —— 那是 os_release）
        x-app-version  = appVersion     （"1.107.1"，本来就对）
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-User-Region": region,
        "x-device-id": device_id,
        "x-device-type": OS_NAME,
        "x-os-version": OS_VERSION,
        "x-app-version": IDE_VERSION,
        "x-device-brand": DEVICE_MODEL,
        "User-Agent": f"Trae/{IDE_VERSION}",
        "Origin": "https://work.trae.cn",
        "Referer": "https://work.trae.cn/",
    }


def _checkin_call(base, path, token, device_id):
    """打一次签到接口；若回 9074 再用 SOLO 口径（req_source=2）重发一次。

    依据：桌面端 `const u = ii(this.X) ? 2 : 1`，`ii()` 判 packageType 是否属于 SOLO 系列。
    本机客户端日志里 `package_type` 就是 `"SOLO"`，所以两种口径都要能过。
    v2.7 恒发 1 能过是因为服务端两种都收，但账号/设备形态不同时可能只接受其中一种。
    返回 (http_status, text)。
    """
    st, text = _post(base + path, _checkin_headers(token, device_id), CHECKIN_BODY)
    if _is_net_error(st, text):
        return st, text
    _, doc = _parse(st, text)
    if doc.get("code") not in DEVICE_CODES:
        return st, text
    st2, text2 = _post(base + path, _checkin_headers(token, device_id), CHECKIN_BODY_SOLO)
    if _is_net_error(st2, text2):
        return st, text
    _, doc2 = _parse(st2, text2)
    return (st, text) if doc2.get("code") in DEVICE_CODES else (st2, text2)


def _parse(status, text):
    try:
        doc = json.loads(text)
    except Exception:
        doc = {"code": -1, "message": text[:300]}
    if not isinstance(doc, dict):
        doc = {"code": -1, "message": str(doc)[:300]}
    return status, doc


def _auth_hint(code):
    """把 Trae 的几个续期/凭据硬失败码翻成人话（来源：L0NE-6/Trae-AutoCheckin FAQ）。"""
    return {
        20405: "（续期需要设备证明：Trae 2026-09 后给 ExchangeToken 加了 ECDSA 设备签名，"
               "缺设备密钥就是这个码。当前实现改用 X-Cloudide-Session 换 token 绕开它。）",
        20101: "（refreshToken 已失效或已被别处轮换掉，请重新登录一次）",
        10101: "（refreshToken 与当前客户端不匹配，请从签发它的同一台机器重新提取）",
        20403: "（token 与设备不匹配：这个 token 是在另一台设备上签发的）",
    }.get(code, "")


def usage_sums(token, device_id, base=BASE_CN):
    """返回 (remaining, used) 两个整数；查询失败返回 None。

    数据源：POST /trae/api/v2/pay/ide_user_ent_usage
            body {"require_usage": true, "req_source": 2}
    口径对齐桌面客户端 main.js 的 uJe()：
        remaining 逐包 = max(credits_limit - credits_amount, 0)，再求和；
        used      逐包 = credits_amount，再求和（这就是「累计已用」）。
    credits_limit <= 0 的是免费订阅包 / 无限量包，不计入。

    注意：`usage_summary.total_amount` 是「历史获得总量」不是余额，
    `total_amount - consumed_amount` 也不可靠（会随套餐变化跳变），
    唯一稳定的是逐包求和。
    """
    if not token or not device_id:
        return None
    try:
        headers = _session_headers(token, device_id)
        headers["Origin"] = "https://work.trae.cn"
        headers["Referer"] = "https://work.trae.cn/"
        status, text = _post(
            base + USAGE_PATH, headers,
            body=json.dumps({"require_usage": True, "req_source": 2}).encode(),
        )
        if status != 200:
            return None
        _, doc = _parse(status, text)
        if doc.get("code") not in (0, None):
            return None
        packs = doc.get("user_entitlement_pack_list") or []
        rem = 0.0
        used_sum = 0.0
        seen = 0
        for p in packs:
            if not isinstance(p, dict):
                continue
            b = p.get("entitlement_base_info") or {}
            q = b.get("quota") or {}
            try:
                limit = float(q.get("credits_limit") or 0)
            except (TypeError, ValueError):
                continue
            if limit <= 0:
                continue          # 免费包 / 无限量包不计入
            try:
                used = float((p.get("usage") or {}).get("credits_amount") or 0)
            except (TypeError, ValueError):
                used = 0.0
            rem += max(limit - used, 0.0)
            used_sum += used
            seen += 1
        if not seen:
            return None
        # 向下取整，与官网显示一致（不做四舍五入）
        return int(rem), int(used_sum)
    except Exception:
        return None


def held_credits(token, device_id, base=BASE_CN):
    """Trae 账号**当前持有**的 Credits。查询失败返回 -1，调用方保持原值，绝不写 0 误导。"""
    got = usage_sums(token, device_id, base)
    return got[0] if got else -1


def used_credits(token, device_id, base=BASE_CN):
    """Trae 账号**累计已用**的 Credits（逐包 credits_amount 求和）。失败返回 -1。"""
    got = usage_sums(token, device_id, base)
    return got[1] if got else -1


class NetError(RuntimeError):
    """网络层失败（断网/超时/连接被拒），由调用方转成 status=fail。"""


def exchange_session(session, base=BASE_CN):
    """用 X-Cloudide-Session 换新 token。"""
    headers = {
        "Cookie": f"X-Cloudide-Session={session}",
        "Referer": "https://www.trae.cn/",
        "Origin": "https://www.trae.cn",
        "Content-Type": "application/json",
    }
    status, text = _post(base + TOKEN_PATH, headers)
    if _is_net_error(status, text):
        raise NetError(text[len(NETERR_PREFIX):])
    _, doc = _parse(status, text)
    result = doc.get("Result") or doc.get("result") or {}
    tok = result.get("Token") or result.get("token")
    return tok if isinstance(tok, str) and len(tok) > 40 else None


def _persist_token(token):
    """把换到的新 token 写回 config.json，否则下一轮定时任务又拿旧的 8 小时 JWT 撞 401。"""
    if not token:
        return
    try:
        from .config import update_config

        def mut(c):
            tr = ((c.get("providers") or {}).get("traesolo") or {}).get("trae")
            if isinstance(tr, dict) and tr.get("token") != token:
                tr["token"] = token

        update_config(mut)
    except Exception:
        pass


def run(cfg=None):
    cfg = cfg or {}
    base = _base(cfg)
    token = (cfg.get("token") or "").strip()
    device_id = (cfg.get("deviceId") or "").strip()
    detail = {"host": base}

    if not device_id:
        found, src = discover_device_id()
        if found:
            device_id = found
            detail["device_from"] = src
    if not device_id:
        return {"status": "fail", "http_status": None,
                "message": "缺少 x-device-id：请在本机安装并登录 Trae 客户端（读 storage.json），或手动填写 16 位设备号。",
                "points": "", "detail": json.dumps(detail, ensure_ascii=False)}

    if not token:
        token = read_token_from_profile() or ""
        if token:
            detail["token_from"] = "playwright-profile"
    if not token:
        sess = (cfg.get("session") or "").strip()
        if sess:
            # 断网时 exchange_session 曾抛穿 run()，让整轮签到变成「执行异常」，这里收口成 fail
            try:
                token = exchange_session(sess, base) or ""
            except Exception as e:
                return {"status": "fail", "http_status": None,
                        "message": f"网络错误：换 token 失败（{e}）",
                        "points": "", "detail": json.dumps(detail, ensure_ascii=False)}
            if token:
                detail["token_from"] = "session-exchange"
                _persist_token(token)
    # 有 session 时，token 过期（401/403）会自动换新再重试一次
    session = (cfg.get("session") or "").strip()
    if not token:
        return {"status": "fail", "http_status": None,
                "message": "缺少 token：请在本机 Trae 客户端登录过，或用「扫码登录」按钮登录一次即可自动获取；"
                           "也可在配置里填 session（X-Cloudide-Session）用于自动续期。",
                "points": "", "detail": json.dumps(detail, ensure_ascii=False)}

    headers = _session_headers(token, device_id)
    try:
        st, text = _checkin_call(base, STATUS_PATH, token, device_id)
        if _is_net_error(st, text):
            return {"status": "fail", "http_status": None,
                    "message": f"网络错误：{text[len(NETERR_PREFIX):]}",
                    "points": "", "detail": json.dumps(detail, ensure_ascii=False)}
        status, doc = _parse(st, text)
        if status in (401, 403) and session:
            fresh = exchange_session(session, base)
            if not fresh:
                return {"status": "fail", "http_status": status,
                        "message": f"凭据被拒绝（HTTP {status}），session 也换不到新 token，请重新登录 Trae。",
                        "points": "", "detail": text[:400]}
            token = fresh
            detail["token_from"] = "session-exchange(retry)"
            _persist_token(token)
            headers = _session_headers(token, device_id)
            st, text = _checkin_call(base, STATUS_PATH, token, device_id)
            if _is_net_error(st, text):
                return {"status": "fail", "http_status": None,
                        "message": f"网络错误：{text[len(NETERR_PREFIX):]}",
                        "points": "", "detail": json.dumps(detail, ensure_ascii=False)}
            status, doc = _parse(st, text)
            if status in (401, 403):
                return {"status": "fail", "http_status": status,
                        "message": f"换新后仍被拒绝（HTTP {status}），session 可能已过期，请重新登录。",
                        "points": "", "detail": text[:400]}
        if status in (401, 403):
            return {"status": "fail", "http_status": status,
                    "message": f"凭据被拒绝（HTTP {status}），token 可能过期；"
                               f"在配置里填 session（X-Cloudide-Session）可自动续期。",
                    "points": "", "detail": text[:400]}
        code = doc.get("code")
        if code not in (0, None) and code not in ALREADY_CODES:
            msg = str(doc.get("message") or doc.get("msg") or "")[:160]
            if code in DEVICE_CODES:
                hint = ("（被 Trae 风控拦了 code=9074 = ErrWorkRiskControlBanned，"
                        "不是「设备没注册」—— 那是 9095）。上游文案「当前参与用户太多」是套话，"
                        "等几分钟没用。头号诱因是使用顺序：账号在官网刚注册、还没正常用过，"
                        "就先被本软件拿来签到。建议：① 先到 trae.cn 官网正常登录用一会儿"
                        "（写几行代码、开一次会话都算），隔几小时（最好隔一天）再回来签；"
                        "② 已经报 9074 的，等 12~24 小时再试；"
                        f"③ 期间不要改设备号（当前 {device_id}）、也不要反复点测试"
                        "（每次调用都会被记一次））")
            elif code in AUTH_HARD_CODES:
                hint = _auth_hint(code)
            else:
                hint = ""
            return {"status": "fail", "http_status": status,
                    "message": f"上游失败 code={code} {msg}{hint}",
                    "points": "", "detail": text[:400]}

        detail["status"] = {k: doc.get(k) for k in ("enable", "checked_in", "credits", "extra_credits")}
        if doc.get("checked_in") is True or code in ALREADY_CODES:
            # 当前持有积分走 ide_user_ent_usage 余额口径；签到响应里的 credits 只是当日奖励
            held = held_credits(token, device_id, base)
            detail["held"] = held
            return {"status": "already", "http_status": status,
                    "message": "今日已签到" + (f" · 当前积分 {held}" if held >= 0 else ""),
                    "points": "", "total_points": held,
                    "detail": json.dumps(detail, ensure_ascii=False)}
        if doc.get("enable") is False:
            return {"status": "fail", "http_status": status,
                    "message": "该账号签到活动未开启（enable=false）",
                    "points": "", "detail": json.dumps(detail, ensure_ascii=False)}

        st2, text2 = _checkin_call(base, CLAIM_PATH, token, device_id)
        if _is_net_error(st2, text2):
            return {"status": "fail", "http_status": None,
                    "message": f"网络错误：领取时 {text2[len(NETERR_PREFIX):]}",
                    "points": "", "detail": json.dumps(detail, ensure_ascii=False)}
        status2, doc2 = _parse(st2, text2)
        if status2 in (401, 403) and session:
            fresh = exchange_session(session, base)
            if fresh:
                token = fresh
                _persist_token(token)
                headers = _session_headers(token, device_id)
                st2, text2 = _checkin_call(base, CLAIM_PATH, token, device_id)
                if _is_net_error(st2, text2):
                    return {"status": "fail", "http_status": None,
                            "message": f"网络错误：领取重试时 {text2[len(NETERR_PREFIX):]}",
                            "points": "", "detail": json.dumps(detail, ensure_ascii=False)}
                status2, doc2 = _parse(st2, text2)
        if status2 in (401, 403):
            return {"status": "fail", "http_status": status2,
                    "message": f"领取时凭据被拒绝（HTTP {status2}）", "points": "",
                    "detail": text2[:400]}
        code2 = doc2.get("code")
        if code2 not in (0, None) and code2 not in ALREADY_CODES and doc2.get("checked_in") is not True:
            if code2 in DEVICE_CODES:
                hint = ("（被 Trae 风控拦了 code=9074 = ErrWorkRiskControlBanned，"
                        "不是「设备没注册」—— 那是 9095）。头号诱因是使用顺序：账号在官网"
                        "刚注册、还没正常用过就先来签到。建议：① 先到 trae.cn 官网正常用一会儿，"
                        "隔几小时（最好隔一天）再回来签；② 已经报 9074 的等 12~24 小时；"
                        f"③ 期间不要改设备号（当前 {device_id}）、也不要反复点测试）")
            elif code2 in AUTH_HARD_CODES:
                hint = _auth_hint(code2)
            else:
                hint = ""
            return {"status": "fail", "http_status": status2,
                    "message": f"领取失败 code={code2} {str(doc2.get('message') or '')[:160]}{hint}",
                    "points": "", "detail": text2[:400]}

        credits = doc2.get("credits")
        extra = doc2.get("extra_credits")
        detail["claim"] = {k: doc2.get(k) for k in ("checked_in", "credits", "extra_credits")}
        gained = ""
        if isinstance(extra, (int, float)) and extra > 0:
            gained = f"+{int(extra)}"
        elif isinstance(credits, (int, float)) and credits > 0:
            gained = f"+{int(credits)}"
        # 领完再查一次真实余额
        held = held_credits(token, device_id, base)
        detail["held"] = held
        return {"status": "success", "http_status": status2,
                "message": "签到成功" + (f" · {gained} 积分" if gained else "")
                           + (f" · 当前积分 {held}" if held >= 0 else ""),
                "points": gained, "total_points": held,
                "detail": json.dumps(detail, ensure_ascii=False)}
    except Exception as e:
        return {"status": "fail", "http_status": None, "message": f"网络错误：{e}",
                "points": "", "detail": json.dumps(detail, ensure_ascii=False)}


def probe(cfg=None):
    """只读体检：token 和 device-id 是否齐备。"""
    cfg = cfg or {}
    token = (cfg.get("token") or "").strip() or (read_token_from_profile() or "")
    device_id = (cfg.get("deviceId") or "").strip()
    src = None
    if not device_id:
        device_id, src = discover_device_id()
    return {
        "token_ok": bool(token),
        "token_len": len(token),
        "device_id": device_id,
        "device_from": src,
        "profile": PROFILE_DIR,
        "profile_exists": os.path.isdir(PROFILE_DIR),
    }