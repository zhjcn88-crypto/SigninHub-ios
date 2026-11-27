"""配置加载与持久化。使用 stdlib，仅依赖 config.json。"""
import json
import os
import threading
from pathlib import Path

# config.json 是「读-改-写」共享文件：token 续期与用户在设置页保存会并发，
# 没有锁时后写的那次会把对方刚改的字段整段覆盖回去。锁必须包住「读」。
_SAVE_LOCK = threading.RLock()

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = Path(os.environ.get("SIGNBOT_DATA", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def harden_perm(path, mode: int) -> None:
    """
    把 path 的权限收紧到 mode（POSIX）。Windows 上 chmod 只能切只读位，
    这里直接跳过 —— 那边靠 ACL 与 DPAPI，不靠位掩码。

    BUGS-v2.61 的 H3：config.json 里放的是加密后的凭据，但**密钥文件**
    （vault.key）与 SQLite 库仍是敏感物，权限必须是 0600 / 0700。
    原来的代码全程没有 chmod，实测落盘是 666。
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def tighten_data_dir() -> None:
    """数据目录 0700：别的本地用户不该能列目录、更不该能读里面的文件。"""
    harden_perm(DATA_DIR, 0o700)

CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "signbot.db"

DEFAULT_CONFIG = {
    "server": {
        # 手机 App 要能连，所以默认仍然监听全网卡（0.0.0.0）。
        # H4（BUGS-v2.61）：这条的安全性不再靠「空口令只读放行」撑着 ——
        # 后端启动时若发现 api_token 为空，会**自动生成**一个 24 字节随机口令
        # 写进 config.json 并在启动横幅里打印一次（见 server.serve）。
        # 想要「局域网免口令」必须自己显式把 api_token 设成空串并重启。
        "host": "0.0.0.0",
        "port": 8787,
        # 手机 App 访问后端所用令牌；为空时启动会自动生成
        "api_token": "",
        # 局域网控制台（BUGS-v2.40 P0）：默认只允许「本机回环 / App 自带页面 /
        # 本机自己的局域网 IP + 本服务端口」作为浏览器来源。要让别的机器上的
        # 浏览器也能读写控制台，必须显式打开并把它的完整 Origin 列进 lan_origins。
        "allow_lan_console": False,
        "lan_origins": [],
    },
    "scheduler": {
        "enabled": True,
        # 每天几点执行（24 小时制），支持多个时间点
        "times": ["08:30"],
        "timezone_offset_hours": 8,
        # 随机浮动上限（分钟）：实际执行 = 目标时间 + random(0..jitter_min)，0 = 准点
        "jitter_min": 30,
        # 启动后是否立刻跑一次
        "run_on_start": False,
    },
    "notify": {
        "enabled": True,
        # 通用 webhook：钉钉/企业微信/飞书/PushPlus/Server酱/自建，按 kind 或 host 适配
        "kind": "wecom",  # wecom | dingtalk | feishu | pushplus | serverchan | generic
        "webhook_url": "",
        # 仅钉钉「加签」需要（SEC 开头）；没开加签留空
        "secret": "",
    },
    "providers": {
        "workbuddy": {
            "enabled": True,
            # wb = 使用 WorkBuddy 官方协议直连（真签到，推荐）
            "mode": "wb",
            "wb": {
                # cn | global；留空表示自动（按凭据 domain 判断）
                "region": "cn",
                # 留空则自动从本机 DSH vault / WorkBuddy 桌面端发现
                "accessToken": "",
                "refreshToken": "",
                "uid": "",
                "uin": "",
                "enterpriseId": "",
                "domain": "",
            },
            "http": {
                "method": "POST",
                "url": "https://www.codebuddy.cn/v2/billing/meter/daily-checkin",
                "headers": {"Content-Type": "application/json", "Authorization": "WORKBUDDY_TOKEN"},
                "body": {},
                "body_type": "json",
                "success_expr": "code",
                "success_value": 0,
            },
            "browser": {
                "login_url": "WORKBUDDY_LOGIN_URL",
                "sign_url": "WORKBUDDY_SIGN_URL",
                "click_selectors": ["text=签到", "text=打卡"],
                "headless": True,
                "storage_state": "workbuddy_state.json",
            },
        },
        "traesolo": {
            "enabled": True,
            # trae = 使用 Trae Solo 官方协议直连（/trae/api/v2/ug/checkin_credits/*）
            "mode": "trae",
            "trae": {
                # cn | global（决定默认 host）
                "region": "cn",
                # 留空用默认 https://api.trae.cn
                "host": "",
                # Authorization: Cloud-IDE-JWT <token>，扫码登录后自动获取
                "token": "",
                # 16 位数字 Aha 设备号，留空自动从客户端 storage.json 读
                "deviceId": "",
                # 备用：X-Cloudide-Session cookie（可换新 token）
                "session": "",
            },
            "http": {
                "method": "POST",
                "url": "TRAESOLO_SIGN_URL",
                "headers": {"Content-Type": "application/json", "Cookie": "TRAESOLO_COOKIE"},
                "body": {"task": "checkin"},
                "body_type": "json",
                "success_expr": "code",
                "success_value": 0,
            },
            "browser": {
                "login_url": "TRAESOLO_LOGIN_URL",
                "sign_url": "TRAESOLO_SIGN_URL",
                "click_selectors": ["text=签到", "text=打卡"],
                "headless": True,
                "storage_state": "traesolo_state.json",
            },
        },
        # ── 2026-11-20 新增：两个真 AI Agent 签到（纯 HTTP 直连，凭据读本机客户端）──
        # qoder = Qoder 国内版（阿里）每日 100 Credits
        #   协议：GET  /sash/api/v1/me/campaigns?clientType=10 → campaigns[] 里 actionType=CLAIM_BENEFIT
        #        POST /sash/api/v1/me/campaigns/{campaignId}/claim
        #   凭据：%APPDATA%\com.qodercn.app.stable\auth.v1.dat
        #        （v10 前缀 = AES-256-GCM，密钥在 Local State 的 os_crypt.encrypted_key，经 DPAPI 解开）
        "qoder": {
            "enabled": False,
            "mode": "qoder",
            "qoder": {
                # cn = openapi.qoder.com.cn / intl = openapi.qoder.sh
                "region": "cn",
                # dt- 开头（accessToken），留空则自动从本机客户端读
                "token": "",
                # drt- 开头，token 过期时用
                "refreshToken": "",
                # 用户 id，留空自动从 campaigns 响应取
                "uid": "",
                # 设备指纹（auth.machine-id），留空自动读
                "machineId": "",
                "proxy": "",
            },
            "http": {
                "method": "GET",
                "url": "https://openapi.qoder.com.cn/sash/api/v1/me/campaigns?clientType=10",
                "headers": {"Authorization": "Bearer QODER_TOKEN", "Cosy-ClientType": "10"},
                "body": {},
                "body_type": "json",
                "success_expr": "claimable",
                "success_value": True,
            },
            "browser": {
                "login_url": "https://qoder.cn/device/selectAccounts",
                "sign_url": "https://qoder.cn",
                "click_selectors": ["text=领取", "text=Claim"],
                "headless": True,
                "storage_state": "qoder_state.json",
            },
        },
        # minimax = MiniMax Code（MiniMax Agent 桌面版）每日签到
        #   协议：GET  /minimax-cloud/api/v1/signin/status
        #        POST /minimax-cloud/api/v1/signin/claim  body {}
        #   签名：x-signature = md5(秒级时间戳 + SALT + body)
        #        yy          = md5(encodeURIComponent(path+query) + '_' + body + md5(毫秒时间戳) + 'ooui')
        #   凭据：%USERPROFILE%\.minimax\auth\prod\cn\mcode-public\auth.json（明文 JSON）
        "minimax": {
            "enabled": False,
            "mode": "minimax",
            "minimax": {
                # mmoat_ 开头
                "accessToken": "",
                "refreshToken": "",
                # realUserID，形如 580084210824519685
                "userId": "",
                # 客户端设备号
                "deviceId": "",
                # 请求 uuid（随便一个 uuid4 即可，留空自动生成并持久化）
                "uuid": "",
                "proxy": "",
            },
            "http": {
                "method": "POST",
                "url": "https://agent.minimax.cn/minimax-cloud/api/v1/signin/claim",
                "headers": {"Authorization": "Bearer MINIMAX_TOKEN"},
                "body": {},
                "body_type": "json",
                "success_expr": "base_resp.status_code",
                "success_value": 0,
            },
            "browser": {
                "login_url": "https://agent.minimax.cn/",
                "sign_url": "https://agent.minimax.cn/",
                "click_selectors": ["text=签到", "text=Check in"],
                "headless": True,
                "storage_state": "minimax_state.json",
            },
        },
        # huawei = 华为码道（DevCloud PromptCenter）每日签到
        #   协议：GET  /chat/PromptCenterService/v1/ops/delivery?channel=WEB
        #        POST /v1/ops/claim    {"campaignId":cid,"idempotentKey":"claim_<cid>_<ts>","channel":"WEB"}
        #        POST /v1/ops/confirm  {"campaignId":cid}   ← 少这步不算签完
        #   鉴权：Cookie 含 devclouddevuibjJ_SESSION_ID；cftk 头取自 cookie devclouddevuibjtcftk
        #   余额：codearts.huaweicloud.com/portal/snap-manager/v1/package/overview（另一份 cookie）
        "huawei": {
            # enabled 恒 true（= 参不参与签到），凭据齐不齐由 readiness 的 ready 表达。
            # 写成 False 会让卡片显示「已禁用」且点不开 → 填不了凭据的死锁。
            "enabled": True,
            "mode": "huawei",
            "huawei": {
                # 整串 Cookie（登录 devcloud.cn-north-4.huaweicloud.com 后采集）
                "cookie": "",
                # 码道门户 Cookie（codearts.huaweicloud.com），仅用于读积分余额
                "portalCookie": "",
                "proxy": "",
            },
            "http": {
                "method": "GET",
                "url": "https://devcloud.cn-north-4.huaweicloud.com/chat/PromptCenterService/v1/ops/delivery?channel=WEB",
                "headers": {"Cookie": "HUAWEI_COOKIE"},
                "body": {},
                "body_type": "json",
                "success_expr": "code",
                "success_value": 0,
            },
            "browser": {
                "login_url": "https://devcloud.cn-north-4.huaweicloud.com/chat/home",
                "sign_url": "https://devcloud.cn-north-4.huaweicloud.com/chat/home",
                "click_selectors": ["text=领取", "text=签到"],
                "headless": True,
                "storage_state": "huawei_state.json",
            },
        },
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # utf-8-sig 兼容 Windows 记事本等保存带 BOM 的 JSON
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        try:
            user = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"[配置错误] {CONFIG_PATH} 不是合法 JSON：{e}\n"
                f"请检查逗号/引号是否配对，或删掉该文件让它重新生成默认配置。"
            )
    # P2-3（BUGS-v2.25）：磁盘上的 token / refreshToken / session 是 DPAPI 密文，
    # 读出来立刻解密成明文，让所有调用方（provider / trae / qoder / minimax /
    # server 的 _masked_config）都不需要知道加密的存在。
    try:
        from . import vault
        vault.decrypt_config(user)
    except Exception:
        # 解密模块本身出问题不能让整个后端起不来 —— 明文老配置也要能继续用
        pass
    return _merge(DEFAULT_CONFIG, user)


def save_config(cfg: dict) -> None:
    with _SAVE_LOCK:
        _save_locked(cfg)


def _save_locked(cfg: dict) -> None:
    # P2-3（BUGS-v2.25）：落盘前把凭据字段加密。这里必须深拷贝 ——
    # 调用方传进来的 cfg 还要继续在内存里当明文用（下一次 load 才解密，
    # 但同一个对象在 update_config 的 mutator 里也还在被读写）。
    disk = json.loads(json.dumps(cfg))
    try:
        from . import vault
        # P1-1（BUGS-v2.40）：DPAPI 解不开的字段不能写成空串 —— 那会把原密文
        # 永久抹掉。先把「解密失败 + 内存空串」的项还原成磁盘上的原密文，
        # 再统一加密（protect 对 `dpapi:` 前缀幂等，不会二次加密）。
        vault.restore_failed(disk)
        vault.encrypt_config(disk)
    except Exception as e:
        # BUGS-v2.63-293 观察项：这里以前 except: pass，vault 整体异常时
        # disk 仍是明文，直接落盘且没有任何记录。现在至少留痕（能 import 就登记，
        # 连 import 都失败就 stderr 打一行）。不抛异常 —— 「vault 坏掉仍能保存」
        # 是既有取舍，改它会连累整套设置页。
        try:
            from . import vault as _vault
            _vault.note_seal_failure("保存前加密异常: %s" % e)
        except Exception:
            try:
                print("[vault] 本机加密流程异常（%s），本次配置可能以明文落盘。" % e)
            except Exception:
                pass
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    # H3：先建 0600 的临时文件再写 —— 不要「先 open 写、写完再 chmod」，
    # 中间那段窗口里文件是默认权限，同机别的用户能读到密文与密钥路径。
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False, indent=2)
    harden_perm(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    harden_perm(CONFIG_PATH, 0o600)


def update_config(mutator) -> dict:
    """读-改-写 config.json，整个过程持锁。

    为什么需要：token 续期（qoder._persist_token / trae._persist_token）与
    用户在设置页保存配置都是「load → 改一个字段 → save」。两者并发时后写的
    那次会把对方刚改的字段整段覆盖回去（丢了用户刚填的凭据，或者丢了续期
    拿到的新 token，下一轮又拿旧 token 撞 401）。锁必须包住「读」而不只是
    「写」，否则两个线程都读到同一份旧快照。
    """
    with _SAVE_LOCK:
        cfg = load_config()
        mutator(cfg)
        _save_locked(cfg)
        return cfg
