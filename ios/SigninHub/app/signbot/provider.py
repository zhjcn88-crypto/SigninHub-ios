"""Provider 执行引擎。

每个服务（WorkBuddy / Trae Solo / Qoder CN / MiniMax Code）按配置运行：
  - mode = "http"    : 用保存的 Cookie/Token 直接请求签到接口（最快最稳）
  - mode = "browser" : 用 Playwright 打开页面并点击「签到」按钮（接口变动时的兜底）

占位符（WORKBUDDY_SIGN_URL / *_COOKIE 等）未被替换时，provider 进入「演示模式(dry-run)」，
只做本地模拟、不发起真实网络请求，方便先跑通全链路。
"""
import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from . import netguard

PLACEHOLDER_RE = re.compile(r"^[A-Z0-9_]{4,}$")


def _looks_like_placeholder(value) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    if v.upper().startswith(("WORKBUDDY_", "TRAESOLO_", "APP_", "TARGET", "TOKEN")):
        return True
    # 形如 WORKBUDDY_SIGN_URL 的全大写占位
    if PLACEHOLDER_RE.match(v) and "_" in v and "://" not in v and "@" not in v:
        return True
    return False


class ProviderError(Exception):
    pass


def _replace_json_body(body, ctx: dict, day: str):
    """对 body 里所有字符串做简单模板替换。"""
    if isinstance(body, dict):
        return {k: _replace_json_body(v, ctx, day) for k, v in body.items()}
    if isinstance(body, list):
        return [_replace_json_body(v, ctx, day) for v in body]
    if isinstance(body, str):
        s = body
        for k, v in ctx.items():
            s = s.replace("{" + k + "}", str(v))
        s = s.replace("{date}", day)
        return s
    return body


def _extract(payload, expr: str):
    """支持 a.b.c 的点路径取值。"""
    cur = payload
    for part in expr.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


class BaseProvider:
    key = "base"
    label = "Base"

    def __init__(self, cfg: dict, full_cfg: dict):
        self.cfg = cfg
        self.full_cfg = full_cfg

    # --- 供子类/引擎调用 ---
    def http_config(self) -> dict:
        return self.cfg.get("http", {})

    def is_dry_run(self) -> bool:
        mode = (self.cfg.get("mode") or "http").lower()
        # 浏览器 / WorkBuddy / Trae / Qoder / MiniMax 直连都不依赖接口占位符
        if mode in ("browser", "wb", "trae", "qoder", "minimax"):
            return False
        h = self.http_config()
        return _looks_like_placeholder(h.get("url", "")) or (
            isinstance(h.get("headers", {}), dict)
            and any(_looks_like_placeholder(v) for v in h.get("headers", {}).values())
        )

    def run(self, day: str) -> dict:
        mode = (self.cfg.get("mode") or "http").lower()
        if mode == "browser":
            # 浏览器模式不使用接口占位符，直接走真实页面点击
            return self._run_browser(day)
        if mode == "wb":
            return self._run_workbuddy()
        if mode == "trae":
            return self._run_trae()
        if mode == "qoder":
            return self._run_qoder()
        if mode == "minimax":
            return self._run_minimax()
        if mode == "huawei":
            return self._run_huawei()
        if self.is_dry_run():
            return self._dry_run(day)
        return self._run_http(day)

    # --- Qoder 国内版（真签到，纯 HTTP + 本机客户端凭据）---
    def _run_qoder(self) -> dict:
        try:
            from . import qoder
        except Exception as e:
            return {"status": "fail", "http_status": None,
                    "message": f"加载 Qoder 模块失败: {e}", "points": "", "detail": ""}
        return qoder.run(self.cfg.get("qoder") or {})

    # --- MiniMax Code（真签到，纯 HTTP + 本机客户端凭据）---
    def _run_minimax(self) -> dict:
        try:
            from . import minimax
        except Exception as e:
            return {"status": "fail", "http_status": None,
                    "message": f"加载 MiniMax 模块失败: {e}", "points": "", "detail": ""}
        return minimax.run(self.cfg.get("minimax") or {})

    # --- 华为码道（真签到，纯 HTTP + 站内登录采集的 Cookie）---
    def _run_huawei(self) -> dict:
        try:
            from . import huawei
        except Exception as e:
            return {"status": "fail", "http_status": None,
                    "message": f"加载华为模块失败: {e}", "points": "", "detail": ""}
        return huawei.run(self.cfg.get("huawei") or {})

    # --- Trae Solo 官方协议直连（真签到）---
    def _run_trae(self) -> dict:
        try:
            from . import trae
        except Exception as e:
            return {"status": "fail", "http_status": None,
                    "message": f"加载 Trae 模块失败: {e}", "points": "", "detail": ""}
        return trae.run(self.cfg.get("trae") or {})

    # --- WorkBuddy 官方协议直连（真签到）---
    def _run_workbuddy(self) -> dict:
        try:
            from . import workbuddy as wb
        except Exception as e:
            return {"status": "fail", "http_status": None,
                    "message": f"加载 WorkBuddy 模块失败: {e}", "points": "", "detail": ""}
        return wb.run(self.cfg.get("wb") or {})

    # --- 演示模式：不发真实请求 ---
    def _dry_run(self, day: str) -> dict:
        return {
            "status": "success",
            "http_status": None,
            "message": "演示模式(dry-run)：未配置真实接口，本地模拟签到成功。填入真实 URL/Cookie 后即请求真实接口。",
            "points": "+10",
            "detail": json.dumps({"dry_run": True, "provider": self.key, "day": day}, ensure_ascii=False),
            "dry_run": True,
        }

    # --- HTTP 接口模式 ---
    def _run_http(self, day: str) -> dict:
        h = self.http_config()
        url = h.get("url", "")
        method = (h.get("method") or "POST").upper()
        headers = dict(h.get("headers") or {})
        body_type = (h.get("body_type") or "json").lower()
        ctx = {"date": day, "day": day}
        body_obj = _replace_json_body(h.get("body") or {}, ctx, day)

        data = None
        if method in ("POST", "PUT", "PATCH"):
            if body_type == "form":
                import urllib.parse
                data = urllib.parse.urlencode(body_obj).encode("utf-8")
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            else:
                data = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        http_status = None
        raw = ""
        try:
            # 出站目标校验：url 来自用户填的自定义 provider，必须过 netguard
            http_status, raw = netguard.open_checked(req, timeout=20)
        except netguard.BlockedTarget as e:
            return {
                "status": "fail", "http_status": None,
                "message": f"接口地址被拒绝: {e}", "points": "",
                "detail": "", "dry_run": False,
            }
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {
                "status": "fail", "http_status": None,
                "message": f"网络请求失败: {e}", "points": "",
                "detail": "", "dry_run": False,
            }
        return self._classify(http_status, raw, h, day, failed=False)

    def _classify(self, http_status, raw, h, day, failed: bool) -> dict:
        payload = None
        try:
            payload = json.loads(raw)
        except Exception:
            pass
        expr = h.get("success_expr", "code")
        want = h.get("success_value", 0)
        ok = False
        if payload is not None:
            got = _extract(payload, expr)
            ok = (got == want) or (str(got) == str(want))
        # 常见「今日已签到」判定
        already = False
        low = (raw or "").lower()
        if any(k in low for k in ("already", "重复", "已签到", "已领取")):
            already = True
        if failed:
            status = "fail"
        elif already and not ok:
            status = "already"
        elif ok:
            status = "success"
        else:
            status = "fail"
        msg = "签到成功" if status == "success" else (
            "今日已签到" if status == "already" else "签到失败（响应未命中成功条件）")
        points = ""
        if isinstance(payload, dict):
            points = str(payload.get("points") or payload.get("data", {}).get("points") or "")
        return {
            "status": status, "http_status": http_status, "message": msg,
            "points": points, "detail": raw[:3000], "dry_run": False,
        }

    # --- 浏览器点击模式（Playwright 兜底）---
    def _run_browser(self, day: str) -> dict:
        b = self.cfg.get("browser", {})
        try:
            from playwright.sync_api import sync_playwright  # 延迟导入
        except Exception as e:
            return {
                "status": "fail", "http_status": None,
                "message": "未安装 Playwright，无法使用浏览器模式。执行: pip install playwright && playwright install chromium",
                "points": "", "detail": str(e), "dry_run": False,
            }
        storage = b.get("storage_state")
        headless = b.get("headless", True)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                ctx_kwargs = {}
                if storage:
                    import os
                    from .config import DATA_DIR
                    sp = os.path.join(DATA_DIR, storage)
                    if os.path.exists(sp):
                        ctx_kwargs["storage_state"] = sp
                context = browser.new_context(**ctx_kwargs)
                page = context.new_page()
                page.goto(b.get("sign_url") or b.get("login_url"), timeout=45000)
                page.wait_for_timeout(2500)
                clicked = False
                for sel in (b.get("click_selectors") or []):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            loc.click(timeout=5000)
                            clicked = True
                            break
                    except Exception:
                        continue
                page.wait_for_timeout(1500)
                # 保存登录态，供下次复用
                if storage:
                    import os
                    from .config import DATA_DIR
                    try:
                        context.storage_state(path=os.path.join(DATA_DIR, storage))
                    except Exception:
                        pass
                browser.close()
            if clicked:
                return {"status": "success", "http_status": None,
                        "message": "浏览器模式：已点击签到按钮", "points": "",
                        "detail": "", "dry_run": False}
            return {"status": "fail", "http_status": None,
                    "message": "浏览器模式：未找到签到按钮（可能已签到或选择器需更新）",
                    "points": "", "detail": "", "dry_run": False}
        except Exception as e:
            return {"status": "fail", "http_status": None,
                    "message": f"浏览器模式异常: {e}", "points": "", "detail": "",
                    "dry_run": False}


class WorkBuddyProvider(BaseProvider):
    key = "workbuddy"
    label = "WorkBuddy"


class TraeSoloProvider(BaseProvider):
    key = "traesolo"
    label = "Trae Solo"


class QoderProvider(BaseProvider):
    key = "qoder"
    label = "Qoder CN"


class MiniMaxProvider(BaseProvider):
    key = "minimax"
    label = "MiniMax Code"


class HuaweiProvider(BaseProvider):
    key = "huawei"
    label = "华为码道"


PROVIDER_CLASSES = {
    "workbuddy": WorkBuddyProvider,
    "traesolo": TraeSoloProvider,
    "qoder": QoderProvider,
    "minimax": MiniMaxProvider,
    "huawei": HuaweiProvider,
}


def build_providers(cfg: dict) -> dict:
    out = {}
    for key, cls in PROVIDER_CLASSES.items():
        pcfg = (cfg.get("providers") or {}).get(key)
        if pcfg is None:
            continue
        out[key] = cls(pcfg, cfg)
    return out


def readiness(key: str, pcfg: dict, data_dir: str = None, light: bool = False) -> dict:
    """判断某服务当前是否具备「真签到」条件。界面与 CLI 体检共用同一口径。

    light=True 时 qoder / minimax 只查本地凭据、不打上游（/api/status 的下拉刷新路径用）。
    这两个 provider 的 probe() 各要一次真实网络请求（实测 qoder 253ms、minimax 239ms），
    而 /api/status 每秒都可能被调，没必要为了「就绪」这一行反复打上游。
    """
    import os
    from .config import DATA_DIR
    data_dir = data_dir or str(DATA_DIR)
    mode = (pcfg.get("mode") or "http").lower()
    issues = []
    if mode == "trae":
        try:
            from . import trae as tj
            p = tj.probe(pcfg.get("trae") or {})
        except Exception as e:
            return {"mode": mode, "ready": False,
                    "issues": [f"Trae 模块不可用: {e}"], "state_exists": False}
        if not p["token_ok"]:
            issues.append("缺少 token：点上面的「登录主账号」，或粘贴 Cloud-IDE-Token")
        if not p["device_id"]:
            issues.append("缺少 x-device-id：需本机登录过 Trae 客户端（读 storage.json）")
        return {"mode": mode, "ready": not issues, "issues": issues,
                "state_exists": bool(p["token_ok"] and p["device_id"])}

    if mode == "wb":
        # WorkBuddy 官方协议直连：有 token（配置或本机发现）即可
        wb_cfg = pcfg.get("wb") or {}
        token = (wb_cfg.get("accessToken") or "").strip()
        if token:
            return {"mode": mode, "ready": True, "issues": [],
                    "state_exists": True, "account_source": "config"}
        try:
            from . import workbuddy as wb  # noqa: F401
            creds, problems = wb.discover_credentials()
        except Exception as e:
            return {"mode": mode, "ready": False, "issues": [f"WorkBuddy 模块不可用: {e}"],
                    "state_exists": False}
        want = (wb_cfg.get("region") or "").strip().lower()
        if want in ("cn", "global"):
            creds = [c for c in creds if wb.region_of(c.domain) == want]
        if not creds:
            issues.append("没有可用 WorkBuddy 凭据：请在设置里填 accessToken，或登录 WorkBuddy 桌面端")
            for p in problems[:2]:
                issues.append(p)
            return {"mode": mode, "ready": False, "issues": issues, "state_exists": False}
        return {"mode": mode, "ready": True, "issues": problems[:2],
                "state_exists": True, "account_source": creds[0].source}

    if mode == "qoder":
        try:
            from . import qoder as qj
            p = qj.quick(pcfg.get("qoder") or {}) if light else qj.probe(pcfg.get("qoder") or {})
        except Exception as e:
            return {"mode": mode, "ready": False,
                    "issues": [f"Qoder 模块不可用: {e}"], "state_exists": False}
        if not p.get("token_ok"):
            issues.append("缺少凭据：填 dt- 的 accessToken 或 drt- 的 refreshToken"
                          "（手机上推荐只填 refreshToken，能自动续期一年）")
        # 设备指纹不是鉴权项：实测不带 Cosy-MachineId、uid 乱填都返回 200，
        # 所以只当提示，不参与 ready 判定（否则手机上永远「未就绪」）。
        hints = []
        if p.get("token_ok") and not p.get("machine_id"):
            hints.append("未填设备指纹（选填，不影响签到）")
        return {"mode": mode, "ready": not issues, "issues": issues, "hints": hints,
                "state_exists": bool(p.get("token_ok")), "account_source": p.get("source")}

    if mode == "minimax":
        try:
            from . import minimax as mm
            p = mm.quick(pcfg.get("minimax") or {}) if light else mm.probe(pcfg.get("minimax") or {})
        except Exception as e:
            return {"mode": mode, "ready": False,
                    "issues": [f"MiniMax 模块不可用: {e}"], "state_exists": False}
        if not p.get("token_ok"):
            issues.append("缺少 accessToken：填网页端 localStorage['_token'] 的值，或在本机登录 MiniMax Code 后点「读取本机凭据」")
        # user_id / device_id 同样不参与鉴权（实测 user_id=0 也 200）
        hints = []
        if p.get("token_ok") and not p.get("user_id"):
            hints.append("未填 user_id（选填，不影响签到）")
        return {"mode": mode, "ready": not issues, "issues": issues, "hints": hints,
                "state_exists": bool(p.get("token_ok")), "account_source": p.get("source")}

    if mode == "huawei":
        try:
            from . import huawei as hw
            p = hw.quick(pcfg.get("huawei") or {}) if light else hw.probe(pcfg.get("huawei") or {})
        except Exception as e:
            return {"mode": mode, "ready": False,
                    "issues": [f"华为码道模块不可用: {e}"], "state_exists": False}
        if not p.get("token_ok"):
            issues.append("缺少华为会话 Cookie：点「登录华为码道」在站内登录一次即可自动采集")
        hints = []
        if p.get("token_ok") and not p.get("has_cftk"):
            hints.append("Cookie 里没有 devclouddevuibjtcftk（CSRF 令牌），领取会被拒 —— 建议重新登录一次")
        # 删掉过时的「未配置码道门户 Cookie」提示（BUGS-v2.48 P3-4）：
        # huawei.py 里 portal_ok 已重新定义成「会话可用」（quick() 的 "portal_ok": ok、
        # probe() 的 "portal_ok": has_session(cookie)），而进入这条 if 的前提是 token_ok 为真
        # —— token_ok 与 portal_ok 现在是同一个 has_session 的返回值，条件永远为假。
        # 门户 Cookie 的兜底路径仍保留在 hwCreditPortal 里，不需要再提示用户去配。
        return {"mode": mode, "ready": not issues, "issues": issues, "hints": hints,
                "state_exists": bool(p.get("token_ok")), "account_source": p.get("source")}

    if mode == "browser":
        b = pcfg.get("browser", {}) or {}
        sign_url = (b.get("sign_url") or "").strip()
        if not sign_url or _looks_like_placeholder(sign_url):
            issues.append("sign_url 未填写真实签到页地址")
        state_name = b.get("storage_state") or f"{key}_state.json"
        state_exists = os.path.exists(os.path.join(data_dir, state_name))
        if not state_exists:
            issues.append(f"缺少登录态，需运行: python tools/login.py {key}")
        try:
            import playwright  # noqa: F401
            has_pw = True
        except Exception:
            has_pw = False
        if not has_pw:
            issues.append("未安装 Playwright（pip install playwright && playwright install chromium）")
        return {"mode": mode, "ready": not issues, "issues": issues, "state_exists": state_exists}

    h = pcfg.get("http", {}) or {}
    if _looks_like_placeholder(h.get("url", "")):
        issues.append("签到接口 url 仍是占位符")
    for k, v in (h.get("headers") or {}).items():
        if _looks_like_placeholder(v):
            issues.append(f"请求头 {k} 仍是占位符（如 Cookie/Authorization）")
    return {"mode": mode, "ready": not issues, "issues": issues, "state_exists": None}
