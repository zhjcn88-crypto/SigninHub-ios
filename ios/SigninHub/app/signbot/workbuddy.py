"""WorkBuddy 真签到实现。

协议来自 dsh-connect-workbuddy-models 的回源核对（src/upstream.ts）：
  - 签到状态: POST {billing}/v2/billing/meter/checkin-activity-status  body {}
  - 领取签到: POST {billing}/v2/billing/meter/daily-checkin          body {}
  - 刷新令牌: POST {chat}/v2/plugin/auth/token/refresh
    请求头: X-Refresh-Token: <refreshToken>, X-Auth-Refresh-Source: workbuddy
  - 信封: {"code":0,"msg":...,"data":{...}}，code==0 为成功
  - 基址: CN chat=https://copilot.tencent.com  CN billing=https://www.codebuddy.cn
          GLOBAL 两者都是 https://www.workbuddy.ai

凭据来源（只读，不回写）：
  1. DSH vault: ~/.dsh/workbuddy-vault/{cn,global}/*.json  —— 明文 accessToken
  2. 桌面端:    %LOCALAPPDATA%|%APPDATA%/CodeBuddyExtension/Data/Public/auth/*.info
     （Windows 新版本把 token 存成 $wbEncrypted 信封，无 at-rest 密钥时解不开 → 明确报告）
  3. 配置里手动填的 accessToken
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from . import netguard

CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"
GLOBAL_BASE = "https://www.workbuddy.ai"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
TIMEOUT = 20

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"


# ── 凭据 ──────────────────────────────────────────────────────────────
class Credential:
    def __init__(self, access_token, refresh_token="", expires_at=0, last_refresh=0,
                 uid="", uin="", enterprise_id="", domain="", source=""):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at or 0
        self.last_refresh = last_refresh or 0
        self.uid = uid or ""
        self.uin = uin or ""
        self.enterprise_id = enterprise_id or ""
        self.domain = domain or ""
        self.source = source or ""

    def account_id(self):
        stable = self.uin or self.uid or "unknown"
        return hashlib.sha256(("workbuddy\0" + stable).encode()).hexdigest()[:16]

    def masked(self):
        t = self.access_token
        shown = (t[:6] + "…" + t[-4:]) if len(t) > 14 else "***"
        return f"{shown} (len={len(t)})"


def _is_encrypted(v):
    return isinstance(v, dict) and ("$wbEncrypted" in v or "envelope" in v)


def _vault_dirs():
    home = os.path.expanduser("~")
    base = os.path.join(home, ".dsh", "workbuddy-vault")
    return [os.path.join(base, "cn"), os.path.join(base, "global")]


def _desktop_auth_dirs():
    out = []
    for env in ("LOCALAPPDATA", "APPDATA"):
        root = os.environ.get(env)
        if root:
            p = os.path.join(root, "CodeBuddyExtension", "Data", "Public", "auth")
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def discover_credentials():
    """返回 (credentials, problems)。problems 说明为什么某些文件读不出来。"""
    creds = []
    problems = []

    # 1) DSH vault（明文，优先）
    for d in _vault_dirs():
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    doc = json.load(f)
                auth = doc.get("auth") or {}
                acct = doc.get("account") or {}
                tok = auth.get("accessToken")
                if not isinstance(tok, str) or not tok:
                    problems.append(f"{os.path.basename(d)}/{name}: 没有可用 accessToken")
                    continue
                creds.append(Credential(
                    access_token=tok,
                    refresh_token=auth.get("refreshToken") if isinstance(auth.get("refreshToken"), str) else "",
                    expires_at=int(auth.get("expiresAt") or 0),
                    last_refresh=int(auth.get("lastRefreshTime") or 0),
                    uid=str(acct.get("uid") or ""),
                    uin=str(acct.get("uin") or ""),
                    enterprise_id=str(acct.get("enterpriseId") or ""),
                    domain=str(auth.get("domain") or ""),
                    source=f"vault:{os.path.basename(d)}",
                ))
            except Exception as e:
                problems.append(f"{name}: 解析失败 {e}")

    # 2) 桌面端 .info（可能加密）
    for d in _desktop_auth_dirs():
        for name in sorted(os.listdir(d)):
            if not name.endswith(".info"):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    doc = json.load(f)
            except Exception as e:
                problems.append(f"{name}: 解析失败 {e}")
                continue
            auth = doc.get("auth") or {}
            tok = auth.get("accessToken")
            if _is_encrypted(tok):
                problems.append(
                    f"{name}: accessToken 是加密信封（$wbEncrypted），本程序不持有 at-rest 密钥，"
                    "请改为在设置里粘贴 accessToken，或使用上面的 vault 凭据"
                )
                continue
            if not isinstance(tok, str) or not tok:
                problems.append(f"{name}: 没有 accessToken")
                continue
            acct = doc.get("account") or {}
            ident = doc.get("identity") or {}
            if _is_encrypted(acct):
                acct = {}
            creds.append(Credential(
                access_token=tok,
                refresh_token=auth.get("refreshToken") if isinstance(auth.get("refreshToken"), str) else "",
                expires_at=int(auth.get("expiresAt") or 0),
                last_refresh=int(auth.get("lastRefreshTime") or 0),
                uid=str((ident or {}).get("uid") or acct.get("uid") or ""),
                uin=str((ident or {}).get("uin") or acct.get("uin") or ""),
                enterprise_id=str((ident or {}).get("enterpriseId") or acct.get("enterpriseId") or ""),
                domain=str(auth.get("domain") or ""),
                source=f"desktop:{name}",
            ))

    # 去重（同账号保留最新签发）
    best = {}
    for c in creds:
        k = c.account_id()
        if k not in best or c.last_refresh > best[k].last_refresh:
            best[k] = c
    creds = sorted(best.values(), key=lambda c: c.last_refresh, reverse=True)
    return creds, problems


# ── HTTP ──────────────────────────────────────────────────────────────
def region_of(domain):
    d = (domain or "").lower()
    if d and ("workbuddy.ai" in d or "global" in d):
        return "global"
    return "cn"


def billing_base(cred):
    return GLOBAL_BASE if region_of(cred.domain) == "global" else CN_BILLING_BASE


def chat_base(cred):
    return GLOBAL_BASE if region_of(cred.domain) == "global" else CN_CHAT_BASE


def _referer(cred):
    return GLOBAL_BASE if region_of(cred.domain) == "global" else CN_BILLING_BASE


def _common_headers(cred):
    ref = _referer(cred)
    return {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": ref,
        "Referer": ref + "/",
        "User-Agent": CLIENT_UA,
    }


def billing_headers(cred):
    h = _common_headers(cred)
    h.update({
        "Authorization": "Bearer " + cred.access_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if cred.uid:
        h["X-User-Id"] = cred.uid
    if cred.enterprise_id:
        h["X-Enterprise-Id"] = cred.enterprise_id
        h["X-Tenant-Id"] = cred.enterprise_id
    if cred.domain:
        h["X-Domain"] = cred.domain
    return h


def _post_json(url, headers, body="{}"):
    """返回 (status, text)。网络层异常抛 RuntimeError。

    BUGS-v2.63-288 P3：出站统一走 netguard.open_checked()（目标校验 + 逐跳重定向
    复检）。这里的 host 可能来自 `cred.domain` / 自定义 base，属于用户可控。
    """
    data = body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        return netguard.open_checked(req, timeout=TIMEOUT, limit=20000)
    except netguard.BlockedTarget as e:
        raise RuntimeError("网络错误: %s" % e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"网络错误: {e}")


def _envelope(status, text):
    try:
        doc = json.loads(text)
    except Exception:
        return status, {"code": -1, "msg": text[:300], "data": None}
    if not isinstance(doc, dict):
        return status, {"code": -1, "msg": text[:300], "data": None}
    return status, doc


# ── 业务 ──────────────────────────────────────────────────────────────
def fetch_status(cred):
    status, text = _post_json(billing_base(cred) + STATUS_PATH, billing_headers(cred))
    _, env = _envelope(status, text)
    if status in (401, 403):
        raise PermissionError(f"凭据被拒绝（http {status}）")
    # 「code != 0 或没有 data」= 该账号没有签到活动，不是错误。
    # 官方桌面客户端 `DesktopAccountRepo.checkinStatus()` 的写法就是
    # `if (e?.code !== 0 || !e.data) return;`（当作没有活动直接返回）。
    # 国际版账号实测走这条路：国内客户端日志对国际版 accountUid 打印
    # `[Checkin] refreshStatus -> inactive {"hasData":false}`。
    if env.get("code") not in (0, None) or env.get("data") is None:
        return {
            "no_activity": True,
            "no_activity_reason": str(env.get("msg") or f"code={env.get('code')}")[:160],
            "today_checked_in": False, "streak_days": 0,
            "daily_credit": 0, "today_credit": 0, "total_credits": -1,
            "active": False, "claim_button_text": "", "raw": env.get("data") or {},
        }
    data = env.get("data") or {}
    return {
        "no_activity": data.get("active") is False,
        "no_activity_reason": "" if data.get("active") is not False else "活动未开启或已结束",
        "today_checked_in": data.get("today_checked_in") is True,
        "streak_days": int(data.get("streak_days") or 0),
        "daily_credit": int(data.get("daily_credit") or 0),
        "today_credit": int(data.get("today_credit") or 0),
        # 账号累计积分：2026-11 起上游返回 total_credits（带 s），老版本可能是别的名字
        "total_credits": _first_int(data, "total_credits", "total_credit", "totalCredits",
                                    "credit_total", "sum_credit", "credit"),
        "active": data.get("active") is True,
        "claim_button_text": data.get("claim_button_text") or "",
        "raw": data,
    }


def _first_int(data, *keys):
    """按顺序取第一个能转成整数的字段，都没有返回 -1。"""
    for k in keys:
        v = data.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return -1


def claim_daily(cred):
    status, text = _post_json(billing_base(cred) + CLAIM_PATH, billing_headers(cred))
    _, env = _envelope(status, text)
    if status in (401, 403):
        raise PermissionError(f"凭据被拒绝（http {status}）")
    if env.get("code") != 0:
        # 10001 = 风控/网关层拒绝，**不是**活动业务码（官方 mapCheckinStatus 只认 1001/1002/1003）。
        # 桌面端签到会额外带 `X-Device-Token`（腾讯 Turing 风控 SDK 的 `v3:` 设备令牌，见
        # app.asar 的 DesktopAccountRepo.getCheckinRequestHeaders），Android 端拿不到。
        # 所以这里不是「失败」，而是「这个账号没有可领的活动」—— 返回标记让 run() 收敛。
        return {
            "no_activity": True,
            "no_activity_reason": str(env.get("msg") or f"code={env.get('code')}")[:160],
            "credit": 0, "streak_days": 0, "total_credits": -1, "raw": env.get("data") or {},
        }
    data = env.get("data") or {}
    return {
        "no_activity": False,
        "no_activity_reason": "",
        "credit": int(data.get("credit") or 0),
        "streak_days": int(data.get("streak_days") or 0),
        # 领奖响应里不一定带累计积分，读不到就 -1，交给 run() 再查一次 status 补齐
        "total_credits": _first_int(data, "total_credits", "total_credit", "totalCredits",
                                    "credit_total", "sum_credit"),
        "raw": data,
    }


def refresh(cred):
    """用 refreshToken 换新 accessToken；成功返回新的 Credential。"""
    if not cred.refresh_token:
        raise RuntimeError("没有 refreshToken，无法自动续期")
    h = _common_headers(cred)
    h.update({
        "Accept": "application/json",
        "X-Refresh-Token": cred.refresh_token,
        "X-Auth-Refresh-Source": "workbuddy",
    })
    status, text = _post_json(chat_base(cred) + REFRESH_PATH, h)
    _, env = _envelope(status, text)
    if status in (401, 403):
        raise PermissionError("refresh token 被拒绝，请重新登录 WorkBuddy")
    if env.get("code") != 0:
        raise RuntimeError(f"刷新失败 code={env.get('code')} msg={str(env.get('msg'))[:160]}")
    data = env.get("data") or {}
    tok = data.get("accessToken") or data.get("access_token")
    if not tok:
        raise RuntimeError("刷新响应里没有 accessToken")
    return Credential(
        access_token=tok,
        refresh_token=data.get("refreshToken") or cred.refresh_token,
        expires_at=int(data.get("expiresAt") or cred.expires_at or 0),
        last_refresh=int(time.time()),
        uid=cred.uid, uin=cred.uin, enterprise_id=cred.enterprise_id,
        domain=cred.domain, source=cred.source + "+refreshed",
    )


# ── 对外入口 ───────────────────────────────────────────────────────────
def resolve_credential(wb_cfg):
    """按优先级取一份凭据：配置里手填 > 本机发现。返回 (cred, problems)。"""
    token = (wb_cfg.get("accessToken") or "").strip()
    want = (wb_cfg.get("region") or "").strip().lower()
    problems = []
    if token:
        domain = (wb_cfg.get("domain") or "").strip()
        # 手填凭据时 domain 常常是空的（用户只粘了 accessToken）。而 region_of()
        # 只看 domain —— 空 domain 会被判成国内，于是国际版的 token 被发到
        # codebuddy.cn，永远 401（用户 m19236）。所以这里用 region 兜底补 domain，
        # 让后面所有按 domain 选域名的分支（billing_base / chat_base / _referer）自动走对。
        if not domain and want == "global":
            domain = "workbuddy.ai"
        return Credential(
            access_token=token,
            refresh_token=(wb_cfg.get("refreshToken") or "").strip(),
            uid=(wb_cfg.get("uid") or "").strip(),
            uin=(wb_cfg.get("uin") or "").strip(),
            enterprise_id=(wb_cfg.get("enterpriseId") or "").strip(),
            domain=domain,
            source="config",
        ), problems
    creds, problems = discover_credentials()
    if want in ("cn", "global"):
        creds = [c for c in creds if region_of(c.domain) == want]
    if not creds:
        return None, problems
    return creds[0], problems


def run(wb_cfg):
    """执行一次 WorkBuddy 签到。返回统一结果字典。"""
    cred, problems = resolve_credential(wb_cfg or {})
    if cred is None:
        msg = "没有可用的 WorkBuddy 凭据。"
        if problems:
            msg += " 本机探测结果：" + "；".join(problems[:2])
        msg += " 请在设置里填写 accessToken，或先登录 WorkBuddy 桌面端 / DSH 插件。"
        return {"status": "fail", "http_status": None, "message": msg,
                "points": "", "detail": json.dumps({"problems": problems}, ensure_ascii=False)}

    detail = {"source": cred.source, "account": cred.account_id(), "region": region_of(cred.domain)}
    try:
        # 余额查询（billing get-user-resource）与签到状态查询互不依赖，两条都走网络，
        # 串行就是 400+300ms。这里先把余额请求抛到后台线程，状态回来时它多半已经到了；
        # 只有「今日已签到」那条分支用得上余额，未签到时多花的一次请求可以忽略。
        from concurrent.futures import ThreadPoolExecutor
        held_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wbheld")
        f_held = held_pool.submit(_held_credits, cred)
        try:
            st = fetch_status(cred)
        finally:
            pass
        detail["status"] = st
        # 该账号没有签到活动（国际版必然走这条，国内版在活动空窗期也会）——
        # 这不是失败：余额照读，状态按「无可领活动」返回，前端不该弹红条。
        if st.get("no_activity"):
            try:
                held = f_held.result(timeout=15)
            except Exception:
                held = -1
            held_pool.shutdown(wait=False)
            is_intl = region_of(cred.domain) == "global"
            head = "国际版账号没有签到活动（只能读余额）" if is_intl else "该账号当前没有可领取的签到活动"
            reason = st.get("no_activity_reason") or ""
            return {
                "status": "already", "http_status": 200,
                "message": head + (f" · {reason}" if reason else "")
                           + (f" · 当前积分 {held}" if held >= 0 else ""),
                "points": "",
                "total_points": held,
                "streak_days": -1,
                "detail": json.dumps(detail, ensure_ascii=False),
            }
        if st["today_checked_in"]:
            try:
                held = f_held.result(timeout=15)
            except Exception:
                held = -1
            held_pool.shutdown(wait=False)
            return {
                "status": "already", "http_status": 200,
                "message": f"今日已签到 · 连续 {st['streak_days']} 天"
                           + (f" · 今日 {st['today_credit']} 积分" if st["today_credit"] else ""),
                "points": (f"+{st['today_credit']}" if st["today_credit"] else ""),
                "total_points": held,
                "streak_days": st["streak_days"],
                "detail": json.dumps(detail, ensure_ascii=False),
            }
        held_pool.shutdown(wait=False)
        res = claim_daily(cred)
        detail["claim"] = res
        if res.get("no_activity"):
            held = _held_credits(cred)
            is_intl = region_of(cred.domain) == "global"
            head = "国际版账号没有签到活动（只能读余额）" if is_intl else "该账号当前没有可领取的签到活动"
            reason = res.get("no_activity_reason") or ""
            return {
                "status": "already", "http_status": 200,
                "message": head + (f" · {reason}" if reason else "")
                           + (f" · 当前积分 {held}" if held >= 0 else ""),
                "points": "",
                "total_points": held,
                "streak_days": st.get("streak_days", -1),
                "detail": json.dumps(detail, ensure_ascii=False),
            }
        total = _after_total(cred, res)
        return {
            "status": "success", "http_status": 200,
            "message": f"签到成功 · +{res['credit']} 积分 · 连续 {res['streak_days']} 天",
            "points": f"+{res['credit']}",
            "total_points": total,
            "streak_days": res["streak_days"],
            "detail": json.dumps(detail, ensure_ascii=False),
        }
    except PermissionError as e:
        # 尝试用 refreshToken 自动续期后重试一次
        try:
            new_cred = refresh(cred)
            detail["refreshed"] = True
            st = fetch_status(new_cred)
            if st["today_checked_in"]:
                return {"status": "already", "http_status": 200,
                        "message": f"（已自动续期）今日已签到 · 连续 {st['streak_days']} 天",
                        "points": "", "total_points": _held_credits(new_cred),
                        "streak_days": st["streak_days"],
                        "detail": json.dumps(detail, ensure_ascii=False)}
            res = claim_daily(new_cred)
            detail["claim"] = res
            return {"status": "success", "http_status": 200,
                    "message": f"（已自动续期）签到成功 · +{res['credit']} 积分 · 连续 {res['streak_days']} 天",
                    "points": f"+{res['credit']}",
                    "total_points": _after_total(new_cred, res),
                    "streak_days": res["streak_days"],
                    "detail": json.dumps(detail, ensure_ascii=False)}
        except Exception as e2:
            return {"status": "fail", "http_status": None,
                    "message": f"{e}；自动续期也失败：{e2}。请重新登录 WorkBuddy 或更新 accessToken。",
                    "points": "", "detail": json.dumps(detail, ensure_ascii=False)}
    except Exception as e:
        return {"status": "fail", "http_status": None, "message": str(e),
                "points": "", "detail": json.dumps(detail, ensure_ascii=False)}


def _held_credits(cred):
    """账号**当前持有**的可用积分。

    注意别用 checkin-activity-status 里的 total_credits —— 那是「活动累计发放」
    （实测某账号 total_credits=2200，而真实可用只有 531.99），拿它当余额会虚高。
    真实余额走 billing 的 get-user-resource，按每个包的
    min(CapacityRemainPrecise, CycleCapacityRemainPrecise) 求和（与官网积分明细一致）。
    失败返回 -1，绝不臆测。
    """
    try:
        from . import wb_assets  # 局部导入：wb_assets 反过来依赖本模块
        b = wb_assets.balance(cred)
        v = b.get("remaining")
        # 向下取整，与官网「积分明细」的显示一致（531.99 显示 531，不四舍五入成 532）
        return int(float(v)) if v is not None else -1
    except Exception:
        return -1


def _after_total(cred, claim_res):
    """签到成功后拿**当前持有**积分。失败返回 -1（不臆测）。"""
    held = _held_credits(cred)
    if held >= 0:
        return held
    return -1