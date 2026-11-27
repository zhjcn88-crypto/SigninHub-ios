"""WorkBuddy 积分资产 / 过期预警。

两条链路：
  余额    POST {base}/v2/billing/meter/get-user-resource          body {}
          -> data.Response.Data.Accounts[]，每项有 CapacityRemainPrecise 等
  赠送包  POST {base}/v2/billing/meter/get-user-resource-free-packages
          body {"PackageCodes":[...],"PageNumber":n,"PageSize":200}
          -> data.Accounts[]，每项有 CycleCapacityRemainPrecise + CycleEndTime

设计要点（对齐参考实现）：
- 可用额度 = min(剩余, 本周期剩余)，只在周期额度 > 0 时才取 min。
- 0 = 免费订阅包（无额度）、-1 = 无限量包，都不计入「将过期积分」。
- 查询失败**不返回 0**，返回 None —— 前端就不显示这一行，绝不臆测。
- 结果缓存 300 秒，避免每次刷新都打上游。
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import netguard
from . import workbuddy as wb

TZ = timezone(timedelta(hours=8))
TIMEOUT = 20
CACHE_TTL = 300

_CACHE = {}
_LOCK = threading.Lock()
# 上次拿到的 PackageCodes，按账号存。刷新时用它先把「赠送包到期」查询
# 和余额/汇总查询并行起来，省掉一次串行等待（实测省 ~380ms）。
_CODES = {}
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wbassets")


def _base(cred):
    """余额接口的基址：国内 billing 走 www.codebuddy.cn，全球走 workbuddy.ai。"""
    return wb.billing_base(cred)


def _hdrs(cred):
    h = wb.billing_headers(cred)
    # 这几个接口只吃 JSON 头，去掉 X-Requested-With 之外的差异即可
    h["Content-Type"] = "application/json"
    return h


def _post(url, headers, body: dict):
    """返回 (status, text)。

    BUGS-v2.63-288 P3：出站统一走 netguard.open_checked()（目标校验 + 逐跳重定向
    复检），不再直接 urlopen。

    `limit=0` 表示不限读取长度 —— 这里不能截断：包多时响应会超过 50KB
    （实测 50 个赠送包 ≈ 90KB），截断后 json.loads 会失败，被误判成「接口异常」。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        return netguard.open_checked(req, timeout=TIMEOUT, limit=0)
    except netguard.BlockedTarget as e:
        raise RuntimeError("网络错误: %s" % e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError("网络错误: %s" % e)


def _num(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def balance(cred):
    """返回 {remaining, used_total, capacity_total, packages:[...], codes:[...]}。失败抛异常。"""
    status, text = _post(_base(cred) + "/v2/billing/meter/get-user-resource", _hdrs(cred), {})
    if status in (401, 403):
        raise PermissionError("凭据被拒绝（http %s）" % status)
    if not (200 <= status < 300):
        raise RuntimeError("余额接口 HTTP %s: %s" % (status, text[:200]))
    try:
        doc = json.loads(text)
    except Exception:
        raise RuntimeError("余额接口返回非 JSON: %s" % text[:200])

    accounts = (
        (((doc.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts")
        or (doc.get("data") or {}).get("Accounts")
        or []
    )
    remain_total = 0.0
    used_total = 0.0
    cap_total = 0.0
    packages = []
    codes = []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        rem = _num(a.get("CapacityRemainPrecise"))
        cyc = a.get("CycleCapacityRemainPrecise")
        cyc_size = _num(a.get("CycleCapacitySizePrecise"))
        cyc_val = None if cyc is None else _num(cyc)
        usable = min(rem, cyc_val) if (cyc_val is not None and cyc_size > 0) else rem
        used = _num(a.get("CapacityUsedPrecise"))
        cap = _num(a.get("CapacitySizePrecise"))
        code = str(a.get("PackageCode") if a.get("PackageCode") is not None else "")
        packages.append({
            "name": a.get("PackageName") or "",
            "code": code,
            "remain": round(usable, 2),
            "cycle_remain": None if cyc_val is None else round(cyc_val, 2),
            "total_remain": round(rem, 2),
            "used": round(used, 2),
            "capacity": round(cap, 2),
        })
        remain_total += usable
        used_total += used
        cap_total += cap
        if code and code not in codes:
            codes.append(code)

    return {
        "remaining": round(remain_total, 2),
        "used_total": round(used_total, 2),
        "capacity_total": round(cap_total, 2),
        "packages": packages,
        "codes": codes,
    }


def summary(cred):
    """官方汇总口径：GET 后端的 get-user-resource-summary。

    这个接口比我们自己累加 packages 可信得多：
      data.TotalCredits      → 官方承认的「当前可用积分」（字符串，可带小数）
      data.CreditGroups[]    → 按 Kind 分组：plan_base 套餐基础 / plan_bonus 套餐赠送
                               / addon 购买 / benefit 平台奖励（签到+任务发的都在这）
      data.Packages[]        → 按 PackageCode 聚合的总量/剩余/已用
      data.SubscriptionPackageName / IsPaidUser → 套餐名与是否付费

    实测（2026-11-20）：19 个包自己累加 min(rem,cyc) 得 1888.64，接口 TotalCredits 也是
    1888.64 —— 两者一致时优先用接口值，因为它自带分组，能拆出「签到/任务得的」与
    「套餐自带的」。
    失败抛异常，由 card() 兜住。

    地址坑（实测）：这个接口在 www.codebuddy.cn 上只认不带 /v2 的 /billing/meter/...，
    带 /v2 直接 404 Route Not Found —— 与 get-user-resource-free-packages 同一个毛病，
    所以两个都试。
    """
    base = _base(cred)
    status, text = 0, ""
    for url in (base + "/billing/meter/get-user-resource-summary",
                base + "/v2/billing/meter/get-user-resource-summary"):
        status, text = _post(url, _hdrs(cred), {})
        if 200 <= status < 300:
            break
    if status in (401, 403):
        raise PermissionError("凭据被拒绝（http %s）" % status)
    if not (200 <= status < 300):
        raise RuntimeError("汇总接口 HTTP %s: %s" % (status, text[:200]))
    try:
        doc = json.loads(text)
    except Exception:
        raise RuntimeError("汇总接口返回非 JSON: %s" % text[:200])
    data = doc.get("data") or {}

    groups = []
    for g in data.get("CreditGroups") or []:
        if not isinstance(g, dict):
            continue
        groups.append({
            "kind": g.get("Kind") or "",
            "name": g.get("Name") or "",
            "remain": round(_num(g.get("CycleRemainCapacity")), 2),
            "used": round(_num(g.get("CycleUsedCapacity")), 2),
            "capacity": round(_num(g.get("CycleTotalCapacity")), 2),
            "count": int(_num(g.get("TotalCount"))),
        })

    pkgs = []
    for p in data.get("Packages") or []:
        if not isinstance(p, dict):
            continue
        pkgs.append({
            "code": p.get("PackageCode") or "",
            "remain": round(_num(p.get("CycleRemainCapacity")), 2),
            "used": round(_num(p.get("CycleUsedCapacity")), 2),
            "capacity": round(_num(p.get("CycleTotalCapacity")), 2),
            "count": int(_num(p.get("TotalCount"))),
        })

    return {
        "total_credits": round(_num(data.get("TotalCredits")), 2),
        "plan_name": data.get("SubscriptionPackageName") or "",
        "is_paid": bool(data.get("IsPaidUser")),
        "groups": groups,
        "packages": pkgs,
    }


def _parse_end(s):
    """CycleEndTime 形如 '2026-12-31 23:59:59'（UTC+8）。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s)[:19], fmt).replace(tzinfo=TZ)
        except Exception:
            continue
    return None


def _free_urls(cred):
    """赠送包接口的候选地址。

    实测：get-user-resource 走 /v2/billing/meter，但 get-user-resource-free-packages
    在 www.codebuddy.cn 上只认不带 /v2 的 /billing/meter（带 /v2 会 404 Route Not Found）。
    两个都试，先 /v2 再退回不带 /v2，谁 200 用谁。
    """
    base = _base(cred)
    return [base + "/billing/meter/get-user-resource-free-packages",
            base + "/v2/billing/meter/get-user-resource-free-packages"]


def expiring(cred, codes):
    """按到期日汇总赠送包。无数据返回 None（失败绝不写 0）。"""
    if not codes:
        return None
    buckets = {}
    page = 1
    got = 0
    url = None
    while page <= 10:
        body = {"PackageCodes": codes, "PageNumber": page, "PageSize": 200}
        status, text = 0, ""
        for cand in ([url] if url else _free_urls(cred)):
            status, text = _post(cand, _hdrs(cred), body)
            if 200 <= status < 300:
                url = cand
                break
        if not (200 <= status < 300):
            return None
        try:
            doc = json.loads(text)
        except Exception:
            return None
        data = doc.get("data") or {}
        rows = data.get("Accounts") or []
        if not rows:
            break
        for a in rows:
            if not isinstance(a, dict):
                continue
            rem = _num(a.get("CycleCapacityRemainPrecise"))
            if rem <= 0:
                continue
            end = _parse_end(a.get("CycleEndTime"))
            if end is None:
                continue
            now = datetime.now(TZ)
            if end < now:
                continue
            key = end.strftime("%Y-%m-%d")
            buckets[key] = buckets.get(key, 0.0) + rem
        got += len(rows)
        total = data.get("TotalCount")
        if isinstance(total, int) and got >= total:
            break
        if len(rows) < 200:
            break
        page += 1

    if not buckets:
        return None

    now = datetime.now(TZ)
    d7 = sum(v for k, v in buckets.items()
             if datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=TZ) <= now + timedelta(days=7))
    d30 = sum(v for k, v in buckets.items()
              if datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=TZ) <= now + timedelta(days=30))
    first = sorted(buckets.keys())[0]
    out = {
        "next": {"amount": round(buckets[first], 2),
                 "date": datetime.strptime(first, "%Y-%m-%d").strftime("%m-%d")},
        "d7": round(d7, 2),
        "d30": round(d30, 2),
        "buckets": [{"date": k, "amount": round(buckets[k], 2)} for k in sorted(buckets.keys())],
    }
    return out


def card(wb_cfg, use_cache=True):
    """给卡片用的合并视图。任一失败只在对应字段说明，不影响另一个。"""
    cred, problems = wb.resolve_credential(wb_cfg or {})
    if cred is None:
        return {"ok": False, "pending": True,
                "error": "未配置 WorkBuddy 凭据" + ("：" + "；".join(problems[:2]) if problems else "")}

    key = "assets:" + cred.account_id()
    if use_cache:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit and time.time() - hit["ts"] < CACHE_TTL:
                return hit["value"]

    out = {}
    bal = None
    acct = cred.account_id()
    known_codes = _CODES.get(acct) or []

    # 三个接口互不依赖（余额 / 官方汇总 / 赠送包到期），全部并行。
    # 赠送包到期需要 PackageCodes：优先用上一次拿到的，没有才等 balance 回来再补一次。
    f_bal = _POOL.submit(balance, cred)
    f_sum = _POOL.submit(summary, cred)
    f_exp = _POOL.submit(expiring, cred, known_codes) if known_codes else None

    try:
        bal = f_bal.result()
        out["remaining"] = bal["remaining"]
        out["used_total"] = bal["used_total"]
        out["capacity_total"] = bal["capacity_total"]
        out["packages"] = bal["packages"]
        if bal["codes"]:
            _CODES[acct] = list(bal["codes"])
    except Exception as e:
        out["error"] = str(e)
        out["packages"] = []
    # 官方汇总接口：TotalCredits 是权威「当前可用」，还带 4 项分组
    # （套餐基础 / 套餐赠送 / 购买 / 平台奖励）。拿不到就不写，绝不编数字。
    try:
        sm = f_sum.result()
        out["official_total"] = sm["total_credits"]
        out["plan_name"] = sm["plan_name"]
        out["is_paid"] = sm["is_paid"]
        out["groups"] = sm["groups"]
        # 汇总接口能拿到就在这里覆盖：它比逐包累加更权威
        out["remaining"] = sm["total_credits"]
        out.pop("error", None)
    except Exception:
        pass
    try:
        ex = f_exp.result() if f_exp is not None else expiring(cred, (bal or {}).get("codes") or [])
        if ex is not None:
            out["expiring"] = ex
    except Exception:
        pass  # 过期信息拿不到就不写，前端不显示这一行

    out["ok"] = "error" not in out
    out["updated_at"] = int(time.time() * 1000)
    with _LOCK:
        _CACHE[key] = {"ts": time.time(), "value": out}
    return out


def cached_only(wb_cfg):
    """只读缓存，给 /api/status 用（首屏不打上游）。"""
    cred, _ = wb.resolve_credential(wb_cfg or {})
    if cred is None:
        return {"ok": False, "pending": True, "error": "未配置 WorkBuddy 凭据"}
    key = "assets:" + cred.account_id()
    with _LOCK:
        hit = _CACHE.get(key)
    if not hit:
        return {"ok": False, "pending": True}
    return hit["value"]
