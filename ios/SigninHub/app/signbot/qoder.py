"""Qoder 国内版（阿里）每日 100 Credits 签到 —— 纯 HTTP 直连。

协议（对齐 Qoder App 0.2.x / 0.3.x 客户端，社区逆向成果 CreditDaddy / 10router 实测可用）：
    GET  {openapi}/sash/api/v1/me/campaigns?clientType=10
         → { uid, showCampaign, claimable, campaigns:[{campaignId, campaignKey, actionType,
             startAt, endAt, claimStatus, benefit:{kind:"CREDITS", amount, validity}}] }
    POST {openapi}/sash/api/v1/me/campaigns/{campaignId}/claim
         → { benefit:{amount} }

判定：
    actionType == "CLAIM_BENEFIT" 才是积分活动；claimStatus == "CLAIMABLE" 才是可领。
    claimStatus == "CLAIMED" → 今日已领（already）。
    没有任何 CLAIM_BENEFIT 活动 → 当前无可领活动（no-activity，不代表签过）。

凭据（Windows，无需客户端运行）：
    %APPDATA%\\com.qodercn.app.stable\\auth.v1.dat
        结构：b"v10" + nonce(12) + ciphertext+tag
        算法：AES-256-GCM，明文是 JSON
              {"token":"dt-...","refreshToken":"drt-...","expiresAt":...,"user":{"id":...}}
    %APPDATA%\\com.qodercn.app.stable\\Local State
        结构：{"os_crypt":{"encrypted_key":"<base64(DPAPI(32字节AES密钥))>"}}
        注意 encrypted_key 解 base64 后带 5 字节 "DPAPI" 前缀，要先剥掉再 CryptUnprotectData。
    %APPDATA%\\com.qodercn.app.stable\\auth.machine-id
        纯文本 UUID，作为 Cosy-MachineId。

国际版（openapi.qoder.sh）额外要求设备风控身份
（Cosy-MachineToken / Cosy-MachineCode / Cosy-MachineType，由客户端 umid 组件每 50 分钟生成），
本模块只做国内版；region=intl 时会在结果里提示需要设备组件。
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
import uuid

# iOS 移植补丁：non-Windows 上 ctypes.wintypes 会抛 ValueError（_type_ 'v'）。
try:
    import ctypes
    from ctypes import wintypes
except Exception:  # pragma: no cover - 非 Windows 平台
    ctypes = None
    wintypes = None

TIMEOUT = 25
UA = "Qoder"

BASE_CN = "https://openapi.qoder.com.cn"
BASE_INTL = "https://openapi.qoder.sh"

CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns?clientType=10"
CLAIM_PATH = "/sash/api/v1/me/campaigns/{cid}/claim"
REFRESH_PATH = "/api/v1/deviceToken/refresh"
# 额度：/api/v1/me/usage 返回 user_quota（套餐内）+ add_on_quota（签到/加油包）
# 实测 2026-11-20：{"user_quota":{"used":0,"remaining":300,"total":300,"unit":"credits"},
#                  "add_on_quota":{"used":0,"remaining":100,"total":100,...}}
USAGE_PATH = "/api/v1/me/usage"

CN_APP_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), "com.qodercn.app.stable"),
    os.path.join(os.environ.get("APPDATA", ""), "Qoder CN"),
]
INTL_APP_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), "com.qoder.app.stable"),
]


# ── Windows DPAPI ────────────────────────────────────────────────────────────
if ctypes is not None:  # iOS 移植补丁：无 ctypes 时不定义 DPAPI 结构体
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
else:  # pragma: no cover - 非 Windows 平台
    _DATA_BLOB = None


def _dpapi_unprotect(blob: bytes) -> bytes:
    """CryptUnprotectData：解当前用户 DPAPI 保护的字节。"""
    if ctypes is None:
        raise OSError("DPAPI 仅在 Windows 可用")
    buf = ctypes.create_string_buffer(blob, len(blob))
    din = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dout = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(din), None, None, None, None, 0, ctypes.byref(dout)
    )
    if not ok:
        raise OSError("CryptUnprotectData 失败（需用当前 Windows 用户运行）")
    try:
        return ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dout.pbData)


def _aes_key(app_dir: str) -> bytes:
    """从 Local State 读 os_crypt.encrypted_key → DPAPI 解出 32 字节 AES-256 密钥。"""
    path = os.path.join(app_dir, "Local State")
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    enc = (state.get("os_crypt") or {}).get("encrypted_key") or ""
    if not enc:
        raise ValueError("Local State 里没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    key = _dpapi_unprotect(raw)
    if len(key) != 32:
        raise ValueError(f"解出的 AES 密钥长度异常：{len(key)}")
    return key


def _aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    """v10 前缀的 AES-256-GCM：nonce = blob[3:15]，其余是 ciphertext||tag。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] != b"v10":
        raise ValueError("auth.v1.dat 不是 v10 格式")
    nonce = blob[3:15]
    ct = blob[15:]
    return AESGCM(key).decrypt(nonce, ct, None)


def read_local_credentials(region: str = "cn") -> dict:
    """读本机 Qoder 客户端的登录态。返回 {token, refreshToken, uid, machineId, source, dir}。"""
    dirs = CN_APP_DIRS if region == "cn" else INTL_APP_DIRS
    last_err = None
    for app_dir in dirs:
        auth_path = os.path.join(app_dir, "auth.v1.dat")
        if not os.path.exists(auth_path):
            continue
        try:
            with open(auth_path, "rb") as f:
                blob = f.read()
            key = _aes_key(app_dir)
            plain = _aes_gcm_decrypt(key, blob)
            doc = json.loads(plain.decode("utf-8"))
            machine = ""
            mid_path = os.path.join(app_dir, "auth.machine-id")
            if os.path.exists(mid_path):
                with open(mid_path, "r", encoding="utf-8") as f:
                    machine = f.read().strip()
            return {
                "token": (doc.get("token") or "").strip(),
                "refreshToken": (doc.get("refreshToken") or "").strip(),
                "uid": ((doc.get("user") or {}).get("id") or "").strip(),
                "name": ((doc.get("user") or {}).get("name") or "").strip(),
                "expiresAt": doc.get("expiresAt") or "",
                "machineId": machine,
                "source": f"client:{os.path.basename(app_dir)}",
                "dir": app_dir,
            }
        except Exception as e:  # 换下一个目录继续试
            last_err = f"{app_dir}: {e}"
            continue
    return {"error": last_err or "未找到 Qoder 客户端登录态（%APPDATA%\\com.qodercn.app.stable\\auth.v1.dat）"}


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _base(cfg: dict) -> str:
    region = (cfg.get("region") or "cn").strip().lower()
    if (cfg.get("baseUrl") or "").strip():
        return cfg["baseUrl"].strip().rstrip("/")
    return BASE_INTL if region == "intl" else BASE_CN


def _opener(proxy: str):
    proxy = (proxy or "").strip()
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )


def _headers(token: str, machine_id: str = "") -> dict:
    h = {
        "Authorization": "Bearer " + token,
        "Cosy-ClientType": "10",
        "Cosy-Version": "0.3.3",
        "Cosy-MachineOS": "windows",
        "User-Agent": UA,
        "Accept": "application/json",
    }
    if machine_id:
        h["Cosy-MachineId"] = machine_id
    return h


def _request(url: str, headers: dict, method: str = "GET", proxy: str = "",
             body: bytes = None) -> tuple:
    opener = _opener(proxy)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    if body is not None:
        req.add_header("Content-Length", str(len(body)))
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read(60000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"__NETERR__{e}"


def refresh_access_token(cfg: dict, refresh_token: str) -> tuple:
    """用 drt- 换新的 dt-。返回 (token, new_refresh, err)。

    这是官方 device flow 的续期端点（客户端 M7t）：
        POST {openapi}/api/v1/deviceToken/refresh  body {"refresh_token": "drt-..."}
        → {"device_token":"dt-...", "refresh_token":"drt-...",
           "expires_at":"...", "refresh_token_expires_at":"..."}
    实测 refreshToken 不是一次性的：同一个 drt- 反复换都能拿到新 token，
    有效期 30 天、refreshToken 有效期一年，所以只要手里有 drt- 就能长期续，
    不需要再登录一次。这是手机端「只填 refreshToken 也能一直签到」的依据。
    """
    rt = (refresh_token or "").strip()
    if not rt:
        return "", "", "缺少 refreshToken（drt- 开头）"
    body = json.dumps({"refresh_token": rt}).encode()
    opener = _opener(cfg.get("proxy") or "")
    req = urllib.request.Request(_base(cfg) + REFRESH_PATH, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            raw = r.read(8000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode("utf-8", "replace")
        return "", "", f"续期失败（HTTP {e.code}）：{detail[:160]}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", "", f"续期网络请求失败: {e}"
    try:
        doc = json.loads(raw)
    except Exception:
        return "", "", f"续期响应非 JSON：{raw[:160]}"
    token = (doc.get("device_token") or doc.get("token") or "").strip()
    new_rt = (doc.get("refresh_token") or "").strip()
    if not token:
        return "", "", f"续期响应缺少 device_token：{raw[:160]}"
    return token, new_rt or rt, ""


def _persist_token(token: str, refresh_token: str) -> None:
    """把续期拿到的新 token 写回 config.json，否则下一轮又拿旧 token 撞 401。"""
    if not token:
        return
    try:
        from .config import update_config

        def mut(c):
            qd = ((c.get("providers") or {}).get("qoder") or {}).get("qoder")
            if not isinstance(qd, dict):
                return
            if qd.get("token") != token:
                qd["token"] = token
            if refresh_token and qd.get("refreshToken") != refresh_token:
                qd["refreshToken"] = refresh_token

        update_config(mut)
    except Exception:
        pass


def _resolve(cfg: dict) -> dict:
    """配置优先，缺项时回落到本机客户端凭据。

    回落条件只判 token 与 refreshToken：
    - machineId / uid 都不是鉴权项（实测不带 Cosy-MachineId、uid 乱填都返回 200），
      手机上根本没有本机凭据，拿它们当必需项会让「只填 token」的用户永远签不了。
    - refreshToken（drt-）单独也能用：fetch_campaigns 遇到 401 会拿它续期，
      所以手机端只填 drt- 同样能长期签到。
    """
    region = (cfg.get("region") or "cn").strip().lower()
    token = (cfg.get("token") or "").strip()
    refresh = (cfg.get("refreshToken") or "").strip()
    uid = (cfg.get("uid") or "").strip()
    machine = (cfg.get("machineId") or "").strip()
    source = "config" if (token or refresh) else ""
    if not token and not refresh:
        local = read_local_credentials(region)
        if "error" not in local:
            token = local.get("token", "")
            refresh = refresh or local.get("refreshToken", "")
            uid = uid or local.get("uid", "")
            machine = machine or local.get("machineId", "")
            source = source or local.get("source", "")
        else:
            # 本机也没有客户端凭据（手机上必然如此）：直接告诉用户该填什么，
            # 不要把「读不到本机客户端」这种内部原因抛给用户。
            return {"error": "缺少凭据：填 dt- 开头的 accessToken，或 drt- 开头的 refreshToken"
                             "（手机上推荐只填 refreshToken，能自动续期一年）"}
    elif not uid or not machine:
        # 有凭据但缺 uid/machine：能从本机读到就补上，读不到也照签
        local = read_local_credentials(region)
        if "error" not in local:
            uid = uid or local.get("uid", "")
            machine = machine or local.get("machineId", "")
            token = token or local.get("token", "")
            refresh = refresh or local.get("refreshToken", "")
    return {"token": token, "refreshToken": refresh, "uid": uid,
            "machineId": machine, "source": source, "region": region}


def fetch_campaigns(cfg: dict, resolved: dict = None) -> tuple:
    r = resolved or _resolve(cfg)
    if "error" in r:
        return None, r["error"]
    status, raw = _request(_base(cfg) + CAMPAIGNS_PATH,
                           _headers(r["token"], r["machineId"]),
                           proxy=cfg.get("proxy") or "")
    if status in (401, 403) and r.get("refreshToken"):
        # token 过期但手里有 drt-：先续期再重试一次，成功就把新 token 写回配置
        new_tok, new_rt, err = refresh_access_token(cfg, r["refreshToken"])
        if new_tok:
            _persist_token(new_tok, new_rt)
            r["token"] = new_tok
            r["refreshToken"] = new_rt
            status, raw = _request(_base(cfg) + CAMPAIGNS_PATH,
                                   _headers(r["token"], r["machineId"]),
                                   proxy=cfg.get("proxy") or "")
    if status is None:
        return None, raw.replace("__NETERR__", "网络请求失败: ")
    if status in (401, 403):
        return None, (f"鉴权失败（HTTP {status}）：token 已过期且续期不成功，"
                      f"请重新登录 Qoder CN 客户端或在设置里重填 refreshToken")
    try:
        doc = json.loads(raw)
    except Exception:
        return None, f"活动列表响应非 JSON（HTTP {status}）：{raw[:160]}"
    if not isinstance(doc, dict):
        return None, "活动列表响应结构异常"
    return doc, ""


def _credit_campaigns(doc: dict) -> list:
    return [c for c in (doc.get("campaigns") or [])
            if isinstance(c, dict) and c.get("actionType") == "CLAIM_BENEFIT"]


def fetch_usage(cfg: dict, resolved: dict = None) -> tuple:
    """查账号当前持有额度。返回 (info, err)。

    info 形如 {"total": 400, "user": 300, "addon": 100, "plan": "Pro Trial",
              "expires_at": 1796368707808, "unit": "credits"}。
    total = user_quota.remaining + add_on_quota.remaining，是「还能用多少」，
    不是「总共多少」——卡片要显示的是剩余额度，跟 WorkBuddy 的持有积分口径一致。
    """
    r = resolved or _resolve(cfg)
    if "error" in r:
        return None, r["error"]
    status, raw = _request(_base(cfg) + USAGE_PATH,
                           _headers(r["token"], r["machineId"]),
                           proxy=cfg.get("proxy") or "")
    if status in (401, 403) and r.get("refreshToken"):
        new_tok, new_rt, _err = refresh_access_token(cfg, r["refreshToken"])
        if new_tok:
            _persist_token(new_tok, new_rt)
            r["token"] = new_tok
            r["refreshToken"] = new_rt
            status, raw = _request(_base(cfg) + USAGE_PATH,
                                   _headers(r["token"], r["machineId"]),
                                   proxy=cfg.get("proxy") or "")
    if status is None:
        return None, raw.replace("__NETERR__", "网络请求失败: ")
    if status in (401, 403):
        return None, f"鉴权失败（HTTP {status}）：token 已过期且续期不成功"
    try:
        doc = json.loads(raw)
    except Exception:
        return None, f"额度响应非 JSON（HTTP {status}）：{raw[:160]}"
    if not isinstance(doc, dict):
        return None, "额度响应结构异常"

    def _rem(key: str) -> int:
        q = doc.get(key) or {}
        try:
            v = q.get("remaining")
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def _used(key: str) -> int:
        q = doc.get(key) or {}
        try:
            v = q.get("used")
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    user_q = doc.get("user_quota") or {}
    addon_q = doc.get("add_on_quota") or {}
    user_rem = _rem("user_quota")
    addon_rem = _rem("add_on_quota")
    user_used = _used("user_quota")
    addon_used = _used("add_on_quota")
    return {
        "total": user_rem + addon_rem,
        "used": user_used + addon_used,
        "user": user_rem,
        "addon": addon_rem,
        "user_used": user_used,
        "addon_used": addon_used,
        "user_total": user_q.get("total"),
        "addon_total": addon_q.get("total"),
        "plan": doc.get("plan_tier_name") or "",
        "user_type": doc.get("user_type") or "",
        "expires_at": doc.get("expires_at"),
        "unit": user_q.get("unit") or addon_q.get("unit") or "credits",
    }, ""


def held_credits(cfg: dict) -> int:
    """只要一个剩余额度数字，读不到返回 -1（绝不返回 0，0 是「真的用完了」）。"""
    info, _err = fetch_usage(cfg)
    if not info:
        return -1
    v = info.get("total")
    return int(v) if isinstance(v, int) and v >= 0 else -1


def used_credits(cfg: dict) -> int:
    """只要一个「累计已用」数字，读不到返回 -1。"""
    info, _err = fetch_usage(cfg)
    if not info:
        return -1
    v = info.get("used")
    return int(v) if isinstance(v, int) and v >= 0 else -1



def run(cfg: dict) -> dict:
    """执行一次 Qoder 签到（领取所有可领的积分活动）。"""
    r = _resolve(cfg)
    if "error" in r:
        return {"status": "fail", "http_status": None, "message": r["error"],
                "points": "", "detail": ""}
    if not r["token"] and r.get("refreshToken"):
        # 只有 refreshToken（手机端常见）：先换一个 dt- 再往下走
        new_tok, new_rt, err = refresh_access_token(cfg, r["refreshToken"])
        if not new_tok:
            return {"status": "fail", "http_status": None,
                    "message": f"refreshToken 续期失败：{err}",
                    "points": "", "detail": ""}
        _persist_token(new_tok, new_rt)
        r["token"] = new_tok
        r["refreshToken"] = new_rt
    if not r["token"]:
        return {"status": "fail", "http_status": None,
                "message": "缺少凭据：填 dt- 开头的 accessToken，或 drt- 开头的 refreshToken"
                           "（手机上推荐只填 refreshToken，能自动续期一年）",
                "points": "", "detail": ""}

    doc, err = fetch_campaigns(cfg, r)
    if doc is None:
        return {"status": "fail", "http_status": None, "message": err,
                "points": "", "detail": ""}

    uid = (doc.get("uid") or r.get("uid") or "").strip()
    credits = _credit_campaigns(doc)
    claimable = [c for c in credits if (c.get("claimStatus") or "").upper() == "CLAIMABLE"]

    if not claimable:
        if not credits:
            # 「没有活动」不是失败：以前回 fail 会让卡片每天亮红点，
            # 用户以为签到坏了。这是「今天没有可领的东西」，按已签到（黄点）处理。
            return {"status": "already", "http_status": 200,
                    "message": "当前没有可领取的积分活动（活动未开窗或已结束）",
                    "points": "", "detail": json.dumps({"uid": uid}, ensure_ascii=False)}
        amount = sum(int((c.get("benefit") or {}).get("amount") or 0) for c in credits)
        # total_points 取账号真实持有额度（/api/v1/me/usage），不是活动面额：
        # 活动面额只是「今天领了多少」，卡片要显示的是「现在还有多少可用」。
        # 同一次 usage 查询顺带拿到「累计已用」，一起回传供资产卡翻转显示。
        usage, _uerr = fetch_usage(cfg, r)
        held = int(usage.get("total")) if usage and isinstance(usage.get("total"), int) else -1
        used = int(usage.get("used")) if usage and isinstance(usage.get("used"), int) else -1
        return {"status": "already", "http_status": 200,
                "message": f"今日已领取（本期 {amount} Credits）· 每日 10:00（UTC+8）刷新",
                # total_points 只放数字：卡片把它当「当前积分」显示，
                # 塞 "100 Credits" 进去 Number() 会变 NaN，整行积分就不显示了。
                # 单位在 message 里已经说清楚。
                "points": "", "total_points": held, "used_points": used,
                "detail": json.dumps({"uid": uid, "held": held, "used": used, "campaigns": [
                    {"id": c.get("campaignId"), "status": c.get("claimStatus")} for c in credits
                ]}, ensure_ascii=False)}

    total = 0
    claimed = []
    errors = []
    for c in claimable:
        cid = c.get("campaignId") or ""
        # claim 是 POST，必须带 body：以前发的是「无 body 的 POST」，
        # 服务端按 Content-Length: 0 解析，能否成功全看它对空 body 的宽容度。
        status, raw = _request(_base(cfg) + CLAIM_PATH.format(cid=cid),
                               _headers(r["token"], r["machineId"]),
                               method="POST", proxy=cfg.get("proxy") or "",
                               body=b"{}")
        if status is None:
            errors.append(raw.replace("__NETERR__", "网络请求失败: "))
            continue
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        # 只看 HTTP 2xx 是不够的：业务失败也是 200，body 里带非 0 的 code。
        # 以前一律当成功，于是「领取失败」被记成「已领 +N 积分」。
        code = body.get("code")
        biz_ok = code in (0, None) and not body.get("error")
        if 200 <= (status or 0) < 300 and biz_ok:
            amount = int(((body.get("benefit") or {}).get("amount"))
                         or ((c.get("benefit") or {}).get("amount")) or 0)
            total += amount
            claimed.append({"campaignId": cid, "campaignKey": c.get("campaignKey"),
                            "amount": amount, "status": "claimed"})
        elif 200 <= (status or 0) < 300:
            errors.append(f"业务失败 code={code} {str(body.get('msg') or body.get('message') or '')[:120]}")
        else:
            errors.append(f"HTTP {status} {raw[:120]}")

    if claimed:
        usage, _uerr = fetch_usage(cfg, r)
        held = int(usage.get("total")) if usage and isinstance(usage.get("total"), int) else -1
        used = int(usage.get("used")) if usage and isinstance(usage.get("used"), int) else -1
        return {"status": "success", "http_status": 200,
                "message": f"已领取 +{total} Credits · 每日 10:00（UTC+8）刷新",
                "points": f"+{total}", "total_points": held, "used_points": used,
                "detail": json.dumps({"uid": uid, "claimed": claimed, "held": held,
                                      "used": used}, ensure_ascii=False)}
    return {"status": "fail", "http_status": 200,
            "message": f"领取失败：{'；'.join(errors) or '未知原因'}",
            "points": "", "detail": json.dumps({"uid": uid}, ensure_ascii=False)}


def quick(cfg: dict) -> dict:
    """只查本地凭据，不联网。给 /api/status 的 light 模式用。

    probe() 每次都要打一次 openapi（实测 253ms），而「就绪与否」只看
    token + machineId 在不在，完全不需要联网。所以这里复用 _resolve()
    的字段，把 reachable/auth_ok 留成 False，语义是「没查」而不是「查不通」。
    """
    out = {"token_ok": False, "token_len": 0, "machine_id": "",
           "uid": "", "source": "", "region": (cfg.get("region") or "cn"),
           "base": _base(cfg), "reachable": None, "auth_ok": None,
           "checked_in": False, "claimable": False, "issues": [], "light": True}
    r = _resolve(cfg)
    if "error" in r:
        out["issues"].append(r["error"])
        return out
    out["token_ok"] = bool(r["token"] or r.get("refreshToken"))
    out["token_len"] = len(r["token"])
    out["machine_id"] = r["machineId"]
    out["uid"] = r["uid"]
    out["source"] = r["source"]
    out["refresh_ok"] = bool(r.get("refreshToken"))
    if not r["token"] and not r.get("refreshToken"):
        out["issues"].append("缺少凭据：填 dt- 的 accessToken 或 drt- 的 refreshToken")
    if r["region"] == "intl" and not cfg.get("machineToken"):
        out["issues"].append("国际版需要设备风控身份（Cosy-MachineToken/Code/Type），本模块只支持国内版")
    return out


def probe(cfg: dict) -> dict:
    """只读体检：本地凭据是否可解、接口是否可达、今日是否已领。"""
    out = {"token_ok": False, "token_len": 0, "machine_id": "",
           "uid": "", "source": "", "region": (cfg.get("region") or "cn"),
           "base": _base(cfg), "reachable": False, "auth_ok": False,
           "checked_in": False, "issues": []}
    r = _resolve(cfg)
    if "error" in r:
        out["issues"].append(r["error"])
        return out
    out["token_ok"] = bool(r["token"] or r.get("refreshToken"))
    out["token_len"] = len(r["token"])
    out["machine_id"] = r["machineId"]
    out["uid"] = r["uid"]
    out["source"] = r["source"]
    out["refresh_ok"] = bool(r.get("refreshToken"))
    if not r["token"] and not r.get("refreshToken"):
        out["issues"].append("缺少凭据：填 dt- 的 accessToken 或 drt- 的 refreshToken")
        return out
    if r["region"] == "intl" and not cfg.get("machineToken"):
        out["issues"].append("国际版需要设备风控身份（Cosy-MachineToken/Code/Type），本模块只支持国内版")
    if not r["token"] and r.get("refreshToken"):
        # 只有 drt-：先续期，续不上就没必要再打活动列表
        new_tok, new_rt, err = refresh_access_token(cfg, r["refreshToken"])
        if not new_tok:
            out["issues"].append(f"refreshToken 续期失败：{err}")
            return out
        _persist_token(new_tok, new_rt)
        r["token"] = new_tok
        r["refreshToken"] = new_rt
        out["token_ok"] = True
        out["token_len"] = len(new_tok)
    doc, err = fetch_campaigns(cfg, r)
    if doc is None:
        out["issues"].append(err)
        return out
    out["reachable"] = True
    out["auth_ok"] = True
    out["uid"] = (doc.get("uid") or out["uid"] or "").strip()
    credits = _credit_campaigns(doc)
    out["checked_in"] = bool(credits) and not any(
        (c.get("claimStatus") or "").upper() == "CLAIMABLE" for c in credits)
    out["claimable"] = any((c.get("claimStatus") or "").upper() == "CLAIMABLE" for c in credits)
    usage, _uerr = fetch_usage(cfg, r)
    if usage:
        out["quota"] = usage
        out["held"] = usage.get("total")
    return out
