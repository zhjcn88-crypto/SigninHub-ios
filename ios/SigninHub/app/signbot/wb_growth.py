"""WorkBuddy 成长中心：任务列表 / 一键完成 / 领奖 / 每日任务。

接口全部走 chat 域（copilot.tencent.com），只把「领奖」放在 web 域
（www.workbuddy.cn），因为官方前端的领奖请求带 x-client-platform: web。

事件上报走 /v2/report，body 是数组，每个元素除了业务字段还要带一整份
桌面指纹（machineId 用 sha256("machine:"+uid)[:36]）——漏了指纹服务端不认。

NOT_AUTO 里的任务需要真实人工动作（关注公众号、公益捐赠），不做自动化。
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from . import netguard
from . import workbuddy as wb

CHAT_BASE = "https://copilot.tencent.com"
WEB_BASE = "https://www.workbuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
GROWTH = "/v2/activity/growth"
TIMEOUT = 30
TZ = timezone(timedelta(hours=8))

NOT_AUTO = {"wb_wechat_oa_subscribe_task", "Expert_Philanthropy"}

STATUS_CN = {
    "claimed": "已领取",
    "completed": "可领取",
    "in_progress": "进行中",
    "accepted": "已接取",
    "not_accepted": "未接取",
}

TASK_HINT = {
    "chat_5": "发 5 次对话",
    "first_buddy": "领养第一个 Buddy",
    "template_5": "用 5 个模板各建一次任务",
    "create_canvas": "新建一个设计画布",
    "playbook_prompt": "用一次 Playbook 提示词",
    "RichMeow_Chat": "和富猫聊一次",
    "Library_read": "读一篇资料库文档",
    "Hp_Appearance": "换一次主题皮肤",
    "Buddy_App": "在 Buddy 里发现一次应用",
    "Buddy_App_QQ": "在 Buddy 里发现一次应用",
    "automation_1": "建一个自动化任务",
    "Model_chat_GLM5.2": "用 GLM-5.2 对话一次",
    "skill_1": "调用一次技能",
    "expert_5": "召唤 5 次专家",
    "Expert_team_use_3": "使用 3 次专家团",
    "Expert_lighthouse": "召唤一次灯塔专家",
    "black_cat": "夜间 23:00–08:00 和黑猫聊",
    "wb_wechat_oa_subscribe_task": "关注 WorkBuddy 公众号",
    "Expert_Philanthropy": "公益捐赠",
}

TIERS = [
    {"tier": "7d", "days": 7, "credit": 0, "energy": 2, "cards": 1, "chances": 1},
    {"tier": "14d", "days": 14, "credit": 50, "energy": 3, "cards": 1, "chances": 1},
    {"tier": "28d", "days": 28, "credit": 150, "energy": 5, "cards": 1, "chances": 1},
]

MAKEUP_MAX_PER_RUN = 1
LOTTERY_MAX_PER_RUN = 10


# ── HTTP ──────────────────────────────────────────────────────────────
def _derive_id(uid, salt):
    return hashlib.sha256(("%s:%s" % (salt, uid)).encode("utf-8")).hexdigest()[:36]


def _req(method, url, cred, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    base = {
        "Authorization": "Bearer %s" % cred.access_token,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-User-Id": cred.uid or "",
        "User-Agent": DESKTOP_UA,
    }
    if cred.domain:
        base["X-Domain"] = cred.domain
    base.update(headers or {})
    for k, v in base.items():
        if v:
            req.add_header(k, v)
    try:
        # BUGS-v2.63-288 P3：出站统一走 netguard.open_checked()（目标校验 + 逐跳
        # 重定向复检）。limit=0 表示不限长度 —— 成长中心有些响应带完整任务列表，
        # 截断会让 json.loads 失败、被当成接口异常。
        status, text = netguard.open_checked(req, timeout=TIMEOUT, limit=0)
        try:
            return status, json.loads(text)
        except Exception:
            return status, {"raw": text[:400]}
    except netguard.BlockedTarget as e:
        return -1, {"err": repr(e)}
    except Exception as e:
        return -1, {"err": repr(e)}


def _get(cred, path):
    s, b = _req("GET", CHAT_BASE + path, cred)
    if s != 200:
        raise RuntimeError("%s http=%s %s" % (path, s, str(b)[:160]))
    return b.get("data") or {}


def _post(cred, path, body=None, headers=None):
    return _req("POST", CHAT_BASE + path, cred, body if body is not None else {}, headers)


def _uuid_token(prefix="u"):
    return "%s-%s" % (prefix, uuid.uuid4())


# ── 只读 ──────────────────────────────────────────────────────────────
def list_tasks(cred):
    s, b = _req("GET", CHAT_BASE + GROWTH + "/tasks", cred)
    if s != 200:
        raise RuntimeError("tasks http=%s %s" % (s, str(b)[:160]))
    return (b.get("data") or {}).get("tasks") or []


def get_profile(cred):
    return _get(cred, GROWTH + "/profile")


def overview(wb_cfg):
    """给界面用：任务总览。"""
    cred, problems = wb.resolve_credential(wb_cfg or {})
    if cred is None:
        return {"ok": False, "error": "未配置 WorkBuddy 凭据"
                + ("：" + "；".join(problems[:2]) if problems else ""),
                "tasks": []}
    try:
        prof = get_profile(cred)
        tasks = list_tasks(cred)
    except Exception as e:
        return {"ok": False, "error": str(e), "tasks": []}

    rows = []
    claimable = 0
    done = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        code = t.get("task_code") or ""
        p = t.get("progress") or {}
        cur = int(p.get("current") or 0)
        tgt = int(p.get("target") or 0)
        ast = t.get("accept_status") or ""
        prog_done = tgt > 0 and cur >= tgt
        if ast == "claimed":
            status = "claimed"
        elif ast == "completed" or prog_done:
            status = "completed"
        elif ast == "in_progress":
            status = "in_progress"
        elif ast == "accepted":
            status = "accepted"
        else:
            status = "not_accepted"
        if status == "completed":
            claimable += 1
        if status == "claimed":
            done += 1
        rows.append({
            "code": code,
            "title": t.get("title") or code,
            "reward": int(t.get("reward_credit") or 0),
            "energy": int(t.get("reward_energy") or 0),
            "status": status,
            "status_cn": STATUS_CN.get(status, status),
            "current": cur, "target": tgt,
            "can_claim": status == "completed",
            "not_auto": code in NOT_AUTO,
            "hint": "" if status in ("claimed", "completed") else TASK_HINT.get(code, "需在客户端真实使用该功能"),
        })
    return {
        "ok": True,
        "level": int(prof.get("level") or 0),
        "completed": int(prof.get("completed") or done),
        "total": int(prof.get("total") or len(tasks)),
        "claimable": claimable,
        "tasks": rows,
    }


# ── 写操作 ────────────────────────────────────────────────────────────
def accept_tasks(cred, codes):
    if not codes:
        return None
    return _post(cred, GROWTH + "/tasks/accept", {"task_codes": codes})


# accept 接口在「任务有前置依赖没做完」时会拒，消息形如：
#   prerequisite not met: <task_code>
# 参考 L0NE-6/WorkBuddy-Daily 的做法：解析出前置任务，先把它跑一遍再重试 accept。
_PREREQ_RE = re.compile(r"prerequisite\s+not\s+met[:\s]+([A-Za-z0-9_\-]+)", re.I)


def accept_with_prereq(cred, codes, depth=2):
    """批量 accept，遇到前置依赖未满足就先补跑前置，再重试。

    返回 (accepted_codes, notes)。notes 是给人看的补充说明，可为空。
    """
    notes = []
    todo = [c for c in codes if c]
    if not todo:
        return [], notes
    for _ in range(max(1, depth + 1)):
        try:
            r = accept_tasks(cred, todo)
        except Exception as e:
            notes.append("accept 异常：%s" % e)
            return todo, notes
        text = json.dumps(r, ensure_ascii=False) if not isinstance(r, str) else r
        miss = _PREREQ_RE.findall(text or "")
        if not miss:
            return todo, notes
        # 前置任务先跑（它自己可能还有前置，递归一层）
        for pre in dict.fromkeys(miss):
            if pre in NOT_AUTO:
                notes.append("前置 %s 需真实动作，跳过" % pre)
                continue
            try:
                ok, msg = run_task(cred, pre)
                notes.append("先补前置 %s：%s" % (pre, msg))
            except Exception as e:
                notes.append("前置 %s 补跑失败：%s" % (pre, e))
            time.sleep(0.5)
        todo = [c for c in todo if c not in miss] or todo
    notes.append("accept 重试后仍有前置未满足")
    return todo, notes


def verify_claimed(cred, code, tries=3):
    """领取后回读确认：accept_status 变成 claimed 才算真到账。

    参考 L0NE-6/WorkBuddy-Daily 的「accept 双重校验」：只看 claim 响应不算数，
    必须回读成长中心的任务列表。
    """
    for i in range(max(1, tries)):
        try:
            tasks = list_tasks(cred)
        except Exception:
            return False, "回读失败"
        for t in tasks:
            if isinstance(t, dict) and t.get("task_code") == code:
                if t.get("accept_status") == "claimed":
                    return True, "已确认领取"
                p = t.get("progress") or {}
                cur = int(p.get("current") or 0)
                tgt = int(p.get("target") or 0)
                if tgt > 0 and cur < tgt:
                    return False, "进度未满（%d/%d）" % (cur, tgt)
        time.sleep(1.0 + i)
    return False, "回读仍是未领取"


def claim_task(cred, code):
    hdr = {
        "x-client-platform": "web",
        "Origin": WEB_BASE,
        "Referer": WEB_BASE + "/profile/growth-center",
        "User-Agent": WEB_UA,
    }
    s, b = _req("POST", WEB_BASE + "/activity/growth/tasks/%s/claim" % code, cred, None, hdr)
    d = (b.get("data") or {}) if isinstance(b, dict) else {}
    return {
        "status": s,
        "ok": 200 <= s < 300 and not (isinstance(b, dict) and b.get("code") not in (0, None)),
        "already": bool(d.get("already_claimed")),
        "credit": int(d.get("credit") or 0),
        "energy": int(d.get("energy") or 0),
        "raw": json.dumps(b, ensure_ascii=False)[:200],
    }


# ── 行为上报 ──────────────────────────────────────────────────────────
def _desktop_fp(cred):
    now = int(time.time() * 1000)
    return {
        "extName": "workbuddy-desktop",
        "ideName": "WorkBuddy",
        "appVersion": "5.5.6",
        "os": "Win32", "arch": "x64", "osVersion": "10.0",
        "userAgent": DESKTOP_UA,
        "machineId": _derive_id(cred.uid, "machine"),
        "userId": cred.uid,
        "locale": "zh-CN",
        "timestamp": now,
        "reportDelay": 0,
    }


def report_desktop(cred, events):
    fp = _desktop_fp(cred)
    arr = []
    for ev in events:
        m = dict(fp)
        m.update(ev)
        m.setdefault("timestamp", fp["timestamp"])
        arr.append(m)
    return _req("POST", CHAT_BASE + "/v2/report", cred, arr,
                {"User-Agent": DESKTOP_UA, "X-Product": "SaaS",
                 "X-Request-ID": _derive_id(cred.uid, "req") + str(int(time.time() * 1e6) % 1000000)})


def report_web(cred, event_code, page_url, element_id, element_name):
    ev = {
        "eventCode": event_code,
        "timestamp": int(time.time() * 1000),
        "reportDelay": 0,
        "pageURL": page_url,
        "elementId": element_id,
        "elementName": element_name,
        "os": "Win32", "arch": "", "osVersion": "10.0",
        "userAgent": WEB_UA,
        "machineId": _derive_id(cred.uid, "webmachine"),
        "userId": cred.uid,
    }
    return _req("POST", WEB_BASE + "/v2/report", cred, [ev],
                {"x-client-platform": "web", "Origin": WEB_BASE,
                 "Referer": page_url, "User-Agent": WEB_UA,
                 "Accept": "application/json"})


def _chat_event(cred, conv_id=None, model_id="deepseek-v4-flash",
                model_name="DeepSeek V4 Flash"):
    now = int(time.time() * 1000)
    cid = conv_id or ("signhub-%d" % now)
    return {
        "eventCode": "chat_request_send",
        "timestamp": now, "reportDelay": 0,
        "mode": "craft", "conversationId": cid, "requestId": cid,
        "inputLength": 12,
        "requestModelId": model_id, "requestModelName": model_name,
        "isPlan": False, "isAutoExecuteTerminal": False,
        "isAutoModify": False, "codebaseEnable": False,
        "maxToken": 0, "maxSteps": 0, "temperature": 0, "maxRetries": 0,
        "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
        "codebaseId": "", "mentionContextCount": 0, "command": "",
        "expertId": "", "recommendId": "", "skillId": "", "skillCount": 0,
        "totalCount": 0, "fileUri": "", "presentAt": now, "traceId": "",
        "rootRequestId": cid, "parentConversationID": cid,
        "agentName": "default", "agentType": "conversation",
        "userId": cred.uid,
    }


def chat_completion(cred, model_id="glm-5.2", prompt="你好"):
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 32,
    }
    s, _ = _req("POST", CHAT_BASE + "/v2/chat/completions", cred, body,
                {"Accept": "text/event-stream", "User-Agent": DESKTOP_UA})
    return 200 <= s < 300


def _run_expert(cred, cid, kind, n, expert_id=None):
    for i in range(n):
        rid = "%s-%s-%d" % (cid, kind, i)
        base = {"conversationId": cid, "requestId": rid, "userId": cred.uid}
        events = []
        if kind == "lh" and expert_id:
            events.append(dict(base, eventCode="expert_summon_click", expertId=expert_id))
            events.append(dict(base, eventCode="expert_summoned", expertId=expert_id))
        elif kind == "team":
            events.append(dict(base, eventCode="expert_team_summon_click", teamId="team-default"))
            events.append(dict(base, eventCode="expert_team_summoned", teamId="team-default"))
        else:
            events.append(dict(base, eventCode="expert_summon_click", expertId="ex-default"))
            events.append(dict(base, eventCode="expert_summoned", expertId="ex-default"))
        events.append(dict(base, eventCode="agent_task_created"))
        events.append(dict(base, eventCode="chat_message_response", isSuccessful=True))
        events.append(dict(base, eventCode="expert_actual_use", expertType=kind))
        report_desktop(cred, events)
        time.sleep(1)


def run_task(cred, code):
    """驱动一个任务并领奖。返回 (ok, msg)。"""
    if code in NOT_AUTO:
        return False, "需真实动作（%s），无法自动化" % TASK_HINT.get(code, code)
    try:
        accept_tasks(cred, [code])
        cid = "signhub-%d" % int(time.time() * 1000)

        if code == "chat_5":
            for i in range(5):
                report_desktop(cred, [_chat_event(cred, "%s-%d" % (cid, i))])
                time.sleep(1)
        elif code == "first_buddy":
            report_desktop(cred, [_chat_event(cred, cid)])
            time.sleep(2)
            _post(cred, "/activity/growth/buddy/agreement", {"agree": True})
            _post(cred, "/activity/growth/buddy/first", {})
        elif code == "template_5":
            tps = [("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"),
                   ("4", "活动策划"), ("5", "代码评审")]
            for tid, tname in tps:
                rid = cid + tid
                report_desktop(cred, [
                    {"eventCode": "agent_task_created_with_template",
                     "templateId": tid, "templateName": tname,
                     "conversationId": cid, "requestId": rid, "userId": cred.uid},
                    {"eventCode": "template_used",
                     "templateId": tid, "templateName": tname,
                     "conversationId": cid, "requestId": rid, "userId": cred.uid},
                ])
                time.sleep(0.8)
        elif code == "create_canvas":
            report_desktop(cred, [{"eventCode": "wbx_design_canvas_task_create/open",
                                   "conversationId": cid, "requestId": cid, "userId": cred.uid}])
        elif code == "playbook_prompt":
            report_desktop(cred, [
                {"eventCode": "playbook_cta_click", "conversationId": cid,
                 "requestId": cid, "userId": cred.uid},
                {"eventCode": "playbook_prompt_send", "promptId": "pm-gtm-launch-plan",
                 "promptName": "新产品上市 GTM 发布计划一页纸",
                 "conversationId": cid, "requestId": cid, "userId": cred.uid},
            ])
        elif code == "RichMeow_Chat":
            report_desktop(cred, [
                {"eventCode": "agent_task_created", "conversationId": cid,
                 "requestId": cid, "userId": cred.uid, "extName": "workbuddy-desktop"},
                {"eventCode": "chat_message_response", "conversationId": cid,
                 "requestId": cid, "userId": cred.uid, "isSuccessful": True},
            ])
        elif code == "Library_read":
            report_web(cred, "web_element_click",
                       WEB_BASE + "/space/d/o0KWYeynteVv06UnAZqIFm",
                       "library_doc_intro_click", "WorkBuddy资料库介绍")
        elif code == "Hp_Appearance":
            theme_key = "theme-tkmw7j"
            _post(cred, "/v2/user-asset/appearance/set", {"themeKey": theme_key})
            report_desktop(cred, [{
                "eventCode": "appearance_skin_apply", "action": "apply",
                "source": "settings_close", "id": theme_key,
                "vipLevel": 0, "series": "", "type": "unknown",
                "conversationId": cid, "requestId": cid, "userId": cred.uid,
            }])
        elif code in ("Buddy_App", "Buddy_App_QQ"):
            report_desktop(cred, [{"eventCode": "buddy_app_event", "stage": "discover",
                                   "appId": "cb_y5Dy46tPQGGWtueMxXbe",
                                   "conversationId": cid, "requestId": cid, "userId": cred.uid}])
        elif code == "automation_1":
            report_desktop(cred, [{"eventCode": "automated_task_create_suc",
                                   "taskName": "signhub 自动化",
                                   "conversationId": cid, "requestId": cid, "userId": cred.uid}])
        elif code == "Model_chat_GLM5.2":
            chat_completion(cred, "glm-5.2", "hi")
            report_desktop(cred, [_chat_event(cred, cid, "glm-5.2", "GLM-5.2")])
        elif code == "skill_1":
            report_desktop(cred, [
                {"eventCode": "chat_message_response", "finishReason": "tool_calls",
                 "conversationId": cid, "requestId": cid, "userId": cred.uid},
                {"eventCode": "skill_info", "id": "润泽小馆·日报撰写",
                 "skillId": "skill_2097350077599879168", "skillVersion": "1.0.0",
                 "toolStatus": "success", "fileCount": 56, "source": "workbuddy-desktop",
                 "conversationId": cid, "requestId": cid, "messageId": cid,
                 "requestModelId": "fast-model", "requestModelName": "fast-model",
                 "traceId": cid, "userId": cred.uid},
            ])
        elif code == "expert_5":
            _run_expert(cred, cid, "agent", 5)
        elif code == "Expert_team_use_3":
            _run_expert(cred, cid, "team", 3)
        elif code == "Expert_lighthouse":
            _run_expert(cred, cid, "lh", 1, "ex_2cvvUZQhDyeJ")
        elif code == "black_cat":
            h = datetime.now(TZ).hour
            if not (h >= 23 or h < 8):
                return False, "black_cat 仅在 23:00–08:00 夜间窗口可完成"
            report_desktop(cred, [_chat_event(cred, cid)])
        else:
            return False, "任务 %s 未实现驱动：需在客户端真实使用该功能" % code

        time.sleep(2)
        r = claim_task(cred, code)
        if r["ok"] and r["already"]:
            return True, "已领过（幂等）"
        if r["ok"] and (r["credit"] or r["energy"]):
            return True, "已领 +%d 分 / +%d 能量" % (r["credit"], r["energy"])
        if r["ok"]:
            return True, "已领取"
        if "not completed" in r["raw"]:
            return False, "进度未满：需先在客户端真实使用该功能"
        return False, r["raw"]
    except Exception as e:
        return False, str(e)


def run_tasks(wb_cfg):
    """一键：accept 全部 → 逐个驱动 → 领奖。"""
    cred, problems = wb.resolve_credential(wb_cfg or {})
    if cred is None:
        return {"ok": False, "error": "未配置 WorkBuddy 凭据", "results": []}
    try:
        tasks = list_tasks(cred)
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}

    codes = [t.get("task_code") for t in tasks if isinstance(t, dict) and t.get("task_code")]
    accept_notes = []
    try:
        # 带前置依赖补救：accept 被「prerequisite not met」拒时先补跑前置再重试
        _, accept_notes = accept_with_prereq(cred, codes)
    except Exception as e:
        accept_notes = ["accept 失败：%s" % e]

    results = []
    for n in accept_notes:
        results.append({"code": "-", "ok": False, "msg": n, "skipped": True})
    for t in tasks:
        if not isinstance(t, dict):
            continue
        code = t.get("task_code") or ""
        if not code:
            continue
        p = t.get("progress") or {}
        cur = int(p.get("current") or 0)
        tgt = int(p.get("target") or 0)
        ast = t.get("accept_status") or ""
        if ast == "claimed" and tgt > 0 and cur >= tgt:
            results.append({"code": code, "ok": True, "msg": "已完成已领", "skipped": True})
            continue
        if code in NOT_AUTO:
            results.append({"code": code, "ok": False,
                            "msg": TASK_HINT.get(code, "需真实动作"), "skipped": True})
            continue
        ok, msg = run_task(cred, code)
        results.append({"code": code, "ok": ok, "msg": msg, "skipped": False})
        time.sleep(0.5)
    return {"ok": True, "results": results}


def claim_all(wb_cfg):
    """只领已达标奖励，不发事件。"""
    cred, problems = wb.resolve_credential(wb_cfg or {})
    if cred is None:
        return {"ok": False, "error": "未配置 WorkBuddy 凭据", "results": []}
    try:
        tasks = list_tasks(cred)
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}

    results = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        code = t.get("task_code") or ""
        if not code:
            continue
        p = t.get("progress") or {}
        cur = int(p.get("current") or 0)
        tgt = int(p.get("target") or 0)
        ast = t.get("accept_status") or ""
        done = tgt > 0 and cur >= tgt
        if ast == "claimed":
            continue
        if not done and ast != "completed":
            continue
        r = claim_task(cred, code)
        base_msg = ("已领过" if r["already"] else
                    ("领取 +%d 分" % r["credit"] if r["credit"] else "已领取")) if r["ok"] else r["raw"]
        # 只相信回读：claim 返回 200 不代表到账，必须再查一次 accept_status
        confirmed, why = (False, "领取请求失败，未回读")
        if r["ok"]:
            confirmed, why = verify_claimed(cred, code)
            if not confirmed and r["credit"] <= 0:
                # 没回读到也没返回积分，说明确实没成
                pass
        msg = base_msg + ("｜" + why if r["ok"] else "")
        results.append({"code": code, "ok": bool(r["ok"] and confirmed) or bool(r["ok"] and r["credit"] > 0),
                        "confirmed": confirmed, "msg": msg, "skipped": False})
        time.sleep(0.3)
    return {"ok": True, "results": results}


# ── 每日任务 ──────────────────────────────────────────────────────────
def get_streak(cred):
    return _get(cred, GROWTH + "/streak")


def get_heatmap(cred):
    return _get(cred, GROWTH + "/heatmap")


def get_energy(cred):
    return _get(cred, GROWTH + "/energy")


def get_lottery(cred):
    return _get(cred, GROWTH + "/lottery/summary")


def get_buddy_quota(cred):
    return _get(cred, GROWTH + "/buddy/quota")


def get_travel_status(cred):
    return _get(cred, GROWTH + "/buddy/travel/status")


def redeem_tier(cred, tier):
    return _post(cred, GROWTH + "/redeem",
                 {"tier": tier, "client_token": _uuid_token("redeem-" + tier)})


def makeup_use(cred, target_date):
    return _post(cred, GROWTH + "/makeup-cards/use", {"target_date": target_date})


def lottery_draw(cred):
    return _post(cred, GROWTH + "/lottery/draw", {"client_token": _uuid_token("draw")})


def buddy_open(cred, count=1):
    return _post(cred, GROWTH + "/buddy/open", {"count": count})


def _makeup_balance(streak_body):
    v = streak_body.get("makeup_cards")
    if isinstance(v, dict):
        v = v.get("balance")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _makeup_candidates(streak_body, heatmap_body):
    """/streak 的 makeup_dates 是**已补登**记录，不能当待办；断点靠 heatmap score==0 反推。"""
    called = set()
    for d in (streak_body.get("streak") or {}).get("makeup_dates") or []:
        if isinstance(d, str):
            called.add(d[:10])
    launch = streak_body.get("launch_date") or ""
    start_min = None
    if launch:
        try:
            start_min = datetime.strptime(str(launch)[:10], "%Y-%m-%d").date()
        except Exception:
            start_min = None
    floor = datetime(2026, 6, 17).date()
    if start_min is None:
        start_min = floor
    else:
        start_min = max(start_min, floor)

    today = datetime.now(TZ).date()
    month_start = today.replace(day=1)
    start = max(month_start, start_min)

    cells = heatmap_body.get("cells") or []
    score = {}
    for c in cells:
        if isinstance(c, dict) and c.get("date"):
            score[str(c["date"])[:10]] = int(c.get("score") or 0)
    if not score:
        return []  # 结构异常宁可不动卡

    out = []
    d = start
    while d < today:
        key = d.strftime("%Y-%m-%d")
        if key not in called and score.get(key, 0) == 0:
            out.append(key)
        d += timedelta(days=1)
    return out


def run_daily(wb_cfg):
    """签到（先补一条对话预热）→ 连登兑换 → 补登 → 抽奖 → 开盲盒。"""
    cred, problems = wb.resolve_credential(wb_cfg or {})
    if cred is None:
        return {"ok": False, "error": "未配置 WorkBuddy 凭据", "results": []}
    results = []

    def add(code, ok, msg):
        results.append({"code": code, "ok": bool(ok), "msg": msg})

    # 1) 签到：官方要求「当天完成一次对话」才算签到，先补一条极简对话
    try:
        chat_completion(cred, "deepseek-v4-flash", "你好")
        time.sleep(1)
        st = wb.fetch_status(cred)
        if st.get("today_checked_in"):
            add("checkin", True, "今日已签到")
        else:
            r = wb.claim_daily(cred)
            add("checkin", True, "签到成功 +%s" % r.get("credit", 0))
    except Exception as e:
        add("checkin", False, str(e)[:120])

    # 2) 连登兑换
    try:
        st = get_streak(cred)
        days = int((st.get("streak") or {}).get("days") or 0)
        red = st.get("redemption_status") or {}
        for t in TIERS:
            if days < t["days"]:
                continue
            flag = red.get("tier_%s_status" % t["tier"])
            if flag in ("claimed", "redeemed", True):
                continue
            s, b = redeem_tier(cred, t["tier"])
            ok = 200 <= s < 300
            add("redeem_%s" % t["tier"], ok, ("兑换成功" if ok else str(b)[:100]))
            time.sleep(0.4)
    except Exception as e:
        add("redeem", False, str(e)[:120])

    # 3) 补登（每次最多 1 张）
    try:
        st = get_streak(cred)
        hm = get_heatmap(cred)
        cards = _makeup_balance(st)
        cands = _makeup_candidates(st, hm)
        if cards <= 0:
            add("makeup", True, "没有补登卡，跳过")
        elif not cands:
            add("makeup", True, "没有可补的断点")
        else:
            n = 0
            for d in cands[:MAKEUP_MAX_PER_RUN]:
                s, b = makeup_use(cred, d)
                ok = 200 <= s < 300
                add("makeup_%s" % d, ok, ("已补 %s" % d) if ok else str(b)[:100])
                n += 1
                time.sleep(0.4)
            if n == 0:
                add("makeup", True, "本次不补")
    except Exception as e:
        add("makeup", False, str(e)[:120])

    # 4) 抽奖
    try:
        lot = get_lottery(cred)
        chances = int(lot.get("chances") or 0)
        if chances <= 0:
            add("lottery", True, "没有抽奖次数")
        else:
            okn = 0
            for _ in range(min(chances, LOTTERY_MAX_PER_RUN)):
                s, b = lottery_draw(cred)
                if 200 <= s < 300:
                    okn += 1
                else:
                    break
                time.sleep(0.4)
            add("lottery", okn > 0, "抽了 %d 次" % okn)
    except Exception as e:
        add("lottery", False, str(e)[:120])

    # 5) 开盲盒（travel 必须 idle）
    try:
        ts = get_travel_status(cred)
        if (ts.get("state") or "") != "idle":
            add("buddy", True, "Buddy 在旅行中，跳过开盒")
        else:
            q = get_buddy_quota(cred)
            affordable = int(q.get("affordable") or 0)
            n = min(affordable, int(q.get("max_open_count") or 5))
            if n <= 0:
                add("buddy", True, "能量不足，跳过开盒")
            else:
                s, b = buddy_open(cred, n)
                add("buddy", 200 <= s < 300, "开盒 %d 个" % n)
    except Exception as e:
        add("buddy", False, str(e)[:120])

    return {"ok": True, "results": results}
