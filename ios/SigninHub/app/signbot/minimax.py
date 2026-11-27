"""MiniMax Code（MiniMax Agent 桌面版）每日签到 —— 纯 HTTP 直连。

协议（从客户端 app.asar 逆向 + 真实抓包核对，2026-11-20 实测可用）：
    GET  https://agent.minimax.cn/minimax-cloud/api/v1/signin/status
    GET  https://agent.minimax.cn/minimax-cloud/api/v1/signin/config
    POST https://agent.minimax.cn/minimax-cloud/api/v1/signin/claim   body {}

签名（缺一不可，否则 403）：
    x-timestamp = 秒级 unix
    x-signature = md5( str(second) + "I*7Cf%WZ#S&%1RlZJ&C2" + body )
                  body 为 GET 时 ""，POST 时为 json 串（如 "{}"）
    yy          = md5( encodeURIComponent(path + "?" + query) + "_" + (body or "{}")
                       + md5(str(now_ms)) + "ooui" )

公共查询参数（顺序无关，服务端不校验顺序）：
    device_platform=web&biz_id=3&app_id=3001&version_code=22201&unix=<ms>&timezone_offset=28800
    &is_desktop=1&desktop_version=3.1.16&sys_language=zh&lang=zh&uuid=<uuid4>&device_id=<id>
    &os_name=Windows&browser_name=Chrome&device_memory=16&cpu_core_num=20&browser_language=zh-CN
    &browser_platform=Win32&user_id=<realUserID>&screen_width=1920&screen_height=1080&client=desktop

响应：
    {"data":{...},"base_resp":{"status_code":0,"status_msg":"ok"}}   status_code != 0 即失败
    status → data.days[] 7 项，每项 {day_no, points, bonus_points, is_today, status}
             status: 1=Upcoming 2=Claimable 3=Claimed 4=Disabled
    claim  → data.{claim_id, claim_result(1=Claimed 2=AlreadyClaimed), day_no, points,
                  expire_at_ms, panel}

凭据（Windows，明文 JSON，无需解密）：
    %USERPROFILE%\\.minimax\\auth\\prod\\cn\\mcode-public\\auth.json
        {"accessToken":"mmoat_...","refreshToken":"mmort_...","tokenType":"Bearer", ...}
    %APPDATA%\\MiniMax\\minimax-agent-cn-config.json
        {"sharedUser":{"userID":"...","realUserID":"580084210824519685","deviceID":"..."}}
"""
import hashlib
import json
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib

TIMEOUT = 25

BASE = "https://agent.minimax.cn"
STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
CONFIG_PATH = "/minimax-cloud/api/v1/signin/config"
CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"
# 额度明细：/credit/details 返回
# {"details":[{"credit_type":2,"granted_amount":"400.00","remaining_amount":"388.50",
#              "consumed_amount":"11.50","granted_at_ms":...,"expire_at_ms":...}],
#  "total_count":1,"base_resp":{"status_code":0}}
# 注意：加 ?page=1&page_size=20 反而 400（errorCode 50001），必须不带参数。
CREDIT_PATH = "/minimax-cloud/api/v1/credit/details"

SIGNATURE_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"
YY_SUFFIX = "ooui"
DESKTOP_VERSION = "3.1.16"
APP_VERSION_CODE = "22201"
UA = ("MiniMaxAgent Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) MiniMax/3.1.16 Chrome/148.0.7778.280 Safari/537.36")

AUTH_JSON = os.path.join(
    os.path.expanduser("~"), ".minimax", "auth", "prod", "cn", "mcode-public", "auth.json")
AUTH_STATE = os.path.join(
    os.path.expanduser("~"), ".minimax", "auth", "prod", "cn", "mcode-public", "auth-state.json")
AGENT_CFG = os.path.join(
    os.environ.get("APPDATA", ""), "MiniMax", "minimax-agent-cn-config.json")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def read_local_credentials() -> dict:
    """读本机 MiniMax Code 的登录态（明文 JSON）。

    auth.json 结构是 records 表（key = "com.minimax.mcode.oauth.prod.cn\\u0000<账号哈希>"）：
        {"schemaVersion":1,"records":{"...":{"accessToken":"mmoat_...",
          "refreshToken":"mmort_...","expiresAtMs":...,"generation":1}}}
    多个账号时取 expiresAtMs 最大的那条（最近登录的）。
    """
    if not os.path.exists(AUTH_JSON):
        return {"error": f"未找到 MiniMax Code 登录态：{AUTH_JSON}"}
    try:
        with open(AUTH_JSON, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        return {"error": f"读取 {AUTH_JSON} 失败: {e}"}

    rec = doc
    records = doc.get("records")
    if isinstance(records, dict) and records:
        cands = [v for v in records.values() if isinstance(v, dict) and v.get("accessToken")]
        if cands:
            rec = max(cands, key=lambda v: v.get("expiresAtMs") or 0)

    out = {
        "accessToken": (rec.get("accessToken") or "").strip(),
        "refreshToken": (rec.get("refreshToken") or "").strip(),
        "expiresAtMs": rec.get("expiresAtMs"),
        "userId": "",
        "deviceId": "",
        "source": "client:mcode-public",
    }
    try:
        with open(AGENT_CFG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        shared = cfg.get("sharedUser") or {}
        out["userId"] = str(shared.get("realUserID") or shared.get("userID") or "").strip()
        out["deviceId"] = str(shared.get("deviceID") or "").strip()
        out["userName"] = str(shared.get("userName") or "").strip()
    except Exception:
        pass
    return out


def _resolve(cfg: dict) -> dict:
    token = (cfg.get("accessToken") or "").strip()
    user_id = str(cfg.get("userId") or "").strip()
    device_id = str(cfg.get("deviceId") or "").strip()
    refresh = (cfg.get("refreshToken") or "").strip()
    source = "config" if token else ""
    if not token or not user_id:
        local = read_local_credentials()
        if "error" not in local:
            token = token or local.get("accessToken", "")
            refresh = refresh or local.get("refreshToken", "")
            user_id = user_id or local.get("userId", "")
            device_id = device_id or local.get("deviceId", "")
            source = source or local.get("source", "")
        elif not token:
            return {"error": local["error"]}
    return {"token": token, "refreshToken": refresh, "userId": user_id,
            "deviceId": device_id, "source": source}


def _fingerprint_seed(cfg: dict, r: dict = None) -> str:
    """指纹种子：优先 userId，其次 accessToken 前 32 位。

    以前 uuid 与 device_id 的兜底值是写死的常量（一个写死的 uuid / deviceId），
    所有用户共用同一个设备指纹——上游只要按 uuid 聚合，就能把所有人的签到
    看成一家的，风控也更容易把整批一起打死。这里改成按账号稳定派生：
    同一个人每次算出来一样（不破坏「设备不变」的语义），不同人不会撞。
    """
    src = r or cfg or {}
    for k in ("userId", "uid"):
        v = str(src.get(k) or "").strip()
        if v and v != "0":
            return v
    tok = str((cfg or {}).get("accessToken") or "").strip()
    if tok:
        return tok[:32]
    return "minimax"


def _uuid(cfg: dict, r: dict = None) -> str:
    u = (cfg.get("uuid") or "").strip()
    if u:
        return u
    seed = _fingerprint_seed(cfg, r)
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_DNS, "signhub-minimax:" + seed))


def _device_id(r: dict, cfg: dict) -> str:
    d = str((r or {}).get("deviceId") or "").strip()
    if d:
        return d
    seed = _fingerprint_seed(cfg, r)
    h = hashlib.sha256(("signhub-device:" + seed).encode("utf-8")).hexdigest()
    return str(int(h[:15], 16)).zfill(16)[:16]


def _params(r: dict, cfg: dict) -> str:
    now = int(time.time() * 1000)
    q = {
        "device_platform": "web",
        "biz_id": "3",
        "app_id": "3001",
        "version_code": APP_VERSION_CODE,
        "unix": str(now),
        "timezone_offset": "28800",
        "is_desktop": "1",
        "desktop_version": DESKTOP_VERSION,
        "sys_language": "zh",
        "lang": "zh",
        "uuid": _uuid(cfg, r),
        "device_id": _device_id(r, cfg),
        "os_name": "Windows",
        "browser_name": "Chrome",
        "device_memory": "16",
        "cpu_core_num": "20",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "user_id": r.get("userId") or "0",
        "screen_width": "1920",
        "screen_height": "1080",
        "client": "desktop",
    }
    return urllib.parse.urlencode(q), now


def _signed_request(path: str, r: dict, cfg: dict, body: str = "", method: str = "GET") -> tuple:
    query, now = _params(r, cfg)
    second = now // 1000
    full = path + "?" + query
    yy = _md5(urllib.parse.quote(full, safe="") + "_" + (body or "{}")
              + _md5(str(now)) + YY_SUFFIX)
    headers = {
        "User-Agent": UA,
        "Origin": BASE,
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "yy": yy,
        "x-timestamp": str(second),
        "x-signature": _md5(str(second) + SIGNATURE_SALT + body),
        # 两条认证通道，按 token 形态二选一：
        #  · 桌面客户端 token（`mmoat_` 开头）→ 服务端认 `Authorization: Bearer`
        #  · 网页端 localStorage['_token']（裸串，无前缀）→ 只认裸 `token` 头，
        #    Web 端 axios 拦截器就是 `e.headers.token = i`。
        # 扫完 agent.minimax.cn 全部 28 个 JS（2,630,423 字符），`mmoat_` 出现 0 次 ——
        # 前缀只存在于桌面客户端，不能拿它当网页 token 的判据。
        "token": r["token"],
    }
    if r["token"].startswith("mmoat_"):
        headers["Authorization"] = "Bearer " + r["token"]
    data = body.encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(BASE + full, data=data, headers=headers, method=method)
    proxy = (cfg.get("proxy") or "").strip()
    opener = urllib.request.build_opener() if not proxy else urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(60000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"__NETERR__{e}"


def _unwrap(status, raw: str) -> tuple:
    """统一剥 {data, base_resp} 信封。返回 (doc, data, err)。

    注意：MiniMax 在认证失败时返回的是 **HTTP 401 + {"error":"invalid access token"}**，
    既没有 base_resp 也没有 data。早先只判 base_resp.status_code，会把 401 误报成
    「响应缺少 data」，看起来像服务端结构异常，实际是 token 无效 —— 必须先判 HTTP 状态码。
    """
    if status is None:
        return None, None, raw.replace("__NETERR__", "网络请求失败: ")
    try:
        doc = json.loads(raw)
    except Exception:
        return None, None, f"响应非 JSON（HTTP {status}）：{raw[:160]}"
    if not isinstance(doc, dict):
        return None, None, "响应结构异常"
    br = doc.get("base_resp") or {}
    code = br.get("status_code")
    if code not in (0, None):
        return doc, None, f"接口返回错误 {code}：{br.get('status_msg') or ''}".strip()
    data = doc.get("data")
    if data is None:
        err_text = str(doc.get("error") or doc.get("message") or "").strip()
        if status in (401, 403):
            hint = f"（{err_text}）" if err_text else ""
            return doc, None, (
                f"token 无效或已过期{hint}：请重新「网页登录 MiniMax」取一份新的 "
                "localStorage['_token']，或在客户端里重新登录后点「读取本机凭据」"
            )
        if status and status >= 400:
            return doc, None, f"HTTP {status}：{err_text or raw[:160]}"
        return doc, None, "响应缺少 data" + (f"（{err_text}）" if err_text else "")
    return doc, data, ""


def fetch_status(r: dict, cfg: dict) -> tuple:
    status, raw = _signed_request(STATUS_PATH, r, cfg)
    doc, data, err = _unwrap(status, raw)
    return data, err


def _today(data: dict) -> dict:
    for d in (data.get("days") or []):
        if isinstance(d, dict) and d.get("is_today"):
            return d
    return {}


def fetch_credits(r: dict, cfg: dict) -> tuple:
    """查积分余额。返回 (info, err)。

    info 形如 {"total": 388, "remaining": 388.5, "granted": 400, "consumed": 11.5,
              "count": 1, "expire_at_ms": 1797696000000}。

    注意这个接口**没有 data 信封**，直接是 {"details":[...],"total_count":N,"base_resp":{...}}，
    所以不能走 _unwrap（它要求 data 存在）。也不能带 page/page_size —— 实测带参数反而 400。
    """
    status, raw = _signed_request(CREDIT_PATH, r, cfg)
    if status is None:
        return None, raw.replace("__NETERR__", "网络请求失败: ")
    try:
        doc = json.loads(raw)
    except Exception:
        return None, f"额度响应非 JSON（HTTP {status}）：{raw[:160]}"
    if not isinstance(doc, dict):
        return None, "额度响应结构异常"
    br = doc.get("base_resp") or {}
    code = br.get("status_code")
    if code not in (0, None):
        return None, f"接口返回错误 {code}：{br.get('status_msg') or ''}".strip()
    details = doc.get("details")
    if not isinstance(details, list):
        err_text = str(doc.get("error") or doc.get("message") or "").strip()
        if status in (401, 403):
            return None, (f"token 无效或已过期（{err_text or 'invalid access token'}）："
                          "请重新「网页登录 MiniMax」")
        return None, f"额度响应缺少 details（HTTP {status}）" + (f"：{err_text}" if err_text else "")

    rem = 0.0
    granted = 0.0
    consumed = 0.0
    expire_at = None
    for d in details:
        if not isinstance(d, dict):
            continue
        try:
            rem += float(d.get("remaining_amount") or 0)
            granted += float(d.get("granted_amount") or 0)
            consumed += float(d.get("consumed_amount") or 0)
        except (TypeError, ValueError):
            continue
        exp = d.get("expire_at_ms")
        if isinstance(exp, int) and (expire_at is None or exp < expire_at):
            expire_at = exp
    return {
        "total": int(rem),
        "remaining": round(rem, 2),
        "granted": round(granted, 2),
        "consumed": round(consumed, 2),
        "count": len(details),
        "expire_at_ms": expire_at,
        "unit": "credits",
    }, ""


def held_credits(cfg: dict) -> int:
    """只要一个剩余积分数字，读不到返回 -1（0 是「真的用完了」，不能用 0 表示读不到）。"""
    r = _resolve(cfg)
    if "error" in r:
        return -1
    if not r["token"]:
        return -1
    info, _err = fetch_credits(r, cfg)
    if not info:
        return -1
    v = info.get("total")
    return int(v) if isinstance(v, int) and v >= 0 else -1


def held_and_used(cfg: dict) -> tuple:
    """一次请求同时拿 (剩余, 累计已用)；读不到各自返回 -1。

    分两次调 fetch_credits 会打两次上游（各 ~250ms），签到流程里没必要。
    """
    r = _resolve(cfg)
    if "error" in r or not r["token"]:
        return -1, -1
    info, _err = fetch_credits(r, cfg)
    if not info:
        return -1, -1
    rem = info.get("total")
    used = info.get("consumed")
    return (
        int(rem) if isinstance(rem, int) and rem >= 0 else -1,
        int(used) if isinstance(used, (int, float)) and used >= 0 else -1,
    )


def run(cfg: dict) -> dict:
    r = _resolve(cfg)
    if "error" in r:
        return {"status": "fail", "http_status": None, "message": r["error"],
                "points": "", "detail": ""}
    if not r["token"]:
        return {"status": "fail", "http_status": None,
                "message": "缺少 accessToken：填网页端 localStorage['_token'] 的值，或在本机登录 MiniMax Code",
                "points": "", "detail": ""}

    data, err = fetch_status(r, cfg)
    if data is None:
        return {"status": "fail", "http_status": None, "message": err,
                "points": "", "detail": ""}

    today = _today(data)
    if (today.get("status") or 0) == 3:
        held, used = held_and_used(cfg)
        return {"status": "already", "http_status": 200,
                "message": f"今日已签到（第 {today.get('day_no')} 天，{today.get('points')} 分）"
                           + _streak_suffix(data),
                "points": "", "total_points": held, "used_points": used,
                "streak_days": _streak_days(data),
                "detail": json.dumps({"scene": data.get("scene"), "today": today,
                                      "held": held, "used": used}, ensure_ascii=False)}

    status, raw = _signed_request(CLAIM_PATH, r, cfg, body="{}", method="POST")
    doc, cdata, err = _unwrap(status, raw)
    if cdata is None:
        return {"status": "fail", "http_status": status, "message": err,
                "points": "", "detail": ""}

    result = cdata.get("claim_result")
    points = int(cdata.get("points") or 0)
    panel = cdata.get("panel") or {}
    if result == 1:
        held, used = held_and_used(cfg)
        return {"status": "success", "http_status": status,
                "message": f"签到成功 +{points} 分（第 {cdata.get('day_no')} 天）"
                           + _streak_suffix(panel),
                "points": f"+{points}" if points else "",
                "total_points": held, "used_points": used,
                "streak_days": _streak_days(panel) if panel else -1,
                "detail": json.dumps({"claim_id": cdata.get("claim_id"),
                                      "claim_result": result,
                                      "day_no": cdata.get("day_no"),
                                      "held": held,
                                      "used": used,
                                      "expire_at_ms": cdata.get("expire_at_ms")},
                                     ensure_ascii=False)}
    if result == 2:
        held, used = held_and_used(cfg)
        return {"status": "already", "http_status": status,
                "message": f"今日已签到（第 {cdata.get('day_no')} 天，{points} 分）"
                           + _streak_suffix(panel),
                "points": "", "total_points": held, "used_points": used,
                "streak_days": _streak_days(panel) if panel else -1,
                "detail": json.dumps({"claim_result": result, "held": held,
                                      "used": used}, ensure_ascii=False)}
    return {"status": "fail", "http_status": status,
            "message": f"签到未成功（claim_result={result}）", "points": "",
            "detail": json.dumps(cdata, ensure_ascii=False)[:400]}


def _streak_days(panel: dict) -> int:
    """连续签到天数（从 is_today 往前数 status==3）。取不到返回 -1（表示「没读到」）。"""
    days = sorted([d for d in (panel.get("days") or []) if isinstance(d, dict)],
                  key=lambda d: d.get("day_no") or 0)
    idx = next((i for i, d in enumerate(days) if d.get("is_today")), -1)
    if idx < 0:
        return -1
    cur = days[idx].get("status")
    start = idx if cur == 3 else (idx - 1 if cur == 2 else -1)
    n = 0
    while start >= 0 and (days[start].get("status") or 0) == 3:
        n += 1
        start -= 1
    return n if n else -1


def _streak_suffix(panel: dict) -> str:
    n = _streak_days(panel)
    return f" · 连签 {n} 天" if n > 0 else ""


def _streak_text(panel: dict) -> str:
    """连续签到天数的可读文案（给 probe 用）。"""
    n = _streak_days(panel)
    return f"连签 {n} 天" if n > 0 else ""


def quick(cfg: dict) -> dict:
    """只查本地凭据，不联网。给 /api/status 的 light 模式用。

    probe() 每次打一次 signin/status（实测 239ms），而「就绪与否」只看
    token + user_id 在不在。这里把 reachable/auth_ok 留成 None，
    语义是「没查」而不是「查不通」。
    """
    out = {"token_ok": False, "token_len": 0, "user_id": "", "device_id": "",
           "source": "", "reachable": None, "auth_ok": None,
           "checked_in": False, "claimable": False, "issues": [], "light": True}
    r = _resolve(cfg)
    if "error" in r:
        out["issues"].append(r["error"])
        return out
    out["token_ok"] = bool(r["token"])
    out["token_len"] = len(r["token"])
    out["user_id"] = r["userId"]
    out["device_id"] = r["deviceId"]
    out["source"] = r["source"]
    if not r["token"]:
        out["issues"].append("缺少 accessToken")
    return out


def probe(cfg: dict) -> dict:
    out = {"token_ok": False, "token_len": 0, "user_id": "", "device_id": "",
           "source": "", "reachable": False, "auth_ok": False,
           "checked_in": False, "claimable": False, "issues": []}
    r = _resolve(cfg)
    if "error" in r:
        out["issues"].append(r["error"])
        return out
    out["token_ok"] = bool(r["token"])
    out["token_len"] = len(r["token"])
    out["user_id"] = r["userId"]
    out["device_id"] = r["deviceId"]
    out["source"] = r["source"]
    if not r["token"]:
        out["issues"].append("缺少 accessToken")
        return out
    data, err = fetch_status(r, cfg)
    if data is None:
        out["issues"].append(err)
        return out
    out["reachable"] = True
    out["auth_ok"] = True
    today = _today(data)
    out["checked_in"] = (today.get("status") or 0) == 3
    out["claimable"] = any((d.get("status") or 0) == 2 for d in (data.get("days") or []))
    out["streak"] = _streak_text(data)
    credits, _cerr = fetch_credits(r, cfg)
    if credits:
        out["quota"] = credits
        out["held"] = credits.get("total")
    return out
