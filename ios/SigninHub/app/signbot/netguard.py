"""
出站请求目标校验（BUGS-v2.61 的 M9：webhook / 自定义 provider 的 SSRF）。

## 为什么需要

`notify.py` 的 `_post_json` / `_post_form` 和 `provider.py` 的 `_run_http` 都是
`urllib.request.urlopen(url)`，而 `url` 完全来自用户在设置页填的
`notify.webhook_url` / `providers.*.http.url`。这条链路没有任何目标校验：

    POST /api/test_notify  {"webhook_url": "http://169.254.169.254/latest/meta-data/"}
    POST /api/test_notify  {"webhook_url": "http://127.0.0.1:8787/api/local_creds/qoder"}
    POST /api/test_notify  {"webhook_url": "http://192.168.1.1/admin/reboot"}

后端会把请求真的发出去，响应体的前 4000 字节还会写进 `notify_logs`，
前端「通知记录」里直接能看到 —— 等于一个**带响应回显的 SSRF**，
可以拿它探内网、读云主机元数据、打本机控制台。

## 策略

三层，缺一层就漏一种：

1. **协议**：默认只允许 `https`。`http` 只在目标是 RFC1918 私网地址时放行
   （用户自建的内网签到接口 / 内网 webhook 确实只有裸 HTTP，这条不能一刀切禁掉）。
2. **地址**：`socket.getaddrinfo` 解析出的**每一个**地址都要过 `_ip_ok`，
   拒绝 loopback / link-local（169.254.169.254 云元数据就在这里）/ 保留段 /
   组播 / 未指定。只看第一个地址是不够的 —— 一个域名可以同时解析出
   公网 IP 和内网 IP，`urlopen` 实际连哪个不由我们决定。
3. **重定向**：`urlopen` 默认自动跟随 302，攻击者可以用一个公网域名 302 到
   `http://127.0.0.1:8787/`。这里禁用自动重定向，自己走，每一跳都重新校验。

## 边界

- 这是**发起前**的校验，理论上存在 DNS rebinding 的 TOCTOU 窗口
  （校验时解析到公网，连接时解析到内网）。要彻底关掉得把解析结果 pin 成 IP
  再连，但那会破坏 TLS 的 SNI 与证书校验 —— 对「用户自己填 webhook」这个
  场景，校验 + 禁跳已经足够，不值得为它引入自签证书信任链。
- 不做域名白名单。四家已知渠道（企业微信 / 钉钉 / 飞书 / Bark …）的域名会变，
  硬编码白名单会在上游换域名时把用户的通知功能整体打死；地址段校验已经
  把「打内网」这条路封住了，剩下的「打公网任意站点」是用户自己的选择。
"""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

# 最多跟随几次重定向。webhook 正常不会跳，给 3 次够用又不会变成探测放大器。
MAX_REDIRECTS = 3

# 私网判定：只有这些才允许走裸 http
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class BlockedTarget(Exception):
    """目标地址被安全策略拒绝。消息直接面向用户，不要塞堆栈。"""


def _ip_obj(ip: str):
    try:
        return ipaddress.ip_address(ip.split("%")[0])  # 去掉 IPv6 zone id
    except ValueError:
        return None


def _is_private(ip) -> bool:
    return any(ip in n for n in _PRIVATE_NETS)


def _ip_ok(ip: str) -> bool:
    """单个解析结果是否允许作为出站目标。"""
    o = _ip_obj(ip)
    if o is None:
        return False
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要还原成 v4 再判，否则能绕过回环检查
    if getattr(o, "ipv4_mapped", None) is not None:
        o = o.ipv4_mapped
    if o.is_loopback or o.is_link_local or o.is_multicast:
        return False
    if o.is_reserved or o.is_unspecified:
        return False
    if getattr(o, "is_site_local", False):  # IPv6 fec0::/10
        return False
    return True


def _resolve(host: str, port: int):
    """解析出全部地址。解析失败抛 BlockedTarget（而不是让 urlopen 抛难懂的 URLError）。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedTarget("域名解析失败：%s（%s）" % (host, e))
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise BlockedTarget("域名解析不到任何地址：%s" % host)
    return ips


def check_url(url: str, *, allow_private_http: bool = True) -> str:
    """
    校验一个出站 URL，通过则原样返回，不通过抛 [BlockedTarget]。

    `allow_private_http=False` 时连私网也只允许 https（给「严格模式」留的开关）。
    """
    raw = (url or "").strip()
    if not raw:
        raise BlockedTarget("地址为空")
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        raise BlockedTarget("地址格式无法解析：%s" % raw)
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BlockedTarget("只支持 http / https 地址（当前是 %s）" % (scheme or "空"))
    host = (u.hostname or "").strip()
    if not host:
        raise BlockedTarget("地址里没有主机名：%s" % raw)
    try:
        port = u.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise BlockedTarget("端口不合法：%s" % raw)

    # 字面量 IP 不必解析，直接判；域名必须解析后逐个判
    literal = _ip_obj(host)
    ips = [host] if literal is not None else _resolve(host, port)
    for ip in ips:
        if not _ip_ok(ip):
            raise BlockedTarget(
                "目标地址 %s 属于本机 / 内网 / 保留段，已拒绝（防止被用来探测内网或读云主机元数据）" % ip
            )

    if scheme == "http":
        if not allow_private_http:
            raise BlockedTarget("出于安全考虑这里只允许 https 地址")
        # 裸 http 只放行「所有解析结果都是 RFC1918 私网」的情况
        all_private = all(_is_private(_ip_obj(i)) for i in ips if _ip_obj(i) is not None)
        if not all_private:
            raise BlockedTarget(
                "公网地址必须用 https（当前是 http://%s）。内网地址才可以走明文。" % host
            )
    return raw


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 的自动重定向，交给 [open_checked] 自己走并逐跳校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_checked(req: urllib.request.Request, timeout: float = 10.0,
                 allow_private_http: bool = True, limit: int = 4000):
    """
    校验目标 → 发请求 → 手动跟随有限次重定向（每跳重新校验）。

    返回 `(status, body_text)`；HTTP 错误码也当正常返回（通知渠道用 body 里的
    业务码表达失败，见 notify._check_body），只有网络层异常才抛。

    `limit` 是响应体最多读多少字节，默认 4000（通知渠道只需要前几 KB 判业务码）。
    余额接口要读全（实测 50 个赠送包 ≈ 90KB，截断会让 json.loads 失败、被误判成
    「接口异常」），调用方传 `limit=0` 表示不限。
    """
    url = req.full_url
    for _hop in range(MAX_REDIRECTS + 1):
        check_url(url, allow_private_http=allow_private_http)
        if url != req.full_url:
            req = urllib.request.Request(
                url, data=req.data, headers=dict(req.headers), method=req.get_method()
            )
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read() if limit <= 0 else r.read(limit)
                return r.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urllib.parse.urljoin(url, loc)
                continue
            raw = e.read() if limit <= 0 else e.read(limit)
            return e.code, raw.decode("utf-8", "replace")
    raise BlockedTarget("重定向次数过多（超过 %d 次）" % MAX_REDIRECTS)
