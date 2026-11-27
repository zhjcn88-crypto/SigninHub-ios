"""签到编排：执行单个/全部 provider，写入日志并推送通知。"""
import json
import re
import threading
import time
from datetime import datetime

from . import db, notify
from .config import load_config
from .provider import build_providers

_RUN_LOCK = threading.Lock()

# 单家签到的「同键互斥 + 结果复用」。
# run_all 以前只锁住整轮（_RUN_LOCK），run_one（/api/checkin/<key>、/api/test/<key>）
# 完全不设防：手动点单家签到撞上定时任务时，同一个 provider 会被打两遍上游接口，
# 容易触发风控（重复请求本身就会被打成异常流量），db.add_sign_log 也会并发写同一行。
# 这里按 provider key 各给一把锁，并用 _INFLIGHT 让后到的那次直接复用前一次的结果，
# 既不重复发请求，也不漏签。
_KEY_LOCKS: dict = {}
_KEY_LOCKS_GUARD = threading.Lock()

# key -> (完成时间戳, 结果)。只缓存「成功/已签到」这类幂等结果，
# 失败不缓存 —— 否则用户点了失败想立刻重试会被挡回来。
_RECENT: dict = {}
_RECENT_GUARD = threading.Lock()
RECENT_TTL = 8.0

STATUS_TEXT = {"success": "成功", "already": "已签到", "fail": "失败"}
EMOJI = {"success": "✅", "already": "🟡", "fail": "❌"}


def _key_lock(key: str) -> threading.Lock:
    """每个 provider 一把锁：同一家永远串行，不同家互不影响。"""
    with _KEY_LOCKS_GUARD:
        lk = _KEY_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _KEY_LOCKS[key] = lk
        return lk


def _run_provider_guarded(key: str, prov, cfg: dict) -> dict:
    """带互斥 + 去重的单家签到。

    run_all 与 run_one（/api/checkin/<key>、/api/test/<key>）都走这里：
    - 同一 provider 同时只允许一个在跑（key 锁）；
    - 刚签完（RECENT_TTL 内）且结果是 success/already 时直接复用，不再打上游；
    - 失败结果不缓存，允许立刻重试。
    """
    with _key_lock(key):
        now = time.time()
        with _RECENT_GUARD:
            hit = _RECENT.get(key)
        if hit and now - hit[0] <= RECENT_TTL and hit[1].get("status") in ("success", "already"):
            r = dict(hit[1])
            r["deduped"] = True
            r["message"] = (r.get("message") or "") + "（同一服务刚刚签过，已复用结果）"
            return r
        res = _run_provider(key, prov, cfg)
        with _RECENT_GUARD:
            if res.get("status") in ("success", "already"):
                _RECENT[key] = (time.time(), dict(res))
            else:
                _RECENT.pop(key, None)
        return res


def _as_int(v, default: int = -1) -> int:
    """把 provider 返回的 total_points / streak_days 安全转成 int。

    qoder 返回的是 "100 Credits"、minimax 是 "连签 1 天" 这类带单位的字符串，
    直接 int() 会抛 ValueError，把一次成功的签到记成「执行异常」。
    这里取开头那段数字；取不到就返回 default（-1 表示「这次没读到」，库里的旧值保留）。
    """
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    if not m:
        return default
    try:
        return int(float(m.group(0)))
    except (TypeError, ValueError):
        return default


def _run_provider(key: str, prov, cfg: dict) -> dict:
    """执行单个 provider 并落库。cfg / prov 由调用方给好，避免每个线程重复读配置。"""
    day = db.today_str(cfg["scheduler"].get("timezone_offset_hours", 8))
    started = time.time()
    try:
        res = prov.run(day)
    except Exception as e:  # 兜底，不让单个服务崩掉整轮
        res = {"status": "fail", "http_status": None, "message": f"执行异常: {e}",
               "points": "", "detail": "", "dry_run": False}
    elapsed = round(time.time() - started, 2)
    # total_points = -2 是 provider 侧的哨兵：「这个服务没有积分概念」（如 MiniMax 只有连签）。
    # 落库时清旧值，回传给前端时统一成 -1（「没读到」），别让 -2 漏出去。
    raw_total = _as_int(res.get("total_points", -1))
    # 一律用 .get() 取：provider 返回的 dict 由各模块自己拼，字段名漏一个就 KeyError，
    # 会把「上游其实成功了」记成「执行异常」（provider 空壳、将来新增 provider、第三方改写都会踩）。
    status = res.get("status") or "fail"
    if status not in STATUS_TEXT:
        status = "fail"
    message = res.get("message") or ""
    db.add_sign_log(key, day, status, res.get("http_status"),
                    message, res.get("points", ""), res.get("detail", ""),
                    raw_total,
                    _as_int(res.get("streak_days", -1)),
                    _as_int(res.get("used_points", -1)))
    res["status"] = status
    res["message"] = message
    if raw_total == -2:
        res["total_points"] = -1
    res["provider"] = key
    # label 用 getattr 兜底：provider 对象由 build_providers 构造，正常都有 label，
    # 但单测/第三方实现的 provider 可能没有 —— 少一个展示字段不该让整轮签到报错。
    res["label"] = getattr(prov, "label", None) or key
    res["day"] = day
    res["elapsed"] = elapsed
    res["time"] = datetime.now().isoformat(timespec="seconds")
    return res


def run_one(key: str) -> dict:
    """单家签到（/api/checkin/<key>、/api/test/<key>）。

    必须走 _run_provider_guarded：以前这里直接 _run_provider，手动点单家撞上
    定时任务/一键签到时同一 provider 会被打两遍上游，触发风控并并发写同一行日志。
    """
    cfg = load_config()
    providers = build_providers(cfg)
    if key not in providers:
        raise KeyError(f"unknown provider: {key}")
    # 已下线的服务不该被打（BUGS-v2.63-290 P3-3）；server 侧也会先拦一次并给出明确文案
    if key not in active_keys(cfg):
        raise KeyError(f"retired provider: {key}")
    return _run_provider_guarded(key, providers[key], cfg)


def run_test(key: str) -> dict:
    """「试签到」（/api/test/<key>）：真打上游、不落库。

    同样要拿 key 锁 —— 它跟真实签到打的是同一个上游接口，并发就是风控源头。
    但**不读也不写 _RECENT**：测试的目的是验证凭据当前是否有效，复用旧结果会骗人；
    也不能把「没落库的结果」写进缓存，否则紧接着的真实签到会被去重掉、变成漏签。
    """
    cfg = load_config()
    providers = build_providers(cfg)
    if key not in providers:
        raise KeyError(f"unknown provider: {key}")
    # 与 run_one 同构（BUGS-v2.63-291 P3-1）：本函数注释声称「四个入口统一」，
    # 之前只有 server 层拦了 retired，任何新调用点（CLI / 未来路由）都能打已下线服务。
    if key not in active_keys(cfg):
        raise KeyError(f"retired provider: {key}")
    prov = providers[key]
    day = db.today_str(cfg["scheduler"].get("timezone_offset_hours", 8))
    with _key_lock(key):
        started = time.time()
        try:
            res = prov.run(day)
        except Exception as e:
            res = {"status": "fail", "http_status": None,
                   "message": f"执行异常: {e}", "points": "", "detail": ""}
        res["provider"] = key
        res["label"] = prov.label
        res["day"] = day
        res["elapsed"] = round(time.time() - started, 2)
        res["logged"] = False
        return res


def expiring_line(wb_cfg):
    """积分过期预警的一行文案。拿不到就返回 None —— 绝不臆测成 0。"""
    try:
        from . import wb_assets
        card = wb_assets.card(wb_cfg or {})
    except Exception:
        return None
    if not isinstance(card, dict) or not card.get("ok"):
        return None
    ex = card.get("expiring") or {}
    d7 = float(ex.get("d7") or 0)
    d30 = float(ex.get("d30") or 0)
    nxt = ex.get("next") or {}
    if d7 > 0 and nxt:
        return f"⏳ 7 天内将过期 {_fmt(d7)} 积分（最近 {nxt.get('date','')} 到期 {_fmt(float(nxt.get('amount') or 0))}）"
    if d30 > 0:
        return f"⏳ 30 天内将过期 {_fmt(d30)} 积分"
    return None


def _fmt(v):
    try:
        return str(int(v)) if float(v).is_integer() else f"{float(v):.1f}"
    except (TypeError, ValueError):
        return str(v)


def active_keys(cfg: dict) -> set:
    """该不该参与签到：`enabled` 为真 **且** 没标 `retired`。

    BUGS-v2.63-290 P3-3：`retired` 原先只有 /api/status、/api/test、all_done_today
    三处认，run_all 与 run_one 不认 —— 界面上看不到的服务，照样被「一键全部签到」
    和定时任务打一遍上游，失败记录进库，污染 /api/stats 的成功率与连续天数判定。
    现在四个入口统一走这一个函数，避免口径再漂。
    """
    out = set()
    for k, v in (cfg.get("providers") or {}).items():
        v = v or {}
        if v.get("enabled", True) and not v.get("retired"):
            out.add(k)
    return out


def all_done_today() -> dict:
    """今天是不是「所有已就绪的服务都已签完」。

    给「一键全部签到」做短路（用户 m09631）：全签完之后再点，只提示
    「你今天已经完成全部签到啦！」，不再重复打上游接口。

    判据与 /api/status 完全一致，避免「界面显示已签、这里判定没签」：
    - 只算 enabled 且没下线的 provider（带 retired 标记的不算）；
    - 只算 readiness().ready 为真的 —— 未配置的服务不参与，
      否则一个没填凭据的服务会让「今天已完成」永远不成立；
    - 当天记录 status 是 success / already 才算这一家完成。
    """
    from .config import DATA_DIR
    from .provider import readiness

    cfg = load_config()
    providers = build_providers(cfg)
    off = cfg["scheduler"].get("timezone_offset_hours", 8)
    day = db.today_str(off)
    summary = db.day_summary(day)
    enabled_keys = active_keys(cfg)
    active = 0
    pending = []
    for key, p in providers.items():
        if key not in enabled_keys:
            continue
        pcfg = cfg["providers"].get(key) or {}
        try:
            # light=True：不为了判「签没签完」去打上游体检（qoder/minimax 各要 250ms）
            rd = readiness(key, pcfg, str(DATA_DIR), light=True)
        except Exception:
            rd = {"ready": False}
        if not rd.get("ready"):
            continue
        active += 1
        rec = summary.get(key)
        if not rec or rec.get("status") not in ("success", "already"):
            pending.append(p.label)
    return {"day": day, "done": active > 0 and not pending,
            "active": active, "pending": pending}


def run_all(trigger: str = "manual") -> dict:
    # 锁只包「真正签到」这一段。通知拼装（expiring_line 会打上游拿过期积分）
    # 以前也在锁内，等于整轮签到期间多占住 _RUN_LOCK 数百毫秒到数秒，
    # 期间任何 /api/checkin/<key> 都要排队。
    with _RUN_LOCK:
        cfg = load_config()
        providers = build_providers(cfg)
        # 手动「一键全部签到」：今天已经全签完就直接回提示，不再打一遍上游。
        # 定时任务（trigger 不是 manual）不走这条 —— 用户中途加了新账号时，
        # 定时任务仍应把新账号补签掉。
        if trigger == "manual":
            chk = all_done_today()
            if chk["done"]:
                msg = "你今天已经完成全部签到啦！"
                return {"trigger": trigger, "skipped": True, "done": True,
                        "message": msg, "results": {}, "summary": msg}
        # 统一口径（BUGS-v2.63-290 P3-3）：enabled 且没标 retired，见 active_keys
        ok_keys = active_keys(cfg)
        keys = [k for k in providers if k in ok_keys]
        results = {}
        if keys:
            # 各家签到互不依赖，并行发出去；总耗时 ≈ 最慢那家而不是各家之和。
            # 单家 1~3 秒，四家串行就是「点了半天没反应」，并行后快一个数量级。
            # db.add_sign_log 内部有全局锁，多线程写 SQLite 是安全的。
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(6, len(keys)),
                                    thread_name_prefix="checkin") as pool:
                futs = {k: pool.submit(_run_provider_guarded, k, providers[k], cfg) for k in keys}
                for k in keys:                     # 按 providers 原顺序收集，输出稳定
                    try:
                        results[k] = futs[k].result()
                    except Exception as e:         # 兜底：单个服务崩掉不影响其它家
                        results[k] = {"provider": k, "label": getattr(providers[k], "label", None) or k,
                                      "status": "fail", "http_status": None,
                                      "message": f"执行异常: {e}", "points": "",
                                      "detail": "", "dry_run": False}
    # ↓ 以下都不需要 _RUN_LOCK：纯展示 + 推送，慢的话不该拖住别人的签到
    lines = []
    for key, r in results.items():
        st = r.get("status") or "fail"
        lines.append(f"{EMOJI.get(st,'·')} {r.get('label') or key}：{STATUS_TEXT.get(st, st)}"
                     + (f"（{r['points']}）" if r.get('points') else "")
                     + ("" if st != "fail" else f" - {r.get('message') or ''}"))
    extra = expiring_line((cfg.get("providers", {}).get("workbuddy", {}) or {}).get("wb") or {})
    if extra:
        lines.append("")
        lines.append(extra)
    title = f"签到日报 · {db.today_str(cfg['scheduler'].get('timezone_offset_hours',8))} [{trigger}]"
    body = "\n".join(lines) if lines else "没有启用的服务"
    notify.push(cfg, title, body)
    return {"trigger": trigger, "results": results, "summary": body}


def heartbeat() -> dict:
    """仅检查一次配置是否就绪，不算签到。"""
    cfg = load_config()
    providers = build_providers(cfg)
    return {
        "providers": list(providers.keys()),
        "dry_run": {k: p.is_dry_run() for k, p in providers.items()},
    }
