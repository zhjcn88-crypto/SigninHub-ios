"""
config.json 里敏感字段的静态加密（BUGS-v2.25 的 P2-3）。

## 为什么需要

Android 侧的凭据早就走 `SecretBox`（AES-GCM + AndroidKeyStore），但电脑端
`backend/data/config.json` 一直是明文：

    "qoder": { "token": "dt-UUlawOCdKEHsNHQtilNDW...",
               "refreshToken": "drt-DtC8J4a8uH7QN99rN0De..." }

任何能读这个文件的东西（同步盘、备份、另一个进程、误发的 zip）都等于拿到了
上游 accessToken / refreshToken。

## 怎么做的

用 Windows DPAPI（`CryptProtectData`）—— 与 `qoder.py` 里解 Qoder 客户端
`Local State` 用的是同一套 API，只是方向相反。密钥由系统按「当前用户」托管，
不落地、不需要用户再记一个口令，重启后照样能解。

存盘形态：`dpapi:<base64(密文)>`。读盘时遇到这个前缀就解密，其它形态原样返回
（兼容老配置文件，也兼容非 Windows 环境）。

## 边界

- 只加密**已知的凭据字段**（见 `SENSITIVE_PATHS`），不动 `mode` / `region` /
  `url` 这些非敏感项 —— 否则用户手改 config.json 时会完全看不懂。
- 带 `***` 的掩码值不加密（那是前端回传的占位，不该进磁盘）。
- 空串不加密（保持 `""`，让 `readiness()` 的「未配置」判断继续成立）。
- 已经是 `dpapi:` / `sealed:` 前缀的值不重复加密（幂等）。

## 非 Windows 平台（BUGS-v2.61 的 H3）

原来 `_available()` 只认 `sys.platform == "win32"`，`protect()` 在 Linux / macOS
上直接 `return plain` —— 用户把后端跑在群晖 / 树莓派 / 云主机上时，accessToken
和 refreshToken 全部明文躺在 config.json 里，权限还是 666。

现在补一条纯标准库的兜底通道：

- 密钥：`DATA_DIR/vault.key`，32 字节 `os.urandom`，创建时就是 `0600`
  （`os.open(..., O_CREAT|O_EXCL, 0o600)`，不经过 umask）。
- 派生：`HMAC-SHA256(master, b"signinhub-vault-v1:enc")` 与 `:mac` 各 32 字节。
- 加密：HMAC-SHA256 作 PRF 的 CTR 流（`block_i = HMAC(enc_key, nonce||i)`），
  随机 16 字节 nonce。
- 认证：encrypt-then-MAC，`tag = HMAC(mac_key, nonce||ct)` 截 32 字节，
  解密前 `hmac.compare_digest` 校验，不通过一律当 `SENTINEL`（不返回半截明文）。
- 形态：`sealed:<b64(nonce|ct|tag)>`。

为什么不用 AES：标准库没有 AES，引第三方库（cryptography / pycryptodome）会
让「零依赖」这条前提破掉（后端一直只靠标准库跑）。HMAC-SHA256 作 PRF 的
CTR + HMAC 认证是标准的 encrypt-then-MAC 构造，安全性取决于密钥保密，
不取决于用哪个分组密码。

`dpapi:` 的密文在非 Windows 上解不开 —— 这是既成事实（DPAPI 密钥绑当前用户
和那台机器），只能报 `SENTINEL` 让用户重填。

## 解密失败不能覆盖密文（BUGS-v2.40 的 P1-1）

`unprotect` 解不开时**不能**只返回空串：`load_config()` 把这个空串当明文交给
调用方，之后任意一次 `save_config()`（设置页保存 / token 续期 / 调度器写库）
都会把 `""` 加密成 `""` 写回，原密文永久丢失。Android 侧有 `DECRYPT_FAILED`
哨兵 + `credsLost` 守卫，电脑端原来没有。

这里补三件事：

1. `unprotect` 失败返回 `SENTINEL`，并在 `_failed` 里记下这个字段路径；
2. `decrypt_config` 把 `SENTINEL` 映射回 `""` 给业务用（运行时语义仍是「未配置」），
   但 `_failed` 保留 —— 谁失败过一目了然；
3. `restore_failed(disk)` 在落盘前把「内存空串 + 磁盘是密文 + 该字段解密失败过」
   的项还原成磁盘原值，于是密文不会被空串覆盖；`reset_creds` 这类**用户主动
   清空**的动作会先 `mark_cleared()`，不进入还原名单。

`failures()` / `reset_failures()` 供 `/api/status` 的 `crypto_ok` 与
`POST /api/crypto_reset` 使用（与 Android 侧口径对齐）。
"""
import base64
import hashlib
import hmac
import os
import sys
import threading
import time

# iOS 移植补丁：non-Windows 上 ctypes.wintypes 会抛 ValueError（_type_ 'v'），
# 而本模块在顶层就要用它定义 _DATA_BLOB。软导入后 iOS 走 seal 兜底路径。
try:
    import ctypes
    from ctypes import wintypes
except Exception:  # pragma: no cover - 非 Windows 平台
    ctypes = None
    wintypes = None

PREFIX = "dpapi:"
SEAL_PREFIX = "sealed:"
_KEY_NAME = "vault.key"
_SEAL_ENC = b"signinhub-vault-v1:enc"
_SEAL_MAC = b"signinhub-vault-v1:mac"
_key_cache = None
_key_lock = threading.Lock()

# 解不开时的哨兵。用控制字符开头，正常凭据（token / cookie / JWT）不可能包含它，
# 因此可以安全地用它区分「解密失败」与「本来就是空」。
SENTINEL = "\u0000VAULT_FAILED"

# (provider, section, key) —— 与 config.py 的 DEFAULT_CONFIG 结构一一对应。
# 新增 provider 时必须同步这里，否则它的凭据会明文落盘（v2.27 的华为就是这样漏的）。
SENSITIVE_PATHS = [
    ("workbuddy", "wb", "accessToken"),
    ("workbuddy", "wb", "refreshToken"),
    ("traesolo", "trae", "token"),
    ("traesolo", "trae", "session"),
    ("qoder", "qoder", "token"),
    ("qoder", "qoder", "refreshToken"),
    ("minimax", "minimax", "accessToken"),
    ("minimax", "minimax", "refreshToken"),
    ("huawei", "huawei", "cookie"),
    ("huawei", "huawei", "portalCookie"),
]

_failed = set()          # {(prov, sect, key)} 本轮解密失败过的字段
_cleared = set()         # {(prov, sect, key)} 用户主动清空的字段（不参与还原）
_enc_failed = set()      # {(prov, sect, key)} 加密失败、只能明文落盘的字段（BUGS-v2.63-291 P2-2）
_enc_reason = ""         # 最近一次加密失败的原因，仅供日志/诊断
_enc_warned = False      # 只提示一次，别每次保存都刷屏
_can_cache = None        # can_encrypt() 的缓存：True 缓到进程结束，False 只缓 _CAN_TTL 秒
_can_cache_at = 0.0
_CAN_TTL = 15.0
_lock = threading.Lock()


def _note_seal_failure(reason: str) -> None:
    """记下「这次没能加密」。**这是安全降级，必须让人看见**，不能静默。"""
    global _enc_reason, _enc_warned
    with _lock:
        _enc_reason = reason or "unknown"
        first = not _enc_warned
        _enc_warned = True
    if first:
        try:
            print("[vault] 本机加密不可用（%s），凭据将以明文写入 config.json —— "
                  "请检查系统密钥/目录权限，修好后重新保存一次凭据可自动恢复加密。"
                  % (reason or "unknown"))
        except Exception:
            pass


def note_seal_failure(reason: str) -> None:
    """公开入口：vault 之外的模块（config.py）发现「这轮没加密」时也要留痕。"""
    _note_seal_failure(reason)


if ctypes is not None:  # iOS 移植补丁：无 ctypes 时不定义 DPAPI 结构体
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
else:  # pragma: no cover - 非 Windows 平台
    _DATA_BLOB = None

    def _blob(data: bytes):
        raise OSError("DPAPI 仅在 Windows 可用")


def _available() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# 纯标准库兜底加密（非 Windows）
# ---------------------------------------------------------------------------

def _key_path():
    from .config import DATA_DIR
    return DATA_DIR / _KEY_NAME


def _master_key():
    """读或首次生成 DATA_DIR/vault.key（32 字节，0600）。失败返回 None。"""
    global _key_cache
    with _key_lock:
        if _key_cache is not None:
            return _key_cache
        path = _key_path()
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(path.parent, 0o700)
                except OSError:
                    pass
                # O_EXCL + 0600：并发启动时只有一个进程能创建成功，且不经过 umask
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, os.urandom(32))
                finally:
                    os.close(fd)
                try:
                    os.chmod(str(path), 0o600)
                except OSError:
                    pass
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) < 32:
                return None
            _key_cache = raw[:32]
            return _key_cache
        except Exception:
            return None


def _subkeys():
    master = _master_key()
    if master is None:
        return None, None
    enc = hmac.new(master, _SEAL_ENC, hashlib.sha256).digest()
    mac = hmac.new(master, _SEAL_MAC, hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 作 PRF 的 CTR 流。"""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def _seal(plain: str):
    enc_key, mac_key = _subkeys()
    if enc_key is None:
        return None
    nonce = os.urandom(16)
    data = plain.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return SEAL_PREFIX + base64.b64encode(nonce + ct + tag).decode("ascii")


def _unseal(value: str):
    enc_key, mac_key = _subkeys()
    if enc_key is None:
        return SENTINEL
    try:
        blob = base64.b64decode(value[len(SEAL_PREFIX):])
        if len(blob) < 16 + 32:
            return SENTINEL
        nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
        want = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, want):
            return SENTINEL
        data = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
        return data.decode("utf-8", "replace")
    except Exception:
        return SENTINEL


def _can_seal() -> bool:
    return _subkeys()[0] is not None


def _dpapi_seal(plain: str):
    """DPAPI 密封：成功返回 `dpapi:` 串，失败返回 None。

    **纯函数：不登记 `_enc_failed`、不改 `_enc_reason`、不打日志。**
    调用方（`protect` / `can_encrypt`）自己决定失败要不要留痕。
    """
    din = _blob(plain.encode("utf-8"))
    dout = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(din), None, None, None, None, 0, ctypes.byref(dout)
    )
    if not ok:
        return None
    try:
        raw = ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dout.pbData)
    if not raw:
        return None
    return PREFIX + base64.b64encode(raw).decode("ascii")


def protect(plain: str) -> str:
    """明文 → 密文。空串 / 掩码值 / 已加密时原样返回。"""
    if not plain or plain[:8].startswith((PREFIX, SEAL_PREFIX)):
        return plain
    if "***" in plain:
        return plain
    if _available():
        try:
            sealed = _dpapi_seal(plain)
        except Exception as e:
            _note_seal_failure(str(e))
            return plain
        if sealed is None:
            # BUGS-v2.63-291 P2-2：以前这里静默 return plain，明文落盘且
            # /api/status 仍报 crypto_ok=true。现在至少留下可诊断的痕迹，
            # 由 encrypt_config() 把具体字段登记进 _enc_failed。
            _note_seal_failure("CryptProtectData 返回 0")
            return plain
        return sealed
    # 非 Windows：走本地密钥的 sealed 通道。密钥建不出来才原样返回（不写坏配置）。
    sealed = _seal(plain)
    if sealed is None:
        _note_seal_failure("vault.key 不可用")
        return plain
    return sealed


def unprotect(value: str) -> str:
    """密文 → 明文。非密文形态原样返回；解不开返回 `SENTINEL`。"""
    if not value:
        return value
    if value.startswith(SEAL_PREFIX):
        return _unseal(value)
    if not value.startswith(PREFIX):
        return value
    if not _available():
        # dpapi 密文绑 Windows 当前用户，换平台必然解不开
        return SENTINEL
    try:
        raw = base64.b64decode(value[len(PREFIX):])
        din = _blob(raw)
        dout = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(din), None, None, None, None, 0, ctypes.byref(dout)
        )
        if not ok:
            return SENTINEL
        try:
            return ctypes.string_at(dout.pbData, dout.cbData).decode("utf-8", "replace")
        finally:
            ctypes.windll.kernel32.LocalFree(dout.pbData)
    except Exception:
        return SENTINEL


def can_encrypt() -> bool:
    """本机**此刻**能不能把凭据加密（与「最近一次保存有没有失败」无关）。

    BUGS-v2.63-293 P3：`_enc_failed` 只是保存时的副作用 —— 用户点「知道了」把它
    清空、或进程重启（内存集合）之后，就没有任何东西会重新填充它，于是
    `/api/status` 永久报 `crypto_ok=true`，而 config.json 里的凭据仍是明文。

    BUGS-v2.63-294 P3：这里**不能借道 `protect()`** —— 那条路径失败时会写
    `_enc_reason` / `_enc_warned` 并打印警告，而本函数由 25 秒轮询的
    `/api/status` 调用，会把「只提示一次」的额度用掉、并用探测原因覆盖真实事故
    原因。现在直接调无副作用的 `_dpapi_seal` / `_subkeys`，不登记、不打日志。

    结果按进程缓存：True 缓到结束（真出问题时 `encrypt_config()` 仍会登记字段
    失败），False 只缓 `_CAN_TTL` 秒 —— 用户修好权限后最多等 15 秒横幅就消。
    `reset_failures()` 会同时失效缓存。
    """
    global _can_cache, _can_cache_at
    with _lock:
        if _can_cache is True:
            return True
        if _can_cache is False and (time.monotonic() - _can_cache_at) < _CAN_TTL:
            return False
    try:
        ok = bool(_dpapi_seal("signhub-can-encrypt-probe")) if _available() else _can_seal()
    except Exception:
        ok = False
    with _lock:
        _can_cache = ok
        _can_cache_at = time.monotonic()
    return ok


def failures():
    """本轮解密失败过的字段路径（`prov.sect.key` 字符串列表）。"""
    with _lock:
        return sorted("%s.%s.%s" % p for p in _failed)


def reset_failures():
    """清空失败名单（前端横幅点「知道了」/ 重新填完凭据后调用）。"""
    global _enc_warned, _can_cache
    with _lock:
        _failed.clear()
        _cleared.clear()
        _enc_failed.clear()
        _enc_warned = False
        # 能力缓存一并失效：点「知道了」之后必须重新探测，不能拿缓存里的旧结论。
        _can_cache = None


def mark_cleared(paths):
    """登记「用户主动清空」的字段，落盘时不再从磁盘还原旧密文。"""
    with _lock:
        for p in paths:
            _cleared.add(tuple(p))
            _failed.discard(tuple(p))


def _node(cfg: dict, prov, sect):
    node = ((cfg.get("providers") or {}).get(prov) or {}).get(sect)
    return node if isinstance(node, dict) else None


def encrypt_config(cfg: dict) -> dict:
    """就地加密敏感字段并返回同一对象（调用方传副本，别直接给 load_config 的结果）。"""
    for prov, sect, key in SENSITIVE_PATHS:
        node = _node(cfg, prov, sect)
        if node is None:
            continue
        cur = node.get(key)
        if isinstance(cur, str):
            out = protect(cur)
            node[key] = out
            # 加密「成功与否」按字段逐个判定：本来就该原样返回的三种形态
            # （空串 / 已是密文 / 掩码 ***）不算失败，其余「进了 protect 又原样出来」
            # 就是这次没能加密 → 登记，供 /api/status 的 crypto_ok 报出来。
            with _lock:
                if _looks_plain(cur) and out == cur:
                    _enc_failed.add((prov, sect, key))
                else:
                    _enc_failed.discard((prov, sect, key))
    return cfg


def _looks_plain(v: str) -> bool:
    """这串值「本应被加密」吗？空串 / 已加密 / 掩码都不算。"""
    if not v:
        return False
    if v.startswith((PREFIX, SEAL_PREFIX)):
        return False
    return "***" not in v


def encrypt_failures():
    """本轮加密失败、只能明文落盘的字段路径（`prov.sect.key`）。"""
    with _lock:
        return sorted("%s.%s.%s" % p for p in _enc_failed)


def encrypt_reason() -> str:
    """最近一次加密失败的原因（给日志/诊断用，不直接展示给用户）。"""
    with _lock:
        return _enc_reason


def decrypt_config(cfg: dict) -> dict:
    """
    就地解密敏感字段并返回同一对象。

    解不开的字段：值改成 `""`（业务语义 = 未配置），但路径记进 `_failed`，
    供 `restore_failed()` 在落盘时把原密文保住。
    """
    for prov, sect, key in SENSITIVE_PATHS:
        node = _node(cfg, prov, sect)
        if node is None:
            continue
        cur = node.get(key)
        if not isinstance(cur, str):
            continue
        out = unprotect(cur)
        path = (prov, sect, key)
        if out == SENTINEL:
            with _lock:
                _failed.add(path)
            node[key] = ""
        else:
            with _lock:
                _failed.discard(path)
            node[key] = out
    return cfg


def restore_failed(disk_cfg: dict) -> dict:
    """
    落盘前守卫：把「解密失败过 + 内存是空串 + 磁盘仍是密文」的字段还原成磁盘原值。

    不这样做的话，`_save_locked()` 会把 `""` 加密成 `""` 写回，
    DPAPI 密钥一变（换用户 / 重装系统 / 拷到别的机器）用户的凭据就被永久抹掉。
    """
    with _lock:
        todo = set(_failed) - set(_cleared)
    if not todo:
        return disk_cfg
    raw_cfg = _load_raw()
    for path in todo:
        prov, sect, key = path
        node = _node(disk_cfg, prov, sect)
        if node is None:
            continue
        if (node.get(key) or "") != "":
            continue  # 内存里已有值（用户刚重新填过），正常写入
        raw = (_node(raw_cfg, prov, sect) or {}).get(key)
        if isinstance(raw, str) and raw[:8].startswith((PREFIX, SEAL_PREFIX)):
            node[key] = raw  # 原密文原样写回，避免被空串覆盖
    return disk_cfg


def _load_raw() -> dict:
    """直接读磁盘上的 config.json 原文（不解密），只用于还原守卫。"""
    import json
    from .config import CONFIG_PATH
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}
