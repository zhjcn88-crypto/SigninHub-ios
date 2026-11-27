# SigninHub for iOS（TrollStore 版）

把 [Ayanguser/SigninHub](https://github.com/Ayanguser/SigninHub) 移植成 iOS App，
产出**无签名 `.ipa`**，用 TrollStore 直接安装。

- 无需越狱、无需 Apple 证书、无需描述文件、无需 Apple ID
- 完全离线运行：签到引擎就在 App 内，不依赖电脑、不依赖服务器

---

## 它是怎么运作的

上游项目是「Android App + 可选电脑端 Python 后端」。这里的做法是
**把那个 Python 后端整个搬进 iOS App**：

```
iOS App
├── CPython 3.13（官方 Python-Apple-support 预编译）
│   ├── 标准库
│   └── lib-dynload（_sqlite3 / _socket / _ssl / _hashlib …）
│       └─ 构建期转成 Frameworks/*.framework，原位置留 .fwork 占位
├── app/            ← 原 backend/（signbot + web）
└── WKWebView ──► http://127.0.0.1:8787/   ← 与桌面版/PWA 完全相同的界面
```

启动流程：

1. `main.m` 起一个 pthread，初始化 CPython 解释器；
2. 先 import `_ios_boot`（修 CA 证书 / 数据目录 / 临时目录）；
3. `prepare_server()` 关掉 `serve()` 的「自动生成访问口令」；
4. `signbot.server.serve("127.0.0.1", 8787)` 起 HTTP 服务；
5. 主线程 `UIApplicationMain` → WKWebView 轮询 `/api/ping`，通则加载页面。

> 为什么必须关掉自动口令：`serve()` 在口令为空时会随机生成一个写进
> `config.json`，那是给「电脑端 + 手机远程连」用的。App 内页面是回环来源，
> 拿不到这个口令就会全站 401，页面直接不可用。关掉后走「空口令 + 本机来源
> 放行」，服务只绑 `127.0.0.1`，局域网连不进来，安全性不受影响。

---

## 目录结构

```
SigninHub-ios/
├── ios/
│   ├── SigninHub.xcodeproj/
│   ├── Python.xcframework/          # fetch_python.sh 下载
│   └── SigninHub/
│       ├── main.m                   # CPython 引导 + UIApplicationMain
│       ├── AppDelegate.swift
│       ├── WebConsoleViewController.swift
│       ├── LoadingViewController.swift
│       ├── BackgroundKeeper.swift
│       ├── Info.plist
│       ├── cacert.pem               # iOS 必须自带 CA，OpenSSL 不认 Keychain
│       ├── Assets.xcassets/          # AppIcon
│       ├── app/                      # ← 后端代码（prepare_app.sh 生成）
│       └── app_packages/             # 纯 Python 依赖（当前为空）
├── scripts/
│   ├── fetch_python.sh              # 下载 CPython for iOS
│   ├── prepare_app.sh               # 复制 backend/ + 打补丁（找不到上游会自动下载）
│   ├── patch_ios.py                 # ctypes.wintypes 可移植性补丁
│   ├── make_boot.py                 # 生成 app/_ios_boot.py
│   ├── install_python.sh            # Xcode 构建阶段：装标准库、转 framework
│   └── build_unsigned_ipa.sh        # 构建 + 打包 ipa
├── verify/                          # 离线自检套件（约 180 项）
│   ├── run_all.py                   # 一键跑全部
│   └── check_*.py                   # 各项检查
└── .github/workflows/build-ipa.yml   # CI 一键出包
```

---

## 方式一：用 GitHub Actions 出包（推荐，无需 Mac）

1. 把这个目录推到你自己的 GitHub 仓库；
2. 打开 **Actions** → 左侧 **Build unsigned IPA** → **Run workflow**；
3. 跑完在该次运行的 **Artifacts** 里下载 `SigninHub-unsigned-ipa`；
4. 解压得到 `SigninHub.ipa`。

打 tag（`v1`、`v2`…）会自动创建 Release 并附上 ipa。

---

## 方式二：本机 macOS 构建

需要 Xcode 15+（macOS 13+）。

```bash
cd SigninHub-ios

# 1) 取上游后端
mkdir -p vendor && git clone --depth 1 https://github.com/Ayanguser/SigninHub vendor/SigninHub

# 2) 下载 CPython for iOS（约 34 MB）
./scripts/fetch_python.sh

# 3) 生成 app/（含 iOS 补丁）
./scripts/prepare_app.sh
python3 scripts/make_boot.py ios/SigninHub/app

# 4) 构建 + 打包
./scripts/build_unsigned_ipa.sh
```

产物：`build/SigninHub.ipa`

也可以直接开 Xcode 跑真机（需自备开发者证书）：

```bash
open ios/SigninHub.xcodeproj
```

---

## 自检（改完工程先跑这个）

```bash
python3 verify/run_all.py
```

7 套检查、约 180 项，全部是**离线**的，不需要 Xcode：

| 检查 | 查什么 |
|---|---|
| `check_project.py` | 文件齐全、源文件都进了编译、补丁已打、图标尺寸 |
| `check_pbxproj.py` | 用真的 OpenStep 解析器读 pbxproj，验 target/phase/build settings |
| `check_contract.py` | C 侧 `PyObject_GetAttrString("xxx")` 的名字在 Python 里真实存在 |
| `check_shell.py` | shell 脚本在 `set -euo pipefail` 下的 SIGPIPE / GNU 专有命令等坑 |
| `check_backend_e2e.py` | 真的起服务，打一遍 WebView 会发的 HTTP 请求 |
| `check_bundle.py` | 复刻 `.app` 结构，断言 `python/lib`、`app/`、`cacert.pem` 等路径成立 |
| `check_ipa.py` | 打一次 ipa，按 installd / TrollStore 的要求验包结构 |

CI 里也跑同一套（`Self-check before build` 步骤），任一项失败就不继续构建。

单跑某一项：

```bash
python3 verify/check_contract.py
```

> `check_pbxproj.py` 需要 `pip install pbxproj`；没装会自动跳过。

---

## 安装到 iPhone（TrollStore）

1. 把 `.ipa` 传到手机（隔空投送 / 文件 App / 任意网盘都行）；
2. 用 **TrollStore** 打开它（在文件 App 里点一下 → 分享 → TrollStore，
   或直接在 TrollStore 的「+”→ 选文件）；
3. 点 **Install**。

装好后图标是「签到中心」。

### 首次启动

- 首次启动会解压标准库、初始化解释器，**可能要 5–15 秒**，期间显示
  「正在启动签到引擎…」，属正常；
- 之后每次冷启动约 1–2 秒。

### 建议开启的设置

| 项 | 在哪 | 为什么 |
|---|---|---|
| 通知 | 系统设置 → 签到中心 → 通知 | 否则签到完没有提示 |
| 后台 App 刷新 | 系统设置 → 签到中心 | 配合 App 内的后台任务，提高定时成功率 |
| 关闭低电量模式 | 系统设置 → 电池 | 低电量会冻结后台 |

App 内「设置」页把「每日自动签到时间」调好即可，默认 08:30。

---

## 已知差异（相对 Android 版）

| 功能 | iOS 版 | 说明 |
|---|---|---|
| 定时签到 | ✅ | 进程内 Scheduler，配合 `beginBackgroundTask` 续期 |
| 后台长期存活 | ⚠️ | iOS 会挂起后台进程；TrollStore 装的应用通常比普通 App 更耐磨，但仍建议每天打开一次 App 让定时器跑起来 |
| 浏览器模式（Playwright） | ❌ | iOS 无法跑 Chromium，相关配置项会提示「未安装 Playwright」；**接口模式与官方协议直连完整可用** |
| 读本机 Qoder/WorkBuddy 凭据 | ❌ | 那是读桌面客户端的本地文件，手机上本来就没有；需手动粘贴 token |
| 本地通知推送 | ⚠️ | Webhook（钉钉/企微/飞书等）完整可用；系统通知需授权 |

**能真正签到的是**：WorkBuddy 官方协议、Trae Solo 官方协议、Qoder、
MiniMax、智谱、华为等「接口模式」，与电脑端能力一致。

---

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 一直停在「正在启动签到引擎…」 | 多半是 `app/` 没打进去。检查 `ios/SigninHub.app/app/signbot/server.py` 是否存在 |
| 启动就闪退 | 用 Xcode → Window → Devices 看控制台日志，搜 `[SigninHub]`，会打印完整 Python traceback |
| 页面能开但签到报 `CERTIFICATE_VERIFY_FAILED` | `cacert.pem` 没进包。检查 App 根目录有没有它 |
| 签到接口 401 / 页面空白 | `_ios_boot.prepare_server()` 没生效，`config.json` 里 `server.api_token` 非空。删掉该文件重启 App |
| 扩展模块 import 失败（`No module named _sqlite3`） | 构建阶段 `install_python.sh` 没跑或失败；检查包内 `Frameworks/_sqlite3.framework` 与 `lib-dynload/_sqlite3.fwork` 是否都在 |

看日志（真机）：

```
Xcode → Window → Devices and Simulators → 选设备 → Open Console
过滤 "SigninHub"
```

---

## 数据与隐私

- 所有数据存在 App 沙箱：`Library/Application Support/SigninHub/`
  （`config.json`、`signbot.db`）；
- 凭据用 `vault.py` 的 `sealed:` 通道加密（HMAC-SHA256 作 PRF 的 CTR + HMAC 认证，
  密钥在 `vault.key`），不是明文落盘；
- 没有公网入口：服务只绑 `127.0.0.1`；
- 卸载 App 即彻底清除。

---

## 授权

- 上游 SigninHub：见其仓库 LICENSE；
- CPython for iOS：[Python-Apple-support](https://github.com/beeware/Python-Apple-support)（PSF / MIT）；
- CA 包：[curl.se/ca](https://curl.se/ca/cacert.pem)（MPL 2.0）。
