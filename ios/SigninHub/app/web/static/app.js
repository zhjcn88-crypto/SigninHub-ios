/* ============================================================
   签到中心 · 交互逻辑
   6 个精致交互 + 真签到配置 + 口令 + 记录增强 + 错误/空态
   ============================================================ */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  /**
   * 安全的事件绑定。
   *
   * 为什么要这个包装：整份 app.js 的初始化是「顶层顺序执行」的，任何一处
   * `$("#someId").addEventListener(...)` 拿到 null 就会抛 TypeError，
   * 而这个异常会中断后面所有的初始化（包括 window.__signhub 的导出），
   * 结果就是整页只剩空壳 —— 之前 smoke.html 少一个 #schedJitter 就整页白屏。
   * 换成 on() 之后，缺元素只是那一个按钮不响应，其余功能照常可用。
   */
  const on = (sel, ev, fn, r = document) => {
    const el = r.querySelector(sel);
    if (el) el.addEventListener(ev, fn);
    else if (window.__signhubErr === "") window.__signhubErr = "warn: 缺少元素 " + sel;
    return el;
  };
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  // 只有显式 #noanim 才关动画；不再跟随系统 prefers-reduced-motion，
  // 否则 Windows「关闭动画效果 / RDP / 虚拟机」会让本机控制台整页无过渡。
  const NOANIM = location.hash.includes("noanim");
  if (NOANIM) document.documentElement.classList.add("noanim");

  /* 全局错误捕获：任何未捕获异常都写进 title + 页面顶部，避免「静默闪退」 */
  window.__signhubErr = "";
  function reportErr(kind, msg) {
    const text = kind + ": " + msg;
    window.__signhubErr = (window.__signhubErr ? window.__signhubErr + " | " : "") + text;
    try { document.title = "ERR " + window.__signhubErr.slice(0, 300); } catch (_) {}
    const box = document.getElementById("fatalError");
    if (box) { box.hidden = false; box.textContent = text; }
  }
  window.addEventListener("error", (e) => reportErr("error", (e.message || "unknown") + " @ " + (e.filename || "").split("/").pop() + ":" + (e.lineno || 0)));
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    reportErr("rejection", (r && (r.message || r)) + "");
  });

  // 豆包于 V2.25 下线（2026-11-20 服务端 710010702 风控封禁），V2.66 重新接入：
  // 实测签到接口并没有封死（七种调用方式都只回 710012001 登录已过期），
  // 所以改成「整串 Cookie 直连」——不需要 a_bogus 签名，也不需要 WebView。
  // V2.68 起补齐 LOGO（官方 lf-flow-web-cdn 的 logo.png 300×300 缩放而来）。
  // V2.75 起**搁置隐藏**（DoubaoWebCheckin.ENABLED=false）：手机端 LocalApi 不再返回
  // providers.doubao / status.providers[doubao]，下面的 knownKeys 过滤后自动不画这张卡，
  // 记录页的筛选 chip 也已从 index.html 删除。这里的 LABELS/LOGOS/EMOJI 条目刻意保留，
  // 将来改回 true 就能立刻恢复，不用再补一遍。
  const LABELS = {
    workbuddy: "WorkBuddy",
    traesolo: "Trae Solo",
    qoder: "Qoder CN",
    minimax: "MiniMax Code",
    huawei: "华为码道",
    doubao: "豆包",
    zhipu: "智谱清言",
    qianfan: "百度千帆",
  };
  /* 真实 LOGO（从本机 App 资源提取，见 backend/web/static/logos/） */
  const LOGOS = {
    workbuddy: "/static/logos/workbuddy-128.png",
    traesolo: "/static/logos/traesolo-128.png",
    qoder: "/static/logos/qoder-128.png",
    minimax: "/static/logos/minimax-128.png",
    huawei: "/static/logos/huawei-128.png",
    doubao: "/static/logos/doubao-128.png",
    zhipu: "/static/logos/zhipu-128.png",
    qianfan: "/static/logos/qianfan-128.png",
  };
  const LOGO_2X = {
    workbuddy: "/static/logos/workbuddy-256.png",
    traesolo: "/static/logos/traesolo-256.png",
    qoder: "/static/logos/qoder-256.png",
    minimax: "/static/logos/minimax-256.png",
    huawei: "/static/logos/huawei-256.png",
    doubao: "/static/logos/doubao-256.png",
    zhipu: "/static/logos/zhipu-256.png",
    qianfan: "/static/logos/qianfan-256.png",
  };
  const EMOJI = {
    workbuddy: "🧩", traesolo: "🚀", qoder: "🅠", minimax: "◈", huawei: "☁", doubao: "🫘",
    zhipu: "✦", qianfan: "🅑",
  };
  function hasLogo(key) { return !!LOGOS[key]; }
  function iconHTML(key) {
    if (LOGOS[key]) {
      return `<img src="${LOGOS[key]}" srcset="${LOGOS[key]} 1x, ${LOGO_2X[key]} 2x" alt="${LABELS[key] || key}" decoding="async" />`;
    }
    return EMOJI[key] || "◉";
  }
  const STATUS_CN = { success: "已签到", already: "已签到", fail: "失败", idle: "待签" };

  /* ---------------- 可配置 API 基址 ---------------- */
  let BASE = (localStorage.getItem("signhub_base") || "").trim();
  function baseUrl() {
    if (!BASE) return location.origin;
    return BASE.replace(/\/+$/, "");
  }
  /* M1（BUGS-v2.61）：口令不再写 localStorage。
     localStorage 是「同源、永久、任何脚本都能读」的存储 —— 后端一旦被 XSS 或
     局域网内被仿站页面劫持，令牌会跟着浏览器一直留在盘上（关掉浏览器也不消失，
     共享电脑上更是直接泄露）。改成 sessionStorage：只活在当前标签页，关掉即失效。
     旧版本存在 localStorage 里的值读一次后立刻删除，完成迁移。 */
  let TOKEN = "";
  try {
    TOKEN = (sessionStorage.getItem("signhub_token") || "").trim();
    if (!TOKEN) {
      const legacy = (localStorage.getItem("signhub_token") || "").trim();
      if (legacy) {
        TOKEN = legacy;
        sessionStorage.setItem("signhub_token", legacy);
      }
    }
    localStorage.removeItem("signhub_token");
  } catch (_) { TOKEN = ""; }

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers);
    if (TOKEN) headers["X-API-Token"] = TOKEN;
    if (opts.body) headers["Content-Type"] = "application/json";
    // 超时兜底：没有它的话后端卡住（上游无响应 / 死锁）时 fetch 永不 settle，
    // 「一键签到」按钮会一直停在「正在签到…」，用户只能杀进程。
    // 签到类接口要打上游，给足 90 秒；其余 25 秒足够（本机接口都是毫秒级）。
    const ms = opts.timeout != null ? opts.timeout : (/\/api\/(checkin|test|refresh_totals|growth)/.test(path) ? 90000 : 25000);
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => { try { ctl.abort(); } catch (_) {} }, ms) : 0;
    let r;
    try {
      r = await fetch(baseUrl() + path, Object.assign({}, opts, { headers, signal: ctl ? ctl.signal : undefined }));
    } catch (e) {
      if (e && (e.name === "AbortError" || /abort/i.test(String(e.message || "")))) {
        const err = new Error("请求超时（" + Math.round(ms / 1000) + " 秒无响应）");
        err.status = 0;
        throw err;
      }
      throw e;
    } finally {
      if (timer) clearTimeout(timer);
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(data.error || ("HTTP " + r.status));
      err.status = r.status;
      throw err;
    }
    return data;
  }

  const state = { status: null, config: null, stats: null, logFilter: "", wheelHour: null, wheelMin: null, wheelLocked: true, lastTest: {}, growthLoaded: false, heat: null, stackOpen: {}, readySnapshot: null, autoRefreshing: false, wbAccounts: null, wbAccountsErr: "" };

  /* 凭据「从无到有」的检测（用户 m13271：登录新 agent 后自动刷新一次余额）。
     用前后快照对比，而不是每次 loadAll 都刷 —— 25 秒轮询每轮打一次上游太重。
     readySnapshot === null 表示还没建立基线（首次加载），此时不触发：
     首装本来就没有数字可刷，先让占位卡出来就行。

     两种「从无到有」都要算：
       ① ready 由 false 变 true（第一次给某家配凭据）；
       ② 同一家的账号数变多（已有 1 个号，又加了第 2 个）——
          ready 一直是 true，只看 ready 会漏掉「加第二个号」这种最常见的情况。 */
  function readyMap(list) {
    const m = {};
    (list || []).forEach((p) => {
      if (!p || !p.key) return;
      const accts = Array.isArray(p.accounts) ? p.accounts.length : 0;
      // portal_ok 是华为专有的「会话是否可用（= 余额能否读）」：
      // 它从 false 变 true 时 ready 早就是 true 了，只看 ready 会漏掉
      // 「积分从读不到变成读得到」这一次刷新（m21086）。
      m[p.key] = { ready: p.ready === true, n: accts, portal: p.portal_ok !== false };
    });
    return m;
  }
  function readyGained() {
    const prev = state.readySnapshot;
    const now = readyMap(state.status && state.status.providers);
    const gained = [];
    if (prev) {
      Object.keys(now).forEach((k) => {
        const a = prev[k];
        const b = now[k];
        if (!b || !b.ready) return;
        if (!a || a.ready === false || b.n > (a.n || 0)) gained.push(k);
        // 华为：门户 cookie 从「没有」变「有」= 积分刚变得可读，值得刷一次
        else if (a.portal === false && b.portal === true) gained.push(k);
      });
    }
    state.readySnapshot = now;
    return gained;
  }

  /* ---------------- 版本号 ---------------- */
  // /api/ping 是唯一不需要口令的接口，且 APK 本地模式由 LocalApi 注入 BuildConfig 版本。
  // 显示成「版本 1.37（37）· 安卓本地」或「版本 1.37 · 电脑端」，用来确认装/连的是哪一版。
  async function renderVersion() {
    const el = $("#verLine");
    if (!el) return;
    try {
      const p = await api("/api/ping");
      const v = p.appVersion || p.version || "?";
      const code = p.versionCode ? "（" + p.versionCode + "）" : "";
      el.textContent = "版本 " + v + code + " · " + (p.local ? "安卓本地" : "电脑端");
    } catch (e) {
      el.textContent = "版本 —（无法读取 /api/ping）";
    }
  }

  /* ---------------- toast ----------------
     用户 m13478：所有提示信息统一放到「下拉后余额显示」的那块顶部区域（和 #ptr 同一个坐标），
     并且弹出/消失都要过渡：
       弹出 —— 从中间向左右两边扩散 + 由模糊转清晰；
       消失 —— 整体文字雾化，像烟一样从字体上方散掉。
     动画全部写在 app.css 的 @keyframes hintIn / hintOut 里（GPU 友好的 transform+filter），
     这里只负责「重启动画」和「入场/退场两段计时」。 */
  function hideHint() {
    const box = $("#hint");
    if (!box) return;
    clearTimeout(hintTimer);
    clearTimeout(hintOutTimer);
    box.classList.remove("show", "out", "busy", "ok", "fail");
  }
  // 提示条与下拉指示器共用同一块顶部坐标：谁出现谁就把对方收掉
  function hidePtr() {
    const p = $("#ptr");
    if (!p) return;
    p.classList.remove("armed", "busy", "ok", "fail", "dragging");
    p.style.setProperty("--ptr-o", "0");
    p.style.setProperty("--ptr-y", "0px");
  }
  let hintTimer = 0;
  let hintOutTimer = 0;
  // 58 处 toast() 调用不可能逐个改签名 —— 没显式给 kind 时按文案推断语义色。
  // 只影响配色，不影响动画；推错了最多是颜色不搭，不会丢信息。
  function guessKind(msg) {
    const s = String(msg || "");
    if (/(失败|错误|无效|超时|不可用|异常|拒绝|丢失|过期|断开|未连接|无法)/.test(s)) return "fail";
    if (/(已更新|已保存|已设置|已添加|已删除|已完成|已复制|成功|完成|已清除|已签到)/.test(s)) return "ok";
    if (/(正在|中…|中\.\.\.)/.test(s)) return "busy";
    return "";
  }
  // kind: "" | "busy" | "ok" | "fail" —— 只影响配色，不影响动画
  function toast(msg, ms = 2400, kind) {
    const box = $("#hint");
    const txt = $("#hintText");
    // 老缓存页面里没有 #hint 时回落到旧 toast，避免提示彻底丢失
    if (!box || !txt) {
      const el = $("#toast");
      if (!el) return;
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(hintTimer);
      hintTimer = setTimeout(() => el.classList.remove("show"), ms);
      return;
    }
    // 与下拉指示器抢同一个位置：先把它收掉，两层叠在一起会糊成一团
    hidePtr();
    txt.textContent = msg;
    box.classList.remove("busy", "ok", "fail");
    const k = kind === undefined ? guessKind(msg) : kind;
    if (k) box.classList.add(k);
    clearTimeout(hintTimer);
    clearTimeout(hintOutTimer);
    box.classList.remove("out");
    if (NOANIM) {
      box.classList.add("show");
    } else {
      // 连续两条提示必须重播入场动画：先摘 show 再强制回流，否则浏览器认为状态没变
      box.classList.remove("show");
      void txt.offsetWidth;
      box.classList.add("show");
    }
    hintTimer = setTimeout(() => {
      if (NOANIM) {
        box.classList.remove("show", "out");
        return;
      }
      box.classList.add("out");
      hintOutTimer = setTimeout(() => box.classList.remove("show", "out"), 640);
    }, Math.max(600, ms));
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ============================================================
     ② 进度联动
     ============================================================ */
  const RING_C = 2 * Math.PI * 52;
  let ringShown = 0;
  function setProgress(done, total) {
    const pct = total ? done / total : 0;
    $("#ringFg").style.strokeDashoffset = RING_C * (1 - pct);
    animateNum($("#ringNum"), ringShown, Math.round(pct * 100));
    ringShown = Math.round(pct * 100);
    $("#ringCap").textContent = `${done} / ${total} 已签`;
    $("#heroHint").textContent = pct >= 1
      ? "今日全部完成，太棒了 🎉"
      : (done > 0 ? "再签 " + (total - done) + " 个就满勤啦" : "连续签到可保持积分增长");
  }
  function animateNum(el, from, to) {
    if (NOANIM) { el.textContent = to; return; }
    const start = performance.now(), dur = 520;
    (function step(now) {
      const p = clamp((now - start) / dur, 0, 1);
      const e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(step);
    })(performance.now());
  }

  /* ============================================================
     ③ 叠卡服务
     ============================================================ */
  /* 服务卡副标题：统一成一句话，不再透出各家五花八门的 message
     （「今日已签到 · 连续 23 天 · 今日 100 积分」「今日已领取（本期 100 Credits）」…）。
     用户 m08182：签到完成统一「今日已签到」，未签到统一「请签到」。
     只有两句 —— 「未配置」「已禁用」这类状态已经由名字后面的小徽章表达，
     详细的 message / 积分 / 连签天数在「记录」页可查。 */
  function svcSubText(p) {
    const done = p.status === "success" || p.status === "already";
    // 「没有签到活动」不是「已签到」：WorkBuddy 国际版账号上游根本没有签到活动
    // （桌面客户端日志对国际版账号打印 `refreshStatus -> inactive {"hasData":false}`），
    // 我们只能读余额。若副标题写「已签到」，用户会以为签到了却没拿到积分。
    // 判据用 message 里的固定前缀，因为它来自 LocalCheckin.noActivityText()。
    const msg = p.message || "";
    if (msg.indexOf("没有签到活动") >= 0 || msg.indexOf("没有可领取的签到活动") >= 0) {
      return "无签到活动 · 仅显示余额";
    }
    // 失败文案统一截到 ~20 字（完整原文仍在 title 上）。抽成函数是为了让
    // Qoder 分支与通用 fail 分支不会各写一套，改一处漏一处（BUGS-v2.63-290 P2-2）。
    const shortMsg = (s) => (s.length > 20 ? s.slice(0, 20) + "…" : s);
    // Qoder 不是「签到」而是每日 10:00（UTC+8）刷新的积分活动（用户 m08264 第 ② 条）：
    // 说「签到成功」会让人以为明天 0 点又能领，实际要等到 10:00。
    // 但失败必须优先说原因：token 失效 / 9074 风控 / 活动没开 与「还没到 10 点」
    // 在界面上长得一模一样，用户看不出要重新登录（BUGS-v2.63-290 P2 ②）。
    if (p.key === "qoder") {
      if (done) return "已领取 · 每日 10:00 刷新";
      if (p.status === "fail" && msg) return shortMsg(msg);
      return "每日 10:00 后可领";
    }
    // 失败必须把原因摆出来（用户 m05383）：以前 fail 和 idle 都写「未签到」，失败原因
    // 只塞在 title 属性里，手机上要长按才看得到 —— 用户只看到「主账号签不上」，
    // 完全无法自查（是 token 过期、还是 9074 风控、还是活动没开）。这里把 message
    // 截到 ~20 字直接显示；完整原文仍在 title 里。
    if (p.status === "fail" && msg) {
      return shortMsg(msg);
    }
    // 用户 m14001：状态就用「已签到 / 未签到」两句直说，不要「今日已签到 / 请签到」
    // 这种一边陈述一边祈使的混搭 —— 两句话法不一致，扫一眼分不出这是状态还是按钮。
    return done ? "已签到" : "未签到";
  }

  function renderStack() {
    const wrap = $("#stack");
    // P2 N2（BUGS-v2.3）：/api/status 抛异常时 LocalApi 返回 {"error":…,"__http_status":500}，
    // 没有 providers 字段 —— 直接 .forEach 会 TypeError，冒到 loadAll 的 catch 变成
    // 「无法连接 … Cannot read properties of undefined (reading 'forEach')」，
    // 真因（服务端 500）完全看不到。同文件其它两处本来就写了 (… || []) 兜底。
    const providers = (state.status && state.status.providers) || [];
    const html = [];
    providers.forEach((p) => {
      // 多账号（用户 m08264 第 ① 条）：/api/status 的 accounts 里一个账号一条，
      // 主账号恒排第一 —— 堆叠时它就是头卡。只有一个账号时退化成单张卡，与以前一致。
      const accts = (Array.isArray(p.accounts) && p.accounts.length) ? p.accounts : [null];
      const multi = accts.length > 1;
      // 叠着的牌可以点开（用户 m08549），展开后同组按平铺排，收起时叠回一摞。
      // 用户 m18704：「签到后显示图片样式」—— 签到完成后 8 张卡全摊开成一长列，
      // 一屏装不下、也看不出哪家签了哪家没签。所以展开状态改成**默认按签到状态决定**：
      //   · 全部签完 → 叠成一摞（只露头卡，一眼看出这家做完了）
      //   · 有没签的 → 摊开（要能看到是哪个账号没签）
      // 用户手动点过的那组（state.stackOpen 里有显式值）优先，尊重手动操作；
      // 「一键全部签到」完成后会清掉所有显式值，让状态重新接管。
      const allDone = accts.every((a) => {
        const s = a ? (a.status || "idle") : p.status;
        return s === "success" || s === "already";
      });
      const unfolded = state.stackOpen[p.key] != null
        ? !!state.stackOpen[p.key]
        : !allDone;
      accts.forEach((a, ai) => {
        const st = a ? (a.status || "idle") : p.status;
        const done = st === "success" || st === "already";
        const disabled = !p.enabled;
        const modeText = p.mode === "browser" ? "浏览器"
        : (p.mode === "wb" || p.mode === "trae"
           || p.mode === "qoder" || p.mode === "minimax"
           || p.mode === "huawei" || p.mode === "doubao"
           || p.mode === "zhipu" || p.mode === "qianfan" ? "直连" : "接口");
        const badge = disabled
          ? `<span class="svc-tag">已禁用</span>`
          : (p.ready ? `<span class="svc-tag">${modeText}</span>`
                      : `<span class="svc-tag" style="color:#ffcf5c;border-color:rgba(255,207,92,.5)">未配置</span>`);
        // 账号徽章：多账号时统一显示「账号 1/2/3/4」，头卡也不例外（用户 m08666）。
        // 用户 m16555 明确要求主页保持编号，不显示昵称/手机号 —— 手机号只出现在
        // 「配置 → 多账号管理」里（那里是管理场景，需要一眼认出是哪个号；
        // 主页是每天签到的场景，编号最短、扫视成本最低）。
        const chip = multi
          ? `<span class="svc-acct">账号 ${ai + 1}</span>`
          : "";
        // 单张卡用 provider 行本身，多张卡用账号行的数据（积分/状态各自独立）
        const v = a ? {
          key: p.key, label: p.label, enabled: p.enabled, mode: p.mode, ready: p.ready,
          status: st,
          message: a.message || "",
          points: a.points == null ? "" : a.points,
          total_points: a.total_points,
        } : p;
        // p.key / st 来自后端 provider 定义，正常是固定枚举值；但一旦有人改了
        // config.json 里的 provider 名（或中间层被篡改），它们就会带着引号拼进
        // 属性与 class 里 —— 统一过一遍 escapeHtml，代价为零。
        html.push(`<div class="svc t-${escapeHtml(st === "already" ? "already" : st)}${multi && ai > 0 && !unfolded ? " piled" : ""}" data-key="${escapeHtml(p.key)}" data-acct="${a ? escapeHtml(a.account_key || "") : ""}" data-pile="${multi ? ai : 0}"${multi ? ` data-multi="1"` : ""}>
        <div class="svc-ico${hasLogo(p.key) ? " has-logo" : ""}">${iconHTML(p.key)}</div>
        <div class="svc-main">
          <div class="svc-name"><span class="svc-label">${escapeHtml(p.label)}</span>${badge}</div>
          <div class="svc-sub"${v.message ? ` title="${escapeHtml(v.message)}"` : ""}>${chip}<span class="svc-subtxt">${escapeHtml(svcSubText(v))}</span></div>
        </div>
        <div class="svc-right">
          ${svcPointsHTML(v)}
          ${disabled ? `<span class="svc-status s-idle">—</span>`
            : (multi && ai > 0
               // 后继账号卡不给按钮：签到是 provider 级的（一次把所有账号都签一遍），
               // N 个按钮点哪个都一样，反而让人以为能单独签某一个。
               ? `<span class="svc-status ${done ? "s-succ" : "s-idle"}">${done ? "✓" : "待签"}</span>`
               : checkBtnHTML(done))}
        </div>
      </div>`);
      });
    });
    wrap.innerHTML = html.join("");
    $$(".ckbtn", wrap).forEach((btn) => {
      // 签到进行中被轮询重绘时把 busy 补回去（BUGS-v2.63-290 P3-6）
      if (busyKeys.has(btn.closest(".svc").dataset.key)) btn.classList.add("busy");
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        doCheckin(btn.closest(".svc"));
      });
    });
    // 点叠着的牌 → 展开 / 收回（用户 m08549）。只绑一次，靠事件委托扛住重渲染。
    if (!wrap.dataset.toggleBound) {
      wrap.dataset.toggleBound = "1";
      wrap.addEventListener("click", (e) => {
        const svc = e.target.closest(".svc[data-multi]");
        if (!svc) return;
        if (e.target.closest(".ckbtn")) return;   // 签到按钮自己处理
        toggleStack(svc.dataset.key);
      });
    }
    // 卡片是绝对定位，渲染完按实际数量排一遍位置
    stackLayout();
  }

  /* 卡片列表布局。单账号的卡一律平铺；同一服务有多个账号时默认叠成一摞，
     点头卡可以展开（用户 m08549），展开后同组也按平铺排。
     .svc 是 position:absolute，所以高度与位移仍要 JS 按实际卡片数算。 */
  const STACK_H = 78, STACK_GAP = 8;
  /* 多账号堆叠参数：每张后继卡下沉 PILE_STEP、左右各收 PILE_INSET，
     形成「一叠牌」的观感；头卡（主账号）完整显示且层级最高。 */
  const PILE_STEP = 16, PILE_INSET = 8;
  /* 这一组该不该叠起来。与 renderStack 里算 unfolded 的逻辑必须完全一致，
     否则 class 与 transform 会打架（一边说叠、一边按平铺排位置）。
     用户手动点过（state.stackOpen 里有显式布尔值）就听用户的；
     没点过则按签到状态：全签完 → 叠起来，还有没签的 → 摊开。 */
  function groupPiled(key, items) {
    if (items.length <= 1) return false;
    const manual = state.stackOpen[key];
    if (manual != null) return !manual;
    return items.every((el) =>
      el.classList.contains("t-success") || el.classList.contains("t-already"));
  }
  function stackLayout() {
    const wrap = $("#stack");
    if (!wrap) return;
    const kids = Array.prototype.slice.call(wrap.children);
    if (!kids.length) { wrap.style.height = "0px"; return; }
    // 按 data-key 分组（同一服务多账号在 DOM 里连续，且主账号在前）
    const groups = [];
    kids.forEach((el) => {
      const k = el.dataset.key || "";
      let g = groups[groups.length - 1];
      if (!g || g.key !== k) { g = { key: k, items: [] }; groups.push(g); }
      g.items.push(el);
    });
    let y = 0;
    groups.forEach((g) => {
      const n = g.items.length;
      const piled = groupPiled(g.key, g.items);   // 展开后不再叠
      g.items.forEach((el, i) => {
        el.classList.toggle("piled", piled && i > 0);
        el.style.zIndex = String(1000 - i);          // 头卡最高，压住后面的
        if (piled && i > 0) {
          // 高度不缩 —— 缩了底边就和头卡齐平、被完全遮住，等于看不见第二张。
          // 靠「下沉 i*PILE_STEP」把头卡下沿之外的一条边露出来，再左右各收 PILE_INSET
          // 露出侧边，才有「一叠牌」的层次感。
          el.style.transform = "translateY(" + (y + i * PILE_STEP) + "px)";
          el.style.left = (i * PILE_INSET) + "px";
          el.style.right = (i * PILE_INSET) + "px";
        } else {
          // 平铺：一张一行，行距 STACK_H + STACK_GAP
          el.style.transform = "translateY(" + (y + i * (STACK_H + STACK_GAP)) + "px)";
          el.style.left = "";
          el.style.right = "";
        }
        el.style.height = "";
      });
      y += piled ? (n - 1) * PILE_STEP + STACK_H : n * (STACK_H + STACK_GAP) - STACK_GAP;
      y += STACK_GAP;
    });
    wrap.style.height = Math.max(0, y - STACK_GAP) + "px";
    wrap.style.marginTop = "0px";
  }
  /* 点叠着的牌 → 展开；再点 → 收回。整组一起动。
     不重渲染 innerHTML（重渲染会让元素变成新节点、transform 过渡丢失，看着像瞬移），
     只改 class + 重排，靠 .svc 自己的 transform transition 补动画。
     展开状态记在 state.stackOpen 里，25 秒轮询重渲染后仍然保持。
     头卡不再有箭头（用户 m08666），展开与否靠牌面自己摊开/叠回表达。
     注意「当前是叠还是摊」不能直接读 state.stackOpen（它可能是 null = 交给签到状态决定），
     必须用 groupPiled 求当前实际状态，再取反写回显式值。 */
  function toggleStack(key) {
    if (!key) return;
    const wrap = $("#stack");
    if (!wrap) return;
    const group = Array.prototype.filter.call(
      wrap.children, (el) => el.dataset.key === key);
    if (!group.length) return;
    const nowPiled = groupPiled(key, group);
    state.stackOpen[key] = nowPiled;   // 叠着 → 展开(true)；摊着 → 叠回(false)
    group.forEach((el, i) => {
      el.classList.toggle("piled", !nowPiled && i > 0);
    });
    stackLayout();
  }
  /**
   * 签到按钮：四种形态共用一套 DOM，靠 class 切换（用户 m22039）。
   *
   * - 默认：紫色胶囊 + 「签到」二字
   * - `.busy`：胶囊收成圆，「签到」淡出，一段弧线绕着圆转圈（加载）
   * - `.done`：绿圈，弧线收成一笔勾
   * - `.fail`：红圈，弧线折成两笔叉
   *
   * 为什么三个图形都预先渲染、只切 class：换 innerHTML 会丢掉正在跑的
   * CSS 过渡（弧线转圈时把节点换掉，动画会从零开始闪一下）。三个 svg
   * 都绝对定位叠在同一处，靠 opacity / dashoffset 决定谁可见。
   */
  /**
   * 正在签到的 provider key 集合（BUGS-v2.63-290 P3-6）。
   *
   * 25 秒轮询会调 renderStack()，用 `wrap.innerHTML = html.join("")` 整体重建
   * #stack —— 挂在按钮上的 `.busy` 随之丢失，用户眼前的转圈变回「签到」，
   * 而 doCheckin 手上还握着已被替换掉的旧节点，勾/叉动画看不见。
   * 把「进行中」提升成模块级状态，重绘时按 key 补回 busy 即可。
   */
  const busyKeys = new Set();

  function checkBtnHTML(done) {
    return `<button class="ckbtn ${done ? "done" : ""}" data-act="sign" aria-label="签到">
      <span class="lbl">${done ? "" : "签到"}</span>
      <svg class="spin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/></svg>
      <svg class="ck" viewBox="0 0 24 24"><path d="M4 12.5 L9.5 18 L20 6"/></svg>
      <svg class="xx" viewBox="0 0 24 24"><path class="x1" d="M6 6 L18 18"/><path class="x2" d="M18 6 L6 18"/></svg>
    </button>`;
  }

  /* ============================================================
     ① 完成反馈
     ============================================================ */
  const PALETTE = ["#37e0a6", "#6c8cff", "#9b6cff", "#ffcf5c", "#4bd6ff"];
  function burst(btn) {
    const wrap = document.createElement("div");
    wrap.className = "burst";
    btn.appendChild(wrap);
    for (let i = 0; i < 14; i++) {
      const dot = document.createElement("i");
      dot.className = "pdot";
      const ang = (Math.PI * 2 * i) / 14 + Math.random() * 0.4;
      const dist = 26 + Math.random() * 30;
      dot.style.setProperty("--dx", Math.cos(ang) * dist + "px");
      dot.style.setProperty("--dy", Math.sin(ang) * dist + "px");
      dot.style.background = PALETTE[i % PALETTE.length];
      const sz = 3 + Math.random() * 4;
      dot.style.width = dot.style.height = sz + "px";
      dot.style.animationDelay = (Math.random() * 50) + "ms";
      wrap.appendChild(dot);
    }
    const ring = document.createElement("i");
    ring.className = "ringburst";
    wrap.appendChild(ring);
    setTimeout(() => wrap.remove(), 900);
  }

  async function doCheckin(svcEl) {
    const key = svcEl.dataset.key;
    const p = ((state.status && state.status.providers) || []).find((x) => x.key === key);
    if (p && !p.ready) {
      toast(`${p.label} 还没配置好，请到「设置 → 签到配置」填写`);
      return;
    }
    const btn = $(".ckbtn", svcEl);
    // 这张卡属于哪个账号（多账号堆叠时每张卡都带 data-acct）
    const acctKey = svcEl.dataset.acct || "";
    // `.done` 挡住重复签到；`.fail` 放行（失败后允许直接重试）；
    // `.busy` 挡住重复点击。
    if (!btn || btn.classList.contains("busy") || btn.classList.contains("done")) return;
    // 用户 m22039：点下去不能立刻打勾 —— 只进加载态，终态等真实结果。
    // 先把上一次的 fail 清掉，否则红圈会在转圈时透出来。
    btn.classList.remove("fail", "done");
    btn.classList.add("busy");
    busyKeys.add(key);
    // 兜底看门狗（用户 m24705）：`.busy` 只有在拿到响应后才会被摘掉，
    // 一旦桥/网络层异常导致 await 永远不 settle（历史上 CheckinGate 锁泄漏就是这样），
    // 卡片会永久转圈，只能清后台重开。这里到点强制落成「失败」，让用户能重试。
    // 取 95 秒：比 api() 给签到类接口的 90 秒超时略长 —— 正常路径上 api() 先抛
    // AbortError 走 catch 分支，这个看门狗只在 Promise 彻底不 settle 时才生效。
    let watchdog = 0;
    const armWatchdog = () => {
      watchdog = setTimeout(() => {
        // 95 秒里 25 秒轮询必然重建过 #stack（约 3 次），btn 是点击时抓的那个旧节点。
        // 只摘旧节点的 class，屏幕上的新按钮会永久停在转圈（BUGS-v2.63-291 P3-2）——
        // 这里和正常路径一样，先按 data-key + data-acct 找当前节点再动。
        const liveCard = $$(`#stack .svc[data-key="${key}"]`).find(
          (el) => (el.dataset.acct || "") === acctKey) || svcEl;
        const liveBtn = $(".ckbtn", liveCard);
        if (!liveBtn || !liveBtn.classList.contains("busy")) return;
        busyKeys.delete(key);
        liveBtn.classList.remove("busy");
        liveBtn.classList.add("fail");
        // 顺带把副标题换掉：只弹 toast 的话，过一会儿没人记得这张卡为什么是叉。
        // 不改积分/总分（markSvcResult 会把 res.points 当空值清屏），只动文案。
        const subTxt = $(".svc-subtxt", liveCard);
        if (subTxt) subTxt.textContent = "无响应 · 可再点一次";
        toast(`${LABELS[key] || key} 无响应：等了 95 秒仍未拿到结果，可再点一次重试`, 4000);
      }, 95000);
    };
    const clearWatchdog = () => { if (watchdog) { clearTimeout(watchdog); watchdog = 0; } };
    armWatchdog();
    try {
      const res = await api("/api/checkin/" + key, { method: "POST" });
      clearWatchdog();
      if (p) {
        p.status = res.status;
        p.points = res.points || p.points;
        p.message = res.message;
        p.total_points = res.total_points;
      }
      // 多账号（BUGS-v2.63-290 P3-2）：进度环按 accounts 逐条统计，
      // 只改 provider 级等于没改 —— 头卡已经 ✓、环上分子仍是旧值，要等下一轮轮询。
      const acctRow = ((p && p.accounts) || []).find((x) => (x.account_key || "") === acctKey);
      if (acctRow) {
        acctRow.status = res.status;
        acctRow.points = res.points || acctRow.points;
        acctRow.message = res.message;
        acctRow.total_points = res.total_points;
      }
      // 签到期间轮询可能已整体重建过 #stack（P3-6）：终态要落在屏幕上当前那张卡，
      // 而不是被替换掉的旧节点 —— 否则勾/叉动画和文案都看不见。
      const liveCard = $$(`#stack .svc[data-key="${key}"]`).find(
        (el) => (el.dataset.acct || "") === acctKey) || svcEl;
      const liveBtn = $(".ckbtn", liveCard) || btn;
      busyKeys.delete(key);
      markSvcResult(liveCard, res);
      // 签完立刻按新状态重排：成功的收进牌堆，失败的留在展开区
      stackLayout();
      // 终态：成功/已签 → 勾；失败 → 叉。两者都先摘 busy 再上终态，
      // 这样弧线动画是「接着转完再变」，不是被硬切。
      liveBtn.classList.remove("busy");
      if (res.status === "fail") {
        liveBtn.classList.add("fail");
        toast(`${LABELS[key] || key} 失败：${res.message || ""}`, 3600);
      } else {
        liveBtn.classList.add("done");
        setTimeout(() => burst(liveBtn), 120);
        if (key === "qoder") {
          // Qoder 是每日 10:00 刷新的积分活动，不是「签到」：直接把后端那句
          // 「已领取 +100 Credits · 每日 10:00（UTC+8）刷新」透出来（用户 m08264 第 ② 条）。
          toast(res.message || "已领取 · 每日 10:00（UTC+8）刷新", 3600);
          if (res.status === "success") { confettiBurst(false); feedPet(true); }
        } else {
          toast(`${LABELS[key] || key} 签到成功${res.points ? " " + res.points : ""}`);
          if (res.status === "success") { confettiBurst(false); feedPet(true); }
        }
      }
      refreshProgress();
    } catch (err) {
      clearWatchdog();
      busyKeys.delete(key);
      const liveCard = $$(`#stack .svc[data-key="${key}"]`).find(
        (el) => (el.dataset.acct || "") === acctKey) || svcEl;
      const liveBtn = $(".ckbtn", liveCard) || btn;
      liveBtn.classList.remove("busy");
      liveBtn.classList.add("fail");
      toast("签到失败：" + err.message, 3200);
    }
    clearWatchdog();
  }

  function markSvcResult(svcEl, res) {
    svcEl.classList.remove("t-success", "t-fail", "t-already");
    if (res.status === "success") svcEl.classList.add("t-success");
    if (res.status === "already") svcEl.classList.add("t-already");
    if (res.status === "fail") svcEl.classList.add("t-fail");
    const pts = $(".svc-points", svcEl);
    if (pts) {
      // 同 svcPointsHTML：null 不能当 0。
      // Qoder 的 total_points 是 "100 Credits"、MiniMax 是 "连签 1 天" 这类带单位的字符串，
      // Number() 会变 NaN —— 那种情况直接原样显示，别退化成只剩「今日 +N」。
      const rawTp = res.total_points;
      const empty = (rawTp === null || rawTp === undefined || rawTp === "");
      const total = empty ? NaN : Number(rawTp);
      if (isFinite(total) && total >= 0) {
        pts.innerHTML = fmtNum(total) + (res.points ? `<i class="svc-delta">${escapeHtml(res.points)}</i>` : "");
        pts.classList.add("has");
      } else if (!empty && typeof rawTp === "string" && rawTp.trim()) {
        pts.innerHTML = escapeHtml(rawTp.trim()) + (res.points ? `<i class="svc-delta">${escapeHtml(res.points)}</i>` : "");
        pts.classList.add("has");
      } else {
        pts.textContent = res.points || "";
        pts.classList.toggle("has", !!res.points);
      }
    }
    const sub = $(".svc-sub", svcEl);
    if (sub) {
      // 与 renderStack 同一口径（svcSubText）：Qoder 说「已领取 · 每日 10:00 刷新」，
      // 其余服务统一「今日已签到 / 请签到」。
      // 原始 message 挂到 title 上，桌面端悬停仍能看到具体原因，不占卡片空间。
      const key = svcEl.dataset.key || "";
      // 只改内层文本节点：sub 里还有 <span class="svc-acct">账号 N</span>
      // （多账号徽章）与 <span class="svc-subtxt">，整体赋值 textContent 会把徽章一起抹掉，
      // 表现为「同一组卡里只有被点的那张没有编号」，直到 25 秒轮询重绘才恢复
      // （BUGS-v2.63-290 P3-1）。没有内层节点时再退回整体赋值，兼容旧骨架。
      const txt = $(".svc-subtxt", sub);
      const text = svcSubText({ key: key, status: res.status, message: res.message });
      if (txt) txt.textContent = text; else sub.textContent = text;
      if (res.message) sub.title = res.message; else sub.removeAttribute("title");
    }
  }

  /**
   * 首页进度环。
   *
   * 用户 m11566（截图）：首页明明是 6 张卡（WorkBuddy 2 个账号叠着、Trae 2 个、
   * Qoder 1 个、MiniMax 2 个），环上却写「2 / 4 已签」「再签 2 个就满勤啦」——
   * 因为以前统计的是 `providers` 数组（一家一条），而卡片是按账号出的（`accounts`）。
   * 多账号一开，分母永远是 4，分子永远是「几家成功」，跟眼睛看到的卡片数完全脱节。
   *
   * 现在按账号统计：有 accounts 明细就逐账号算，没有就退回 provider 自己（单账号）。
   * 已禁用的服务不参与（卡片上标「已禁用」，不该拉低满勤率）。
   */
  function refreshProgress() {
    const list = (state.status && state.status.providers) || [];
    let done = 0, total = 0;
    list.forEach((p) => {
      if (p.enabled === false) return;
      const accts = (Array.isArray(p.accounts) && p.accounts.length) ? p.accounts : [p];
      total += accts.length;
      accts.forEach((a) => {
        const st = (a && a.status) || "idle";
        if (st === "success" || st === "already") done++;
      });
    });
    setProgress(done, total);
  }

  /* ============================================================
     ④ 最近 7 天：吸附 + 画线
     ============================================================ */
  let weekPos = 0, weekEls = [], weekDays = [], weekSelected = 0;
  const WEEK_WD = ["日", "一", "二", "三", "四", "五", "六"];
  function parseYMD(s) { const p = String(s).split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function fmtYMD(dt) {
    return dt.getFullYear() + "-" + String(dt.getMonth() + 1).padStart(2, "0") + "-" + String(dt.getDate()).padStart(2, "0");
  }
  function addDays(dt, n) { const x = new Date(dt.getTime()); x.setDate(x.getDate() + n); return x; }
  function renderWeek() {
    const track = $("#weekTrack");
    const real = (state.status && state.status.week) || [];   // 真实 7 天（旧→新，最后一天=今天）
    let days = real.map((d) => ({ day: d.day, wd: d.weekday, ok: !!d.ok, future: false }));
    if (days.length) {
      // 补齐「今天之后」的日期，右侧不再空白；多补一些也保证宽屏能铺满
      const last = parseYMD(days[days.length - 1].day);
      for (let k = 1; k <= 12; k++) {
        const nd = addDays(last, k);
        days.push({ day: fmtYMD(nd), wd: WEEK_WD[nd.getDay()], ok: false, future: true });
      }
    }
    weekDays = days;
    const todayIdx = real.length - 1;
    weekSelected = todayIdx >= 0 ? todayIdx : 0;
    track.innerHTML = days.map((d, i) => {
      const isToday = i === todayIdx;
      return `<div class="day ${d.ok ? "ok" : ""} ${isToday ? "today" : ""} ${d.future ? "future" : ""}" data-i="${i}">
        <span class="wd">周${d.wd}</span><span class="dt">${d.day.slice(8)}</span><span class="dd"></span></div>`;
    }).join("");
    weekEls = $$(".day", track);
    // 每一格都可以点：点哪格就把哪格居中选中
    weekEls.forEach((el, i) => {
      el.style.cursor = "pointer";
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        if (window.__weekDragged) { window.__weekDragged = false; return; }
        snapWeek(i, true);
      });
    });
    requestAnimationFrame(() => snapWeek(weekSelected, true));
  }
  const DAY_W = 64, DAY_GAP = 7, DAY_STEP = DAY_W + DAY_GAP * 2;
  function weekFirstCenter() { return $("#weekRail").clientWidth / 2; }
  function weekCenterOf(i) { return DAY_GAP + DAY_W / 2 + i * DAY_STEP; }
  function weekApply(p, tr) {
    const track = $("#weekTrack");
    track.style.transition = tr ? "transform .5s cubic-bezier(.22,1.1,.32,1)" : "none";
    track.style.transform = `translateX(${p}px)`;
  }
  /* 位移要「整格对齐」：让 rail 左边缘永远落在两格之间的间隙上，而不是按格子中心算。
     按中心算的话（旧写法 weekPos = weekFirstCenter() - weekCenterOf(i)），19 格里今天
     在第 6 格，位移 -334px 会让第 0..5 格整体跑到 rail 左侧外面，左边缘留下半个圆角
     卡片边缘（用户截图里那个空框就是这么来的）。改成取 DAY_STEP 的整数倍后，左边缘
     对齐到间隙，第一个可见格子必然是完整的。今天会偏离正中最多半格（≈39px），肉眼
     几乎看不出，但不再出现半格。 */
  function snapWeek(i, draw) {
    weekSelected = i;
    const ideal = weekFirstCenter() - weekCenterOf(i);
    const k = clamp(Math.round(-ideal / DAY_STEP), 0, Math.max(0, weekEls.length - 1));
    weekPos = -k * DAY_STEP;
    weekApply(weekPos, true);
    weekEls.forEach((el, idx) => el.classList.toggle("active", idx === i));
    if (draw) drawWeekLine(i);
  }
  function drawWeekLine(i) {
    const svg = $("#weekLine"), W = $("#weekRail").clientWidth, y = 11;
    svg.setAttribute("viewBox", `0 0 ${W} 22`);
    svg.innerHTML = `<path d="M8,${y} C ${W * 0.35},${y - 7} ${W * 0.65},${y + 7} ${W - 8},${y}"/>`;
    const path = svg.querySelector("path");
    const L = path.getTotalLength();
    path.style.strokeDasharray = L;
    path.style.strokeDashoffset = NOANIM ? 0 : L;
    if (NOANIM) return;
    const dur = 420 + i * 90, start = performance.now();
    (function step(now) {
      const t = clamp((now - start) / dur, 0, 1);
      path.style.strokeDashoffset = L * (1 - (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    })(performance.now());
  }
  (function bindWeekDrag() {
    const track = $("#weekTrack"), rail = $("#weekRail");
    let dragging = false, startX = 0, startPos = 0, lastX = 0, vx = 0, lastT = 0;
    function nearest(p) {
      const c = rail.clientWidth / 2 - p;
      return clamp(Math.round((c - DAY_GAP - DAY_W / 2) / DAY_STEP), 0, weekEls.length - 1);
    }
    function rubber(p) {
      const n = weekEls.length;
      const maxP = 0;                       // k=0：第 0 格贴左（左边缘落在它前面的间隙里）
      const minP = -(n - 1) * DAY_STEP;      // k=n-1：最后一格贴左
      if (p > maxP) return maxP + (p - maxP) * 0.3;
      if (p < minP) return minP + (p - minP) * 0.3;
      return p;
    }
    track.addEventListener("pointerdown", (e) => {
      dragging = true; startX = lastX = e.clientX; startPos = weekPos;
      lastT = performance.now(); vx = 0; window.__weekDragged = false;
      track.setPointerCapture && track.setPointerCapture(e.pointerId);
    });
    track.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX, now = performance.now();
      if (Math.abs(dx) > 6) window.__weekDragged = true;
      vx = (e.clientX - lastX) / Math.max(now - lastT, 1);
      lastX = e.clientX; lastT = now;
      weekApply(rubber(startPos + dx), false);
      const i = nearest(rubber(startPos + dx));
      weekEls.forEach((el, k) => el.classList.toggle("active", k === i));
    });
    function up() {
      if (!dragging) return; dragging = false;
      let target = nearest(weekPos);
      if (Math.abs(vx) > 0.5) target += (vx > 0 ? -1 : 1) * Math.min(2, Math.round(Math.abs(vx) * 6));
      snapWeek(clamp(target, 0, weekEls.length - 1), true);
    }
    track.addEventListener("pointerup", up);
    track.addEventListener("pointercancel", up);
    track.addEventListener("pointerleave", () => { if (dragging) up(); });
  })();

  /* ============================================================
     ⑤ 惯性滚轮
     ============================================================ */
  function createWheel(el, count, onChange) {
    const ITEM = 50, pad = 1, total = count + pad * 2;
    const strip = document.createElement("div");
    strip.className = "wheel-strip";
    for (let i = 0; i < total; i++) {
      const item = document.createElement("div");
      item.className = "wheel-item";
      const real = i - pad;
      item.textContent = real >= 0 && real < count ? String(real).padStart(2, "0") : "";
      if (real < 0 || real >= count) item.style.opacity = "0";
      strip.appendChild(item);
    }
    el.innerHTML = ""; el.appendChild(strip);
    let selected = pad, pos = 0, dragging = false, lastY = 0, lastT = 0, vel = 0, raf = null;
    const targetPos = (idx) => 75 - ((pad + idx) * ITEM + ITEM / 2);
    function highlight(p) {
      const center = (75 - p) / ITEM - 0.5;
      const k = clamp(Math.round(center), 0, total - 1);
      $$(".wheel-item", strip).forEach((it, j) => it.classList.toggle("on", j === k));
    }
    function setPos(p, tr) { strip.style.transition = tr || "none"; strip.style.transform = `translateY(${p}px)`; highlight(p); }
    function commit() {
      const center = (75 - pos) / ITEM - 0.5;
      const idx = clamp(Math.round(center), pad, pad + count - 1);
      selected = idx - pad;
      pos = targetPos(selected);
      strip.style.transition = "transform .42s cubic-bezier(.2,.9,.25,1.08)";
      strip.style.transform = `translateY(${pos}px)`; highlight(pos);
      if (onChange) onChange(selected);
    }
    const top = () => targetPos(count - 1), bottom = () => targetPos(0);
    function rubber(p) {
      if (p < top()) return top() + (p - top()) * 0.32;
      if (p > bottom()) return bottom() + (p - bottom()) * 0.32;
      return p;
    }
    function momentum() {
      cancelAnimationFrame(raf);
      (function frame() {
        vel *= 0.94; pos += vel;
        if (Math.abs(vel) < 0.5) { commit(); return; }
        if (pos < top()) { pos = top() + (pos - top()) * 0.55; vel *= 0.6; }
        if (pos > bottom()) { pos = bottom() + (pos - bottom()) * 0.55; vel *= 0.6; }
        setPos(pos, "none");
        raf = requestAnimationFrame(frame);
      })();
    }
    el.addEventListener("pointerdown", (e) => {
      // 锁定时完全不响应拖动：让用户明确「先点设置定时」，
      // 避免滑动滚轮误改时间（原来一碰就变）。
      if (state.wheelLocked) return;
      dragging = true; cancelAnimationFrame(raf);
      lastY = e.clientY; lastT = performance.now(); vel = 0;
      el.setPointerCapture && el.setPointerCapture(e.pointerId);
      strip.style.transition = "none";
    });
    el.addEventListener("pointermove", (e) => {
      if (!dragging || state.wheelLocked) return;
      const dy = e.clientY - lastY, now = performance.now();
      vel = (dy / Math.max(now - lastT, 1)) * 16;
      lastY = e.clientY; lastT = now;
      pos = rubber(pos + dy); setPos(pos, "none");
    });
    function up() { if (!dragging) return; dragging = false; if (Math.abs(vel) > 0.6) momentum(); else commit(); }
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("pointerleave", () => { if (dragging) up(); });
    pos = targetPos(pad); setPos(pos, "none");
    return {
      get: () => selected,
      setTo: (v) => { selected = clamp(v, 0, count - 1); pos = targetPos(selected); setPos(pos, "transform .3s ease"); if (onChange) onChange(selected); },
    };
  }
  /* 滚轮锁定：默认锁住，点「设置定时」才解锁，避免误滑改掉时间。
     解锁后按钮变成「取消」，再点一次又锁上。 */
  function applyWheelLock(locked) {
    state.wheelLocked = !!locked;
    const box = $("#timeWheelBox");
    if (box) box.classList.toggle("locked", state.wheelLocked);
    const lock = $("#twLock");
    if (lock) lock.hidden = !state.wheelLocked;
    const btn = $("#editSched");
    if (btn) {
      btn.textContent = state.wheelLocked ? "设置定时" : "取消修改";
      btn.classList.toggle("editing", !state.wheelLocked);
    }
    const save = $("#saveSched");
    if (save) save.disabled = state.wheelLocked;
    // 随机浮动滑块跟滚轮同一把锁：锁定态禁用拖动并灰化，解锁后可调
    const jf = $("#jitterField");
    if (jf) jf.classList.toggle("locked", state.wheelLocked);
    const jel = $("#schedJitter");
    if (jel) jel.disabled = state.wheelLocked;
  }

  function initWheels() {
    if (state.wheelHour) return;
    const disp = $("#twDisplay");
    function onChange() {
      const h = String(state.wheelHour.get()).padStart(2, "0");
      const m = String(state.wheelMin.get()).padStart(2, "0");
      disp.textContent = `${h}:${m}`;
    }
    state.wheelHour = createWheel($("#hourWheel"), 24, onChange);
    state.wheelMin = createWheel($("#minWheel"), 60, onChange);
    const t = (state.status && state.status.scheduler && state.status.scheduler.times[0]) || "08:30";
    const [hh, mm] = t.split(":").map((x) => parseInt(x, 10));
    state.wheelHour.setTo(isNaN(hh) ? 8 : hh);
    state.wheelMin.setTo(isNaN(mm) ? 30 : mm);
    onChange();
    applyWheelLock(true);          // 默认锁定（滚轮 + 随机浮动一起），需要点「设置定时」才解锁
    $("#autoOn").checked = !!(state.status && state.status.scheduler && state.status.scheduler.enabled);
    const jit = (state.status && state.status.scheduler && state.status.scheduler.jitter_min);
    const jv = Math.max(0, Math.min(240, jit == null ? 30 : parseInt(jit, 10) || 0));
    paintJitter(jv);
  }

  /* ============================================================
     记录页头图：日历（标出本月已签到日 + 今天）
     ============================================================ */
  function renderCalHero() {
    const grid = $("#calGrid");
    if (!grid) return;
    const st = state.status || {};
    const hasDay = typeof st.day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(st.day);
    const d = hasDay ? new Date(st.day + "T00:00:00") : new Date();
    const y = d.getFullYear(), m = d.getMonth(), today = d.getDate();
    const firstDow = new Date(y, m, 1).getDay();      // 0=周日
    const days = new Date(y, m + 1, 0).getDate();
    // 已签到日：从 week 序列里取当月且 ok 的日期
    const okDays = new Set();
    (st.week || []).forEach((w) => {
      if (w && w.ok && typeof w.day === "string" && w.day.slice(0, 7) === st.day.slice(0, 7)) {
        okDays.add(parseInt(w.day.slice(8), 10));
      }
    });
    if (st.status && st.status.providers) { /* noop：占位，保持结构可扩展 */ }
    const moEl = $("#calMonth"); if (moEl) moEl.textContent = (m + 1) + "月";
    const yrEl = document.querySelector(".cal-yr"); if (yrEl) yrEl.textContent = String(y);
    let html = "";
    for (let i = 0; i < firstDow; i++) html += `<span class="cal-d wd"></span>`;
    for (let day = 1; day <= days; day++) {
      let cls = "";
      if (okDays.has(day)) cls += "on ";
      if (day === today) cls += "today ";
      html += `<span class="cal-d ${cls.trim()}">${day}</span>`;
    }
    grid.innerHTML = html;
  }

  /* ============================================================
     ⑦ 吸顶大标题：滚动视差 / 压暗 / 标题飞入 + 下拉放大 + 松手吸附
     ============================================================ */
  const FOLD = 160;         // 折叠阈值(px)：大标题飞入 + 头图压暗的滚动行程（越大越"慢慢"）
  const PULL_MAX = 96;      // 下拉最大放大位移(px)
  const PULL_RESIST = 0.52; // 下拉阻尼
  const scr = {
    hero: null, head: null, img: null, title: null, sub: null, kicker: null, shade: null,
    fly: null, name: "",
    progress: 0, pull: 0, raf: 0,
    mFrom: null, mTo: null, metricsDirty: true,
  };
  const appbarEl = () => $("#appbar");

  // 固定层「飞行标题」节点（脱离头图 overflow 裁切，才能横向飞进导航栏）
  function ensureFlyTitle() {
    if (scr.fly) return scr.fly;
    const el = document.createElement("div");
    el.className = "fly-title";
    el.setAttribute("aria-hidden", "true");
    document.body.appendChild(el);
    scr.fly = el;
    return el;
  }

  function activeHero() {
    const view = $$(".view").find((v) => !v.hidden);
    if (!view) return null;
    return view.querySelector(".hero");
  }
  function cacheHero() {
    scr.hero = activeHero();
    scr.head = scr.hero ? scr.hero.querySelector(".hero-head") : null;
    scr.img = scr.hero ? scr.hero.querySelector(".hero-img") : null;
    scr.title = scr.hero ? scr.hero.querySelector(".hero-title") : null;
    scr.sub = scr.hero ? scr.hero.querySelector(".hero-sub") : null;
    scr.kicker = scr.hero ? scr.hero.querySelector(".hero-kicker") : null;
    scr.shade = scr.hero ? scr.hero.querySelector(".hero-shade") : null;
    scr.abt = $("#viewTitle");
    scr.name = scr.title ? scr.title.textContent.trim() : "";
    const fly = ensureFlyTitle();
    if (fly) fly.textContent = scr.name;
    // 在 scrollY=0 时测量起点，记录「绝对文档坐标」，滚动时随头图自然上移
    measureFly();
  }
  // 起点：头图里大标题的「文字实际矩形」（左边缘 + 垂直中心，绝对坐标）；终点：导航栏正中
  function measureFly() {
    scr.metricsDirty = false;
    if (!scr.title) { scr.mFrom = null; return; }
    let w = scr.title.getBoundingClientRect().width;
    try {
      const rg = document.createRange(); rg.selectNodeContents(scr.title);
      const tw = rg.getBoundingClientRect().width; if (tw) w = tw;
    } catch (_) {}
    const tr = scr.title.getBoundingClientRect();
    if (!tr.width) { scr.mFrom = null; return; }
    scr.mFrom = { left: tr.left, topAbs: tr.top + (window.scrollY || 0), w: w, h: tr.height };
    const bar = appbarEl();
    const br = bar ? bar.getBoundingClientRect() : null;
    // 落点 = 导航栏「内容区」正中（要扣掉状态栏 padding-top）
    const barTop = br ? br.top : 0;
    const inset = br ? (br.height - 56) / 2 : 0;   // --barh:56px，差值即状态栏高度
    scr.mTo = { cx: br ? br.left + br.width / 2 : window.innerWidth / 2, cy: barTop + inset + 28 };
  }

  function renderHero() {
    const p = scr.progress, pull = scr.pull;
    const bar = appbarEl();
    const scrolled = (window.scrollY > 8) || p >= 0.98;
    if (bar) bar.classList.toggle("frosted", scrolled);
    // 提示条（#hint）停在导航栏那一条线上，滚动后标题飞进栏内会被压住 —— 同步把根节点标上 scrolled，
    // CSS 里让提示条顺势下移到栏下方。
    document.documentElement.classList.toggle("scrolled", scrolled);

    // 0.5× 速度视差 + 下拉放大
    if (scr.img) {
      const ty = -p * FOLD * 0.5;
      scr.img.style.transform = `translate3d(0,${ty.toFixed(1)}px,0) scale(${(1 + pull / 300).toFixed(3)})`;
    }
    if (scr.shade) scr.shade.style.opacity = String(Math.min(1, p * 1.05 + pull / 500));
    // 副标题（日期）+ 小标签 随滚动淡出
    const softOp = String(Math.max(0, 1 - p * 2.6));
    if (scr.sub) scr.sub.style.opacity = softOp;
    if (scr.kicker) scr.kicker.style.opacity = softOp;

    // 大标题：从左 -> 中、从大 -> 小，飞进导航栏正中
    const fly = scr.fly;
    let flying = false;
    if (p > 0.001 && fly) {
      if (scr.metricsDirty || !scr.mFrom) measureFly();
      if (scr.mFrom) {
        if (scr.title) scr.title.style.opacity = "0";        // 原文字隐去，避免重影
        const e = p * p * (3 - 2 * p);                        // easeInOut
        const fromLeft = scr.mFrom.left;                     // 左边缘
        const fromCy = scr.mFrom.topAbs - (window.scrollY || 0) + scr.mFrom.h / 2;
        const s = 1 - (1 - 16 / 38) * e;                      // 38px -> 16px
        const curW = scr.mFrom.w * s;                         // 当前文字宽
        const targetLeft = scr.mTo.cx - curW / 2;             // 使文字中心落到导航栏正中
        const left = fromLeft + (targetLeft - fromLeft) * e;  // 左边缘从 18 -> 居中
        const cy = fromCy + (scr.mTo.cy - fromCy) * e;
        fly.style.left = left.toFixed(1) + "px";
        fly.style.top = cy.toFixed(1) + "px";
        fly.style.transform = `translate(0,-50%) scale(${s.toFixed(3)})`;
        // 近末端交叉溶解到导航栏原生（清晰的）居中标题
        const hand = clamp((p - 0.9) / 0.09, 0, 1);
        fly.style.opacity = String(clamp(p * 5, 0, 1) * (1 - hand));
        if (scr.abt) scr.abt.style.opacity = String(hand);
        flying = true;
      }
    }
    if (!flying) {
      if (fly) fly.style.opacity = "0";
      if (scr.title) scr.title.style.opacity = "1";
      if (scr.abt) scr.abt.style.opacity = "0";
    }
  }
  function updateScroll() {
    const y = window.scrollY || 0;
    scr.progress = Math.max(0, Math.min(1, y / FOLD));
    renderHero();
  }
  let scrollQueued = false;
  function onScroll() {
    if (NOANIM) return;
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => { scrollQueued = false; updateScroll(); });
  }
  // 当前滚动顶（用于下拉判断：首屏且未滚动时才允许放大）
  function atTop() { return (window.scrollY || 0) <= 0; }

  // 下拉放大 + 松手吸附（速度优先 -> 收到底 / 弹回，绝不停半路）
  // 同一套手势兼做「全站下拉刷新余额」：拉得浅 = 原样（放大头图 / 收到底），
  // 拉过 REF_TH 就转为刷新余额，不再滚动。
  (function bindPull() {
    let startY = 0, startX = 0, pulling = false, lastTs = 0, lastPull = 0, vel = 0;
    const TH = 62;
    const REF_TH = 84;          // 触发刷新余额的下拉量（scr.pull 已阻尼，对应手指约 160px）
    const ptr = () => $("#ptr");
    const ptrText = () => $("#ptrText");
    let ptrState = "idle";      // idle | dragging | armed | busy | ok | fail

    function paintPtr(show, ratio) {
      const el = ptr();
      if (!el) return;
      const t = ptrText();
      if (!show) {
        el.style.setProperty("--ptr-o", "0");
        el.style.setProperty("--ptr-y", "0px");
        el.style.setProperty("--ptr-s", ".9");
        return;
      }
      el.style.setProperty("--ptr-o", String(Math.min(1, 0.35 + ratio * 0.9)));
      el.style.setProperty("--ptr-y", (Math.min(1, ratio) * 26).toFixed(1) + "px");
      el.style.setProperty("--ptr-s", String(0.9 + Math.min(1, ratio) * 0.1));
      if (ptrState === "dragging" || ptrState === "armed") {
        el.classList.toggle("armed", ratio >= 1);
        if (t) t.textContent = ratio >= 1 ? "松手刷新余额" : "下拉刷新余额";
      }
    }
    function setPtrState(s, text) {
      const el = ptr();
      if (!el) return;
      ptrState = s;
      el.classList.remove("armed", "busy", "ok", "fail", "dragging");
      if (s !== "idle") el.classList.add(s);
      if (text !== undefined && ptrText()) {
        const t = ptrText();
        t.textContent = text;
        // 与全站提示条同一套动画：从中间向两侧扩散 + 由模糊转清晰
        if (!NOANIM) {
          t.classList.remove("pop", "vanish");
          void t.offsetWidth;
          t.classList.add("pop");
        }
      }
      if (s === "idle") { el.style.setProperty("--ptr-o", "0"); el.style.setProperty("--ptr-y", "0px"); }
      else {
        el.style.setProperty("--ptr-o", "1");
        el.style.setProperty("--ptr-y", "26px");
        el.style.setProperty("--ptr-s", "1");
      }
    }
    function resetPtrLater(ms) {
      window.setTimeout(() => {
        if (ptrState === "ok" || ptrState === "fail") {
          // 结果提示退场也走「雾化上飘」那一套，和全站提示条一致
          const t = ptrText();
          if (t && !NOANIM) {
            t.classList.remove("pop");
            void t.offsetWidth;
            t.classList.add("vanish");
            window.setTimeout(() => { t.classList.remove("vanish"); }, 640);
          }
          setPtrState("idle");
        }
      }, ms);
    }

    async function doRefresh() {
      if (ptrState === "busy") return;
      setPtrState("busy", "正在刷新余额…");
      let ok = false;
      try {
        ok = await refreshBalances({ silent: true });
      } catch (_) {
        ok = false;
      }
      setPtrState(ok ? "ok" : "fail", ok ? "余额已更新" : "刷新失败");
      resetPtrLater(760);              // 结果只做一次确认提示，别让指示器杵在那儿
    }

    document.addEventListener("touchstart", (e) => {
      if (NOANIM || !atTop() || e.touches.length !== 1) { pulling = false; return; }
      const container = e.target.closest(".view");
      if (!container || !container.querySelector(".hero")) { pulling = false; return; }
      startY = e.touches[0].clientY; startX = e.touches[0].clientX; pulling = true;
      lastTs = performance.now(); lastPull = 0; vel = 0;
    }, { passive: true });
    document.addEventListener("touchmove", (e) => {
      if (!pulling) return;
      const dy = e.touches[0].clientY - startY;
      const dx = e.touches[0].clientX - startX;
      if (dy <= 0 || Math.abs(dx) > dy * 1.2) { pulling = false; return; }
      if (!atTop()) { pulling = false; return; }
      e.preventDefault();               // 阻止原生回弹/滚动，交给我们
      const now = performance.now();
      const target = Math.min(PULL_MAX, dy * PULL_RESIST);
      scr.pull = target;
      vel = (target - lastPull) / Math.max(1, now - lastTs); // px/ms
      lastTs = now; lastPull = target;
      // 下拉指示器：超过 PULL_MAX 的 8% 才露头，避免轻扫就闪
      if (target > PULL_MAX * 0.08) {
        hideHint();                     // 提示条和指示器共用一个位置，别叠在一起
        const el = ptr();
        if (el) {
          // 上一轮的 ok/fail 残留必须先摘掉，否则新一轮拖动会顶着绿色的「余额已更新」
          if (ptrState !== "dragging") el.classList.remove("ok", "fail", "busy");
          el.classList.add("dragging");
        }
        ptrState = "dragging";
        paintPtr(true, target / REF_TH);
      }
      cancelAnimationFrame(scr.raf);
      scr.raf = requestAnimationFrame(renderHero);
    }, { passive: false });
    function endPull() {
      if (!pulling) return;
      pulling = false;
      const pulled = scr.pull;
      const el = ptr();
      if (el) el.classList.remove("dragging");
      // 拉过刷新阈值：优先刷新余额，不做「收到底」
      if (pulled >= REF_TH) {
        scr.pull = 0;
        cancelAnimationFrame(scr.raf);
        scr.raf = requestAnimationFrame(renderHero);
        doRefresh();
        return;
      }
      // 速度优先：快速下拉(>0.35px/ms)或超过阈值一半 -> 收到底；否则弹回
      const toOpen = vel > 0.35 || pulled > TH / 2;
      scr.pull = 0;
      if (toOpen) {
        // 收到底：折叠英雄+吸顶栏，滚到刚好折叠位置
        const target = Math.max(FOLD, 1);
        try { window.scrollTo({ top: target, behavior: NOANIM ? "auto" : "smooth" }); } catch (_) { window.scrollTo(0, target); }
      }
      cancelAnimationFrame(scr.raf);
      scr.raf = requestAnimationFrame(renderHero);
      if (ptrState === "dragging" || ptrState === "armed") setPtrState("idle");
    }
    document.addEventListener("touchend", endPull, { passive: true });
    document.addEventListener("touchcancel", endPull, { passive: true });
  })();

  /* ============================================================
     ⑥ 圆形扩散转场
     ============================================================ */
  const views = { home: "#viewHome", logs: "#viewLogs", stats: "#viewStats", settings: "#viewSettings", prefs: "#viewPrefs" };
  let switching = false;
  let switchTimer = 0;
  const VIEW_TITLES = { home: "签到中心", logs: "记录", stats: "统计", settings: "配置", prefs: "设置" };
  function setDocTitle(name) {
    const t = VIEW_TITLES[name] || VIEW_TITLES.home;
    try { document.title = t; } catch (_) {}
    const h = $("#viewTitle");
    if (h) h.textContent = t;
  }

  /**
   * 当前页重复点同一个 tab → 就地刷新（用户 m14411）。
   *
   * 原来这里直接 return，用户点第二下完全没反应，像卡住了。
   * 刷新只重跑该页自己的渲染与数据拉取，不播转场动画、不滚回顶部
   * （用户可能正看到一半，把他弹回顶部比不刷新更烦）。
   */
  function refreshCurrentView(name) {
    if (name === "home") {
      loadAll({ light: true });
      loadAssets(false);
      if (state.growthLoaded) loadGrowth(false);
    } else if (name === "logs") {
      renderCalHero();
      renderLogs();
    } else if (name === "stats") {
      renderStatsView();
    } else if (name === "settings") {
      renderProviderEditors();
      renderTokenHealth();
      loadAutoDiag();
    } else if (name === "prefs") {
      // 设置页（用户 m15092）：使用说明 / 后台可靠性 / 外观 / 水滴宠物 / 关于
      renderGuide();
      renderBgPanel();
      renderThemeGrids();
      renderPetCard();
      renderVersion();
    }
  }

  function switchView(name, x, y) {
    const target = $(views[name]);
    if (!target) return;
    // 记住当前 tab：WebView 被系统回收 / 登录页回来整页重载后，能回到原来那一页
    // （否则从「网页登录 Qoder」返回时会莫名其妙跳回首页，用户以为数据丢了）
    try { localStorage.setItem("signhub_view", name); } catch (_) {}
    const cur = $$(".view").find((v) => !v.hidden);
    if (cur === target) {
      // 重复点当前 tab = 刷新当前页（用户 m14411）
      setDocTitle(name);
      refreshCurrentView(name);
      return;
    }
    setDocTitle(name);

    // 记下方向：新 tab 在原 tab 右边就向左推
    const order = ["home", "logs", "stats", "settings", "prefs"];
    const prevKey = cur ? cur.id.replace(/^view/, "").toLowerCase() : "home";
    const dir = order.indexOf(name) >= order.indexOf(prevKey) ? 1 : -1;
    // 设置页没有自己的 tab：胶囊与图标都停在它的父页「配置」上，用户不会看到胶囊凭空消失
    const pillKey = name === "prefs" ? "settings" : name;

    // 胶囊与图标状态必须无条件先更新 —— 上一轮动画没播完时也要立刻跟随手指，
    // 否则连点会「按了没反应」。这是「不跟手」的主因。
    movePill(pillKey, NOANIM);
    popIcon(pillKey);
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === pillKey));
    // 齿轮只在非配置页出现；切 tab 时退出资产宫格（用户 m14411）
    syncGear(name);
    exitAssetMulti();

    if (NOANIM) {
      $$(".view").forEach((v) => (v.hidden = v !== target));
      window.scrollTo({ top: 0 });
      // P2R3（BUGS-v2.2）：横幅跟着当前可见 view 走，必须放在 hidden 更新之后
      refreshCredsAlert();
      if (name === "logs") renderLogs();
      if (name === "stats") renderStatsView();
      if (name === "settings") loadAutoDiag();
      if (name === "prefs") refreshCurrentView("prefs");
      scr.progress = 0; scr.pull = 0; cacheHero(); renderHero();
      return;
    }

    switching = true;

    // 内容：旧视图立即隐藏，新视图沿方向轻推淡入
    $$(".view").forEach((v) => (v.hidden = v !== target));
    window.scrollTo({ top: 0 });
    // P2R3（BUGS-v2.2）：同上，hidden 更新后把横幅搬到新 view 里
    refreshCredsAlert();

    target.classList.remove("push-left", "push-right");
    void target.offsetWidth;                     // 强制重排，让动画能重播
    target.classList.add(dir >= 0 ? "push-left" : "push-right");

    // 重内容渲染挪到下一帧，先把转场推出去 —— 统计页要画热力图/趋势/勋章，
    // 同步渲染会卡住主线程 100ms+，手感就是「点了不动」。
    requestAnimationFrame(() => {
      if (name === "logs") { renderCalHero(); renderLogs(); }
      if (name === "stats") renderStatsView();
      if (name === "settings") loadAutoDiag();
      if (name === "prefs") refreshCurrentView("prefs");
      scr.progress = 0; scr.pull = 0; cacheHero(); renderHero();
    });

    // 用可覆盖的定时器：连点时旧的收尾不会把新一轮的状态提前清掉
    clearTimeout(switchTimer);
    switchTimer = window.setTimeout(() => {
      target.classList.remove("push-left", "push-right");
      switching = false;
    }, 400);
  }

  /* ============================================================
     齿轮入口（用户 m14411）
     - 顶栏右上角那个按钮以前是「主题」，现在改成「齿轮」
     - 齿轮只出现在配置页右上角（用户原话：「设置齿轮只设置在配置页的右上角
       代替主题按键」）；其它页面顶栏保持干净，进配置页走底栏「配置」
     ============================================================ */
  /* 用户 m14954 纠正：齿轮必须「点得进去」。
     上一版理解成「只在配置页显示」，结果其它页面它被 hidden 掉，
     等于右上角没有入口。现在改成：**所有页面都显示**，点它进配置页；
     已经停在配置页时给它加 .on 表示当前就在这一页。 */
  /* 用户 m15092 再纠正：齿轮进的是**独立的「设置页」**（外观 / 水滴宠物 / 关于），
     不是底栏那个「配置」页。所以高亮状态跟随 prefs，配置页不再高亮。 */
  function syncGear(name) {
    const g = $("#gearEntry");
    if (!g) return;
    // 设置页里齿轮换成一个「←」返回键，位置相同、含义相同（都是"离开当前页"）
    g.hidden = (name === "prefs");
    g.classList.toggle("on", name === "prefs");
    g.setAttribute("aria-current", name === "prefs" ? "page" : "false");
    const b = $("#prefsBack");
    if (b) b.hidden = (name !== "prefs");
  }

  /* ============================================================
     水滴胶囊：前沿先到、后沿慢半拍 -> 中途拉长变扁 -> 弹簧回弹停稳
     - 前沿 / 后沿各用一条阻尼弹簧（前沿 K 大跑得快，后沿 K 小慢半拍）
     - 两者错位 => 过程中胶囊被拉长，再按拉伸量整体压扁
     - 领先端冒出一颗「水珠」强化流动性；总时长 400ms，收尾带轻微回弹
     ============================================================ */
  const pill = { x: 0, w: 0, raf: 0, lx: -1, lw: -1 };

  function pillGeometry(name) {
    const bar = $(".tabbar");
    const tab = $(`.tab[data-view="${name}"]`);
    if (!bar || !tab) return null;
    const br = bar.getBoundingClientRect();
    const tr = tab.getBoundingClientRect();
    const pad = Math.min(tr.width * 0.18, 26);
    return { x: tr.left - br.left + pad, w: tr.width - pad * 2 };
  }

  /** 位置未变就不写 DOM —— 每帧设置 left/width 会触发重排，是「不跟手」的隐性来源 */
  function paintPill(el, left, w, squashY) {
    if (pill.lx !== left) { el.style.left = left + "px"; pill.lx = left; }
    if (pill.lw !== w) { el.style.width = w + "px"; pill.lw = w; }
    if (squashY !== undefined) el.style.transform = `translate3d(0,50%,0) scaleY(${squashY.toFixed(3)})`;
  }

  /** 立即落位（无动画），用于首次渲染、resize 与 NOANIM */
  function movePill(name, instant) {
    const el = $("#tabPill");
    if (!el) return;
    const g = pillGeometry(name);
    if (!g) return;
    if (instant || NOANIM) {
      cancelAnimationFrame(pill.raf);
      el.classList.remove("flowing");
      el.style.transition = "";
      el.style.opacity = "1";
      paintPill(el, g.x, g.w);
      el.style.transform = "translate3d(0,50%,0) scaleY(1)";
      pill.x = g.x; pill.w = g.w;
      return;
    }

    // 终点已知就用精确目标；若 pill 尚未初始化（w<=0）则先取当前选中项几何，保证不跳
    let fromX = pill.x, fromW = pill.w;
    if (fromW <= 1) {
      const act = $$(".tab").find((t) => t.classList.contains("active"));
      const sg = act ? pillGeometry(act.dataset.view) : null;
      if (sg) { fromX = sg.x; fromW = sg.w; }
    }
    const toX = g.x, toW = g.w;
    const fromRight = fromX + fromW, toRight = toX + toW;

    el.style.opacity = "1";
    el.style.transition = "";
    el.classList.add("flowing");            // 打开领先端水珠
    cancelAnimationFrame(pill.raf);

    const DUR = 190;                       // 主流动段；+约 90ms 收尾 ≈ 总 280ms（原来 400ms，偏拖）
    const t0 = performance.now();
    const dir = (toX + toW / 2) >= (fromX + fromW / 2) ? 1 : -1;   // +1 向右移动，-1 向左

    // 增量式阻尼弹簧：逐帧推进，保留速度状态（不能每帧重算积分，否则一帧跳完）
    // 前沿快（K 大 / C 偏小）先冲出去，后沿慢半拍（K 小 / C 偏大）被落在后面。
    // 关键：按移动方向决定「前沿」是左边缘还是右边缘 —— 朝目标那一侧的边缘先走。
    // K/C 调硬一档：追赶感更强，连点时不会明显「掉在手指后面」。
    const mkSpring = (from, to, k, c) => ({ x: from - to, v: 0, k, c });
    let front, back;
    if (dir > 0) {
      front = mkSpring(fromRight, toRight, 1100, 46);   // 向右：右边缘先冲
      back  = mkSpring(fromX, toX, 520, 56);            //       左边缘慢半拍
    } else {
      front = mkSpring(fromX, toX, 1100, 46);           // 向左：左边缘先冲
      back  = mkSpring(fromRight, toRight, 520, 56);    //       右边缘慢半拍
    }

    let last = t0;

    const step = (now) => {
      const dt = Math.min((now - last) / 1000, 1 / 30);   // 限幅，避免掉帧时爆走
      last = now;

      // 子步积分，保证稳定
      const sub = 4, h = dt / sub;
      for (let i = 0; i < sub; i++) {
        front.v += (-front.k * front.x - front.c * front.v) * h;
        front.x += front.v * h;
        back.v += (-back.k * back.x - back.c * back.v) * h;
        back.x += back.v * h;
      }

      let left, right;
      if (dir > 0) { left = toX + back.x;  right = toRight + front.x; }
      else         { left = toX + front.x; right = toRight + back.x; }
      const w = Math.max(14, right - left);

      // 拉伸越多，整体越扁一点（宽度吃掉体积 → 高度补偿性压扁）
      const stretch = Math.abs(w - toW) / Math.max(toW, 1);
      const squashY = Math.max(0.8, 1 - stretch * 0.2);

      paintPill(el, Math.min(left, right), w, squashY);

      // 领先端方向：向右移动时水珠在右端，向左移动时在左端
      el.setAttribute("data-lead", dir > 0 ? "right" : "left");

      // 两个弹簧都收敛了就提前收尾，避免尾部拖沓
      const settled = Math.abs(front.x) < 0.6 && Math.abs(front.v) < 12 &&
                      Math.abs(back.x) < 0.6 && Math.abs(back.v) < 12;

      if (now - t0 < DUR && !settled) {
        pill.raf = requestAnimationFrame(step);
      } else {
        // 收尾：精确落到目标并恢复高度（90ms 轻微回弹 overshoot）
        el.classList.remove("flowing");
        el.style.transition = "left 90ms cubic-bezier(.34,1.56,.64,1), width 90ms cubic-bezier(.34,1.56,.64,1), transform 90ms cubic-bezier(.34,1.56,.64,1)";
        paintPill(el, toX, toW);
        el.style.transform = "translate3d(0,50%,0) scaleY(1)";
        window.setTimeout(() => { el.style.transition = ""; }, 110);
        pill.x = toX; pill.w = toW;
      }
    };
    pill.raf = requestAnimationFrame(step);
  }

  /** 图标先缩小再弹回 */
  function popIcon(name) {
    const tab = $(`.tab[data-view="${name}"]`);
    if (!tab) return;
    tab.classList.remove("squash");
    void tab.offsetWidth;
    tab.classList.add("squash");
    window.setTimeout(() => tab.classList.remove("squash"), 280);
  }
  $$(".tab").forEach((tab) => {
    // 触摸/鼠标按下即响应：click 在移动端有 50~100ms 的延迟（等双击判定），
    // 那一段就是「点了不跟手」的直接来源。pointerdown 先动，click 再来时已被
    // switchView 的 cur===target 早退吃掉，不会重复执行。
    let lastDown = 0;
    const fire = () => {
      const r = tab.getBoundingClientRect();
      switchView(tab.dataset.view, r.left + r.width / 2, r.top + r.height / 2 - 10);
    };
    tab.addEventListener("pointerdown", (e) => {
      if (e.button !== undefined && e.button > 0) return;   // 只认左键/单指
      lastDown = performance.now();
      fire();
    }, { passive: true });
    tab.addEventListener("click", () => {
      if (performance.now() - lastDown < 600) return;        // 刚由 pointerdown 处理过
      fire();
    });
  });

  /* ============================================================
     记录 + 统计
     ============================================================ */
  let cachedLogs = [];
  async function loadLogs() {
    try {
      const q = "/api/logs?limit=60" + (state.logFilter ? "&provider=" + state.logFilter : "");
      const d = await api(q);
      cachedLogs = d.logs || [];
    } catch (_) { cachedLogs = []; }
    return cachedLogs;
  }
  function renderLogs() {
    const box = $("#logList");
    const logs = cachedLogs;
    if (!logs.length) {
      box.innerHTML = `<div class="empty">暂无记录</div>`;
      $("#logStats").textContent = "";
      return;
    }
    const succ = logs.filter((l) => l.status === "success" || l.status === "already").length;
    $("#logStats").textContent = `成功 ${succ} / ${logs.length}`;
    box.innerHTML = logs.map((l) => {
      const sc = l.status === "fail" ? "s-fail" : (l.status === "already" ? "s-already" : "s-succ");
      return `<div class="log-item">
        <div class="li-main">
          <div class="li-top"><span class="li-prov">${escapeHtml(LABELS[l.provider] || l.provider)}</span>
            ${l.points ? `<span class="li-pts">${escapeHtml(l.points)}</span>` : ""}</div>
          <div class="li-time">${(l.created_at || "").replace("T", " ").slice(0, 19)}</div>
          <div class="li-msg">${escapeHtml(l.message || "")}</div>
        </div>
        <span class="li-badge ${sc}">${STATUS_CN[l.status] || l.status}</span></div>`;
    }).join("");
  }
  function renderStats() {
    const rows = $("#statRows");
    if (!state.stats) { rows.innerHTML = `<div class="empty">暂无统计</div>`; return; }
    const days = state.stats.month_done_days || {};
    $("#statMonth").textContent = state.stats.month || "";
    // 只画后端真正认得的那几家（电脑端 Python 后端还没有豆包），
    // 否则会多出一行永远 0 天的空统计。
    const alive = (state.status && state.status.providers) || [];
    const keys = Object.keys(LABELS).filter((k) => !alive.length || alive.some((x) => x.key === k));
    const max = Math.max(1, ...keys.map((k) => days[k] || 0));
    rows.innerHTML = keys.map((k) => {
      const n = days[k] || 0;
      return `<div class="stat-row">
        <span class="sr-name">${LABELS[k]}</span>
        <span class="sr-bar"><span class="sr-fill" style="width:${(n / max) * 100}%"></span></span>
        <span class="sr-val">${n} 天</span></div>`;
    }).join("");
  }
  $$("#logFilters .chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      $$("#logFilters .chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      state.logFilter = chip.dataset.p || "";
      await loadLogs();
      renderLogs();
    });
  });

  /* ============================================================
     签到配置编辑器
     ============================================================ */
  function headersToText(h) {
    return Object.entries(h || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  }

  /* 分词：按 shell 引号规则切分，支持 ^ / \ 续行 */
  function tokenizeCurl(text) {
    const s = String(text || "")
      .replace(/\r/g, "")
      .replace(/\\\n/g, " ")
      .replace(/\^\n/g, " ")
      .replace(/\n/g, " ");
    const out = [];
    let cur = "", q = null, i = 0;
    while (i < s.length) {
      const c = s[i];
      if (q) {
        if (c === q) { q = null; i++; continue; }
        if (c === "\\" && i + 1 < s.length) { cur += s[i + 1]; i += 2; continue; }
        cur += c; i++; continue;
      }
      if (c === "'" || c === '"') { q = c; i++; continue; }
      if (/\s/.test(c)) { if (cur) { out.push(cur); cur = ""; } i++; continue; }
      cur += c; i++;
    }
    if (cur) out.push(cur);
    return out;
  }

  /** 把 "Copy as cURL" 的内容解析成 {url, method, headers, body} */
  function parseCurlCommand(text) {
    const tk = tokenizeCurl(text);
    let url = "", method = "", body = "";
    const headers = {};
    for (let i = 0; i < tk.length; i++) {
      const t = tk[i];
      const low = t.toLowerCase();
      if (low === "curl" || low === "curl.exe" || t.startsWith("-")) {
        if (low === "-x" || low === "--request") { method = (tk[++i] || "").toUpperCase(); continue; }
        if (low === "-h" || low === "--header") {
          const h = tk[++i] || "";
          const p = h.indexOf(":");
          if (p > 0) headers[h.slice(0, p).trim()] = h.slice(p + 1).trim();
          continue;
        }
        if (low === "-b" || low === "--cookie") { headers["Cookie"] = (tk[++i] || "").trim(); continue; }
        if (low === "-d" || low === "--data" || low === "--data-raw" || low === "--data-binary" || low === "--data-urlencode") {
          const v = tk[++i] || "";
          body = body ? body + "&" + v : v;
          if (!method) method = "POST";
          continue;
        }
        // 其它 -X 形式或未知 flag
        if (low === "--compressed" || low === "-s" || low === "-k" || low === "--insecure" || low === "-i" || low === "-v") continue;
        continue;
      }
      if (/^https?:\/\//i.test(t) && !url) { url = t; continue; }
    }
    if (!method) method = body ? "POST" : "GET";
    return { url, method, headers, body };
  }
  function textToHeaders(t) {
    const out = {};
    String(t || "").split("\n").forEach((line) => {
      const i = line.indexOf(":");
      if (i > 0) {
        const k = line.slice(0, i).trim();
        const v = line.slice(i + 1).trim();
        if (k) out[k] = v;
      }
    });
    return out;
  }

  /* 折叠容器高度过渡：open=true 展开到内容高，false 收回到 0。
     展开结束后把 height 复位成 auto，这样内部内容变化（切换接口/浏览器模式、
     出现报错提示）时高度不会卡死在旧值上。 */
  function foldToggle(box, open) {
    if (!box) return;
    // 连点保护：先把上一轮挂的回调摘掉，否则「开→关」时上一轮的 transitionend
    // 会在关闭动画结束时把 height 复位成 auto，等于又把面板顶开了。
    if (box._foldDone) {
      box.removeEventListener("transitionend", box._foldDone);
      box._foldDone = null;
    }
    if (box._foldTimer) { clearTimeout(box._foldTimer); box._foldTimer = null; }
    // 起点取实时渲染高度：过渡中连点也能从当前位置平滑接上，不会跳一下
    const from = box.getBoundingClientRect().height;
    box.style.height = "auto";              // 量内容真实高度
    const target = box.offsetHeight;
    box.style.height = from + "px";         // 回到起点
    void box.offsetHeight;                  // 强制回流，让过渡有起点
    if (!open) { box.style.height = "0px"; return; }
    box.style.height = target + "px";
    const done = (e) => {
      if (e && e.target !== box) return;
      box.style.height = "auto";            // 展开后复位，内容变化不会被旧高度截断
      box.removeEventListener("transitionend", done);
      box._foldDone = null;
      if (box._foldTimer) { clearTimeout(box._foldTimer); box._foldTimer = null; }
    };
    box._foldDone = done;
    box.addEventListener("transitionend", done);
    // 兜底：某些情况下 transitionend 不触发（元素被移出视口/过渡被禁用），400ms 后强制复位
    box._foldTimer = setTimeout(done, 420);
  }

  /**
   * N2（BUGS-v2.1）：Keystore 密钥变更后的提示横幅。
   *
   * P1-1 只做到「不覆盖密文」，但界面照旧显示「未配置」，用户不知道要重新登录。
   * lost=true  → 红黄横幅「系统密钥已变更，请重新填写凭据」；
   * lost=false 但 cryptoOk=false → 说明加密一直失败、凭据正以明文落盘。
   * 用户点「知道了」后本标签页内不再弹（sessionStorage 记住），但下次启动会再提醒一次
   * —— 凭据丢了这件事不能永久静音。crypto_ok 的真值只由后端能力探测决定
   * （BUGS-v2.63-293 P3），前端不写死。
   */
  const CREDS_TITLE_LOST = "系统密钥已变更，请重新填写凭据";
  const CREDS_SUB_LOST = "本机加密密钥丢失（换机 / 清数据 / 移除锁屏密码都会导致），旧凭据已无法解密。为免覆盖仍完好的密文，程序保留了原值未删除 —— 重新登录或重填一次即可恢复。";
  const CREDS_TITLE_PLAIN = "本机凭据未加密";
  const CREDS_SUB_PLAIN = "系统密钥不可用，凭据正以明文保存在本机。功能不受影响，但建议检查设备的锁屏/安全设置后重新保存一次凭据。";

  function showCredsAlert(lost, cryptoOk) {
    const box = $("#credsAlert");
    if (!box) return;
    if (!lost && cryptoOk) { box.hidden = true; return; }
    // 两种状态来回切换时必须重写文案 —— 只写 plain 那支的话，先 plain 后 lost
    // 会残留「本机凭据未加密」的旧标题。
    const tb = $("#credsAlert b");
    const sub = $("#credsAlertSub");
    if (tb) tb.textContent = lost ? CREDS_TITLE_LOST : CREDS_TITLE_PLAIN;
    if (sub) sub.textContent = lost ? CREDS_SUB_LOST : CREDS_SUB_PLAIN;
    if (sessionStorage.getItem("signhub_creds_alert_off") === "1") { box.hidden = true; return; }
    box.hidden = false;
  }

  function bindCredsAlert() {
    const btn = $("#credsAlertClose");
    if (!btn || btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", async () => {
      const box = $("#credsAlert");
      if (box) box.hidden = true;
      sessionStorage.setItem("signhub_creds_alert_off", "1");
      // P2 N4（BUGS-v2.3）：SecretBox.reset() 以前没有任何调用方 —— 用户点「知道了」
      // 只是把横幅藏起来，crypto_ok 还是 false，下次 loadAll 又弹出来，只能等
      // Keystore 恰好自己恢复。这里真的复位一次失败计数再刷横幅。
      //
      // BUGS-v2.63-293 P3：**不要**把 crypto_ok 写死成 true。它原来一写就盖掉了
      // 后端/本机的能力探测结果 —— 用户点一次「知道了」，即使凭据仍是明文，
      // /api/status 之后也永远报加密正常。以接口返回值为准（Android 是
      // SecretBox.healthy()，电脑端是 vault.can_encrypt()），只保留「本页不再弹」
      // 这个纯前端开关（sessionStorage），下次启动横幅会重新按真实能力判断。
      try {
        const res = await api("/api/crypto_reset", { method: "POST" });
        if (state.status && res && typeof res.crypto_ok === "boolean") {
          state.status.crypto_ok = res.crypto_ok;
        }
        refreshCredsAlert();
      } catch (_) {}
    });
  }

  /**
   * 从 state 里重新计算横幅状态。
   *
   * P2R3（BUGS-v2.2）：以前 showCredsAlert 只在 loadAll() 里被调用一次，
   * 而横幅元素又嵌在 #viewSettings 内部 —— 用户停在首页时根本看不到。
   * 现在元素搬到全局浮层，这里统一从 /api/status（顶层）与 /api/config
   * （server 节点）两处取数，switchView() 与 loadAll() 都调它。
   * 两个数据源优先级：/api/status 顶层（每 25 秒刷新一次）> /api/config.server。
   */
  function refreshCredsAlert() {
    const st = state.status || {};
    const srv = (state.config && state.config.server) || {};
    const lost = st.creds_lost != null ? !!st.creds_lost : !!srv.creds_lost;
    const ok = st.crypto_ok != null ? st.crypto_ok !== false : srv.crypto_ok !== false;
    // P2R3（BUGS-v2.2）：横幅元素跟着当前可见的 view 走 —— 插到它的 .view-body
    // 最前面。这样任何 tab 都能看到，同时保持文档流（fixed 浮层会压住头图大标题）。
    const box = $("#credsAlert");
    if (box) {
      const view = $$(".view").find((v) => !v.hidden);
      const host = view && view.querySelector(".view-body");
      if (host && box.parentElement !== host) host.insertBefore(box, host.firstChild);
    }
    showCredsAlert(lost, ok);
  }

  /* 登录块统一（用户 m17033）：四张卡的「登录」区完全一致 ——
     绿色主按钮 = 登录主账号（单账号，写设置页那组扁平字段）；
     蓝色次按钮 = 多账号管理（进列表，增删 / 逐账号签到）。
     以前 WorkBuddy 的两个按钮是 Android 侧 injectWbLogin() 用 inline style 注入的，
     Trae/Qoder/MiniMax 只有网页端零散的 ghost-btn，四张卡长得都不一样。
     现在统一由这里渲染，样式走 app.css 的 .lg-row / .lg-main / .lg-multi，
     Android 侧那两个注入函数不再创建按钮（只保留给旧缓存页面兜底）。 */
  const LOGIN_PROVIDERS = ["workbuddy", "traesolo", "qoder", "minimax", "huawei", "doubao", "zhipu", "qianfan"];

  function loginBlockHTML(key) {
    if (LOGIN_PROVIDERS.indexOf(key) < 0) return "";
    // WorkBuddy 多一个「国际版」入口：workbuddy.ai 与 codebuddy.cn 是同一套协议、
    // 两套账号体系（实测两边未登录时的错误体一字不差，但登录态互不可见）。
    // 所以国际版不是另一家 provider，而是同一个登录流程的另一个区域参数。
    const aiBtn = key === "workbuddy"
      ? `<button class="lg-ai" type="button" data-lg-act="main-ai">
        <span class="lg-ico">${iconHTML(key)}</span>
        <span class="lg-txt">国际版</span>
      </button>`
      : "";
    // 华为码道也支持多账号（用户 m24258）：每个账号一份会话 Cookie，互不干扰。
    // 以前这里特意不渲染，理由写的是「一份 Cookie = 一个会话，换号就是覆盖」——
    // 那个理由只在「凭据是单字段」时成立；现在多账号列表按整条 Cookie 判重，
    // 换号就是新增一条，不再是覆盖。
    //
    // 智谱清言 / 百度千帆是单账号 provider（多账号列表 MultiAccountsActivity 只认
    // traesolo / qoder / minimax，其余一律落 else 分支当 MiniMax 处理 —— 那正是
    // workbuddy 曾经踩过的 bug），所以这里**不给**它们渲染「多账号管理」按钮。
    const singleOnly = key === "zhipu" || key === "qianfan";
    const multiBtn = singleOnly ? "" : `<button class="lg-multi" type="button" data-lg-act="multi">
        <span class="lg-ico">${iconHTML(key)}</span>
        <span class="lg-txt">多账号管理</span>
      </button>`;
    return `<div class="lg-block" data-lg="${escapeHtml(key)}">
      <button class="lg-main" type="button" data-lg-act="main">
        <span class="lg-ico">${iconHTML(key)}</span>
        <span class="lg-txt">登录主账号</span>
      </button>
      ${aiBtn}
      ${multiBtn}
    </div>`;
  }

  function bindLoginBlock(card) {
    const key = card.dataset.key;
    const box = $(".lg-block", card);
    if (!box) return;
    const main = $('[data-lg-act="main"]', box);
    const multi = $('[data-lg-act="multi"]', box);
    const openLogin = (provider) => {
      if (window.SignHubApi && window.SignHubApi.openLogin) {
        try { window.SignHubApi.openLogin(provider); return true; } catch (_) {}
      }
      return false;
    };
    const openMulti = (provider) => {
      // WorkBuddy 有自己的一页（WbAccountsActivity，OAuth 通道），必须走 openAccounts。
      // 以前这里先调 openMultiAccounts("workbuddy")，而 MultiAccountsActivity 只认
      // traesolo / qoder / minimax，其余一律落 else 分支当 MiniMax 处理 ——
      // 于是 WorkBuddy 的「多账号管理」打开的是 MiniMax 的页面、点添加账号进的也是
      // MiniMax 的登录网址（用户 m17284 报的 bug）。
      if (key === "workbuddy") {
        if (window.SignHubApi && window.SignHubApi.openAccounts) {
          try { window.SignHubApi.openAccounts(); return true; } catch (_) {}
        }
        return false;
      }
      if (window.SignHubApi && window.SignHubApi.openMultiAccounts) {
        try { window.SignHubApi.openMultiAccounts(provider); return true; } catch (_) {}
      }
      return false;
    };
    if (main) {
      main.addEventListener("click", (e) => {
        e.stopPropagation();
        // WorkBuddy 的「主账号」走 openLogin('workbuddy')；其余三家同 key
        const provider = key === "traesolo" ? "trae" : key;
        if (openLogin(provider)) return;
        toast(PC_LOGIN_HINT[key] || "请到对应官网登录后把凭据粘到下面", 6000);
      });
    }
    const ai = $('[data-lg-act="main-ai"]', box);
    if (ai) {
      ai.addEventListener("click", (e) => {
        e.stopPropagation();
        if (openLogin("workbuddy-ai")) return;
        toast("电脑端请浏览器登录 workbuddy.ai，F12 → Application → Local Storage → 复制 accessToken 粘到下面", 6000);
      });
    }
    if (multi) {
      multi.addEventListener("click", (e) => {
        e.stopPropagation();
        if (openMulti(key)) return;
        toast("电脑端请到「多账号管理」里逐个添加（手机端可用 App 内网页登录）", 6000);
      });
    }
  }

  const PC_LOGIN_HINT = {
    workbuddy: "电脑端请浏览器登录 codebuddy.cn，F12 → Application → Local Storage → 复制 accessToken 粘到下面",
    traesolo: "电脑端请浏览器登录 www.trae.cn，F12 → Application → Cookies → 复制 X-Cloudide-Session 粘到下面",
    qoder: "电脑端请点「读取本机凭据」直接读 Qoder CN 客户端登录态，或把 dt- token 粘到下面",
    minimax: "电脑端请浏览器登录 agent.minimax.cn，F12 → Application → Local Storage → 复制 _token 粘到下面",
    huawei: "电脑端请浏览器登录 devcloud.cn-north-4.huaweicloud.com/chat/home，F12 → Network → 任一带 PromptCenterService 的请求 → 复制整条 Cookie 粘到下面",
    doubao: "电脑端请浏览器登录 www.doubao.com，F12 → Network → 任一带 sessionid 的请求 → 复制整条 Cookie 粘到下面（手机端点「登录豆包」自动采集更省事）",
    zhipu: "电脑端请浏览器登录 chatglm.cn，F12 → Application → Cookies → 复制整串 Cookie 粘到下面（手机端点「登录主账号」自动采集更省事）",
    qianfan: "电脑端请浏览器登录 console.bce.baidu.com，F12 → Application → Cookies → 复制整串 Cookie 粘到下面（手机端点「登录主账号」自动采集更省事）",
  };

  /**
   * 自绘下拉（用户 m20782）。
   *
   * WebView 里点 `<select>` 弹出的是**系统原生单选列表**（灰底、直角、带系统配色），
   * CSS 完全改不到它 —— 那是 WebView 内部的对话框，不属于页面 DOM。
   * 做法：`<select>` 留在 DOM 里（全软件的读写都走 `.value` / `.options`，
   * 换实现风险太大），只用 CSS 把它藏起来，前面放一个自绘胶囊按钮；
   * 点按钮弹底部选择器，选中后写回 `select.value` 并派发 `change`。
   *
   * 这样 `.value` / `.options` / `[...mSel.options]` / `appendChild` 全部照旧可用。
   */
  function selHTML(cls, title, opts, cur) {
    const list = opts
      .map(([v, t]) => `<option value="${escapeHtml(v)}"${v === cur ? " selected" : ""}>${escapeHtml(t)}</option>`)
      .join("");
    const hit = opts.find(([v]) => v === cur) || opts[0] || ["", ""];
    return `<div class="sel" data-title="${escapeHtml(title)}">` +
      `<button type="button" class="sel-btn">` +
      `<span class="sel-txt">${escapeHtml(hit[1])}</span><i class="sel-caret">▾</i></button>` +
      `<select class="${cls}">${list}</select></div>`;
  }

  function paintSel(sel) {
    const box = sel.closest(".sel");
    if (!box) return;
    const o = sel.options[sel.selectedIndex];
    const t = $(".sel-txt", box);
    if (t) t.textContent = o ? (o.textContent || "").trim() : "";
  }

  function closeSelSheet() {
    const s = $("#selSheet");
    if (!s) return;
    s.classList.remove("show");
    setTimeout(() => { if (!s.classList.contains("show")) s.hidden = true; }, 260);
  }

  function openSelSheet(sel) {
    const opts = [...sel.options].map((o) => ({ v: o.value, t: (o.textContent || "").trim() }));
    if (!opts.length) return;
    const box = sel.closest(".sel");
    const title = (box && box.dataset.title) || "请选择";
    let sheet = $("#selSheet");
    if (!sheet) {
      sheet = document.createElement("div");
      sheet.className = "wb-acct-mask";
      sheet.id = "selSheet";
      sheet.innerHTML = '<div class="wb-acct-panel"><div class="wb-acct-head">' +
        '<b id="selSheetTitle">请选择</b>' +
        '<button class="wb-acct-x" type="button">关闭</button></div>' +
        '<div class="wb-acct-list" id="selSheetList"></div></div>';
      document.body.appendChild(sheet);
      sheet.addEventListener("click", (e) => {
        if (e.target === sheet || (e.target.closest && e.target.closest(".wb-acct-x"))) closeSelSheet();
      });
    }
    $("#selSheetTitle").textContent = title;
    const listBox = $("#selSheetList");
    listBox.innerHTML = opts.map((o) => {
      const on = o.v === sel.value;
      return `<button class="wb-acct-item${on ? " on" : ""}" type="button" data-v="${escapeHtml(o.v)}">` +
        `<span class="wa-name">${escapeHtml(o.t)}</span>` +
        (on ? '<span class="wa-cur">当前</span>' : "") + "</button>";
    }).join("");
    listBox.querySelectorAll(".wb-acct-item").forEach((b) => {
      b.addEventListener("click", () => {
        sel.value = b.dataset.v;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        paintSel(sel);
        closeSelSheet();
      });
    });
    sheet.hidden = false;
    void sheet.offsetWidth;
    sheet.classList.add("show");
  }

  // 事件委托：配置卡每次重渲染都会换掉 DOM，逐个绑会漏；document 级只绑一次。
  function initSelPicker() {
    document.addEventListener("click", (e) => {
      const btn = e.target instanceof Element ? e.target.closest(".sel-btn") : null;
      if (!btn) return;
      const sel = $("select", btn.closest(".sel"));
      if (sel) { e.preventDefault(); e.stopPropagation(); openSelSheet(sel); }
    });
  }

  /**
   * 自绘二次确认弹层（替代 window.confirm）。
   *
   * 为什么不能用 window.confirm：Android WebView 会把它渲染成**系统原生 AlertDialog**
   * —— 深灰底、直角、系统配色，与软件的三套主题完全不搭（用户 m20150 / m20782
   * 连续两次抱怨过这类原生框）。而且原生框还会阻塞 JS 线程，动画全停。
   *
   * 这里复用底部弹层（.wb-acct-mask / .wb-acct-panel）的视觉，返回 Promise<boolean>。
   */
  function askConfirm(opt) {
    const o = opt || {};
    return new Promise((resolve) => {
      let sheet = $("#askSheet");
      if (!sheet) {
        sheet = document.createElement("div");
        sheet.className = "wb-acct-mask";
        sheet.id = "askSheet";
        sheet.innerHTML =
          '<div class="wb-acct-panel">' +
          '<div class="wb-acct-head"><b id="askTitle"></b></div>' +
          '<div class="ask-body" id="askBody"></div>' +
          '<div class="ask-actions">' +
          '<button class="ghost-btn" type="button" id="askCancel"></button>' +
          '<button class="save-btn" type="button" id="askOk"></button>' +
          "</div></div>";
        document.body.appendChild(sheet);
      }
      $("#askTitle").textContent = o.title || "确认";
      $("#askBody").textContent = o.message || "";
      const ok = $("#askOk");
      const cancel = $("#askCancel");
      ok.textContent = o.okText || "确定";
      cancel.textContent = o.cancelText || "取消";
      ok.className = "save-btn" + (o.danger ? " danger-solid" : "");
      let done = false;
      const close = (v) => {
        if (done) return;
        done = true;
        sheet.classList.remove("show");
        setTimeout(() => { if (!sheet.classList.contains("show")) sheet.hidden = true; }, 260);
        resolve(v);
      };
      ok.onclick = (e) => { e.stopPropagation(); close(true); };
      cancel.onclick = (e) => { e.stopPropagation(); close(false); };
      sheet.onclick = (e) => { if (e.target === sheet) close(false); };
      sheet.hidden = false;
      void sheet.offsetWidth;
      sheet.classList.add("show");
    });
  }

  function renderProviderEditors() {
    const box = $("#providerEditors");
    let cfg = state.config;
    // 兜底：/api/config 挂了（安卓端 DataStore 读写竞争、电脑端后端异常）时，
    // 用 /api/status 里已有的 provider 列表拼一份最小配置，让卡片照样渲染出来。
    // 否则整片「签到配置」是空白的，用户连凭据都填不进去，只能等 25 秒后的下一轮。
    if (!cfg || !cfg.providers) {
      const list = (state.status && state.status.providers) || [];
      if (!list.length) return;
      const providers = {};
      list.forEach((p) => {
        providers[p.key] = { enabled: p.enabled !== false, mode: p.mode || "http" };
      });
      cfg = { providers: providers, degraded: true };
    }
    // 重建前记住：哪些卡是展开的、页面滚到哪 —— 避免「点测试后列表跳回去」
    const openKeys = $$(".prov-card.open", box).map((c) => c.dataset.key);
    const scrollY = window.scrollY;
    const st = (state.status && state.status.providers) || [];
    $("#readySummary").textContent = `${st.filter((p) => p.ready).length} / ${st.length} 已就绪`;

    // 只渲染「当前后端认得」的 provider：手机端 LocalApi 有豆包，电脑端 Python 后端还没有，
    // 直接用 Object.keys(LABELS) 会在电脑端多出一张点了就报错的空卡片。
    const knownKeys = Object.keys(LABELS).filter((k) => cfg.providers[k] || st.some((x) => x.key === k));
    box.innerHTML = knownKeys.map((key) => {
      const p = cfg.providers[key] || {};
      const info = st.find((x) => x.key === key) || {};
      const mode = (p.mode || "http").toLowerCase();
      const h = p.http || {};
      const b = p.browser || {};
      const wb = p.wb || {};
      const tr = p.trae || {};
      const qdc = p.qoder || {};
      const mmc = p.minimax || {};
      const hwc = p.huawei || {};
      const dbc = p.doubao || {};
      const zpc = p.zhipu || {};
      const qfc = p.qianfan || {};
      const ready = !!info.ready;
      const enabled = p.enabled !== false;
      const badge = !enabled ? `<span class="pv-badge off">已禁用</span>`
        : ready ? `<span class="pv-badge ok">就绪</span>` : `<span class="pv-badge bad">未就绪</span>`;
      const issues = info.issues || [];
      const issuesHtml = issues.length
        ? `<ul class="pv-issues">${issues.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : "";
      const selectors = Array.isArray(b.click_selectors) ? b.click_selectors.join(", ") : "";
      const bodyText = (h.body && Object.keys(h.body).length) ? JSON.stringify(h.body, null, 2) : "";
      return `<div class="prov-card" data-key="${key}">
        <div class="prov-head">
          <div class="pv-ico${hasLogo(key) ? " has-logo" : ""}">${iconHTML(key)}</div>
          <div class="pv-name">${LABELS[key]} ${badge}</div>
          <svg class="pv-caret" width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div class="prov-body">
         <div class="pv-body-inner">
          ${loginBlockHTML(key)}
          <div class="mini-switch" style="margin-bottom:12px">
            <label class="switch"><input type="checkbox" class="pv-enabled" ${enabled ? "checked" : ""}><span></span></label>
            <span>参与每日签到</span>
          </div>
          ${issuesHtml}
          <div class="seg" data-seg>
            ${key === "qoder" ? `<button data-mode="qoder" class="${mode === "qoder" ? "on" : ""}">Qoder 直连</button>`
              : key === "minimax" ? `<button data-mode="minimax" class="${mode === "minimax" ? "on" : ""}">MiniMax 直连</button>`
              : key === "huawei" ? `<button data-mode="huawei" class="${mode === "huawei" ? "on" : ""}">码道直连</button>`
              : key === "doubao" ? `<button data-mode="doubao" class="${mode === "doubao" ? "on" : ""}">豆包直连</button>`
              : key === "zhipu" ? `<button data-mode="zhipu" class="${mode === "zhipu" ? "on" : ""}">智谱直连</button>`
              : key === "qianfan" ? `<button data-mode="qianfan" class="${mode === "qianfan" ? "on" : ""}">千帆直连</button>`
              : `<button data-mode="http" class="${mode === "http" ? "on" : ""}">接口模式</button>
            <button data-mode="browser" class="${mode === "browser" ? "on" : ""}">浏览器模式</button>
            ${key === "workbuddy" ? `<button data-mode="wb" class="${mode === "wb" ? "on" : ""}">WB 直连</button>` : ""}
            ${key === "traesolo" ? `<button data-mode="trae" class="${mode === "trae" ? "on" : ""}">Trae 直连</button>` : ""}`}
          </div>
          <div class="pv-http" ${mode === "http" ? "" : "hidden"}>
            <label class="field">一键粘贴 cURL（在 DevTools 里右键请求 → Copy → Copy as cURL）
              <textarea class="f-curl" rows="3" placeholder="curl 'https://...' -X POST -H 'cookie: ...' --data-raw '{...}'"></textarea>
            </label>
            <button class="ghost-btn f-curlparse" style="width:100%;height:38px;font-size:12.5px;margin-bottom:12px">解析并填入下面的字段</button>
            <label class="field">签到接口 URL<input class="f-url" value="${escapeHtml(h.url || "")}" placeholder="https://抓到的签到接口" /></label>
            <label class="field">请求方法
              ${selHTML("f-method", "请求方法", [["POST", "POST"], ["GET", "GET"], ["PUT", "PUT"]], (h.method || "POST").toUpperCase())}
            </label>
            <label class="field">请求头（每行 Key: Value）<textarea class="f-headers" placeholder="Cookie: xxx&#10;User-Agent: xxx">${escapeHtml(headersToText(h.headers))}</textarea></label>
            <label class="field">请求体（JSON，可留空）<textarea class="f-body" placeholder='{"task":"checkin"}'>${escapeHtml(bodyText)}</textarea></label>
            <label class="field">成功判定字段<input class="f-expr" value="${escapeHtml(h.success_expr || "code")}" placeholder="code 或 data.status" /></label>
            <label class="field">成功判定值<input class="f-sval" value="${escapeHtml(String(h.success_value == null ? 0 : h.success_value))}" placeholder="0" /></label>
          </div>
          <div class="pv-browser" ${mode === "browser" ? "" : "hidden"}>
            <label class="field">登录页地址<input class="f-login" value="${escapeHtml(b.login_url || "")}" placeholder="https://..." /></label>
            <label class="field">签到页地址<input class="f-sign" value="${escapeHtml(b.sign_url || "")}" placeholder="https://..." /></label>
            <label class="field">签到按钮选择器（逗号分隔）<input class="f-selectors" value="${escapeHtml(selectors)}" placeholder="text=签到, text=领取" /></label>
            <div class="mini-switch" style="margin-bottom:10px">
              <label class="switch"><input type="checkbox" class="f-headless" ${b.headless !== false ? "checked" : ""}><span></span></label>
              <span>无头运行（不弹窗口）</span>
            </div>
          </div>
          ${key === "workbuddy" ? `<div class="pv-wb" ${mode === "wb" ? "" : "hidden"}>
            <label class="field">区域
              ${selHTML("f-wbregion", "WorkBuddy 区域", [
                ["cn", "国内版 (cn)"],
                ["global", "国际版 (global)"],
                ["", "自动"],
              ], wb.region || "cn")}
            </label>
            <label class="field">accessToken（可留空自动发现）<input class="f-wbtoken" value="${escapeHtml(wb.accessToken || "")}" placeholder="留空 = 从本机自动读取" /></label>
            <label class="field">uid（可选）<input class="f-wbuid" value="${escapeHtml(wb.uid || "")}" placeholder="账号 uid" /></label>
            <label class="field">domain（可选）<input class="f-wbdomain" value="${escapeHtml(wb.domain || "")}" placeholder="如 xxx.workbuddy.cn" /></label>
          </div>` : ""}
          ${key === "traesolo" ? `<div class="pv-trae" ${mode === "trae" ? "" : "hidden"}>
            <div class="row-btns" style="margin-bottom:10px">
              <button class="ghost-btn f-trae-probe">检查凭据</button>
            </div>
            <p class="muted small f-trae-state" style="margin:0 0 10px"></p>
            <label class="field">区域
              ${selHTML("f-traeregion", "Trae 区域", [
                ["cn", "国内版 (cn)"],
                ["global", "国际版 (global)"],
              ], tr.region || "cn")}
            </label>
            <label class="field">API Host（留空用默认）<input class="f-traehost" value="${escapeHtml(tr.host || "")}" placeholder="https://api.trae.cn" /></label>
            <label class="field">x-device-id（16 位数字）<input class="f-traedevice" value="${escapeHtml(tr.deviceId || "")}" placeholder="留空自动从 Trae 客户端 storage.json 读取" /></label>
            <label class="field">X-Cloudide-Session（推荐，约 14 天，可自动续期）<input class="f-traesession" value="${escapeHtml(tr.session || "")}" placeholder="登录一次后自动填入；也可手动粘贴：F12 → Application → Cookies → www.trae.cn" /></label>
            <label class="field">Cloud-IDE-Token（可选，约 8 小时）<input class="f-traetoken" value="${escapeHtml(tr.token || "")}" placeholder="扫码登录成功后自动填入" /></label>
          </div>` : ""}
          ${key === "qoder" ? `<div class="pv-qoder" ${mode === "qoder" ? "" : "hidden"}>
            <label class="field">区域
              ${selHTML("f-qd-region", "Qoder 区域", [
                ["cn", "国内版（openapi.qoder.com.cn）"],
                ["intl", "国际版（openapi.qoder.sh，需设备证书）"],
              ], qdc.region || "cn")}
            </label>
            <label class="field">accessToken（dt- 开头，可留空）<input class="f-qd-token" value="${escapeHtml(qdc.token || "")}" placeholder="可留空；只填下面的 refreshToken 也能签到" /></label>
            <label class="field">refreshToken（drt- 开头，推荐）<input class="f-qd-refresh" value="${escapeHtml(qdc.refreshToken || "")}" placeholder="只填这个就够：有效期一年，token 过期会自动续" /></label>
            <label class="field">用户 id（可留空）<input class="f-qd-uid" value="${escapeHtml(qdc.uid || "")}" placeholder="选填，不影响签到" /></label>
            <label class="field">设备指纹 Cosy-MachineId（可留空）<input class="f-qd-machine" value="${escapeHtml(qdc.machineId || "")}" placeholder="选填，签到不校验" /></label>
            <label class="field">代理（可选）<input class="f-qd-proxy" value="${escapeHtml(qdc.proxy || "")}" placeholder="http://127.0.0.1:7890" /></label>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:2px">
              <button class="ghost-btn f-qd-read" type="button">读取本机凭据</button>
            </div>
          </div>` : ""}
          ${key === "minimax" ? `<div class="pv-mm" ${mode === "minimax" ? "" : "hidden"}>
            <label class="field">accessToken<input class="f-mm-token" value="${escapeHtml(mmc.accessToken || "")}" placeholder="网页端 localStorage['_token'] 的值（无前缀），或留空自动读本机登录态" /></label>
            <label class="field">user_id（可留空）<input class="f-mm-uid" value="${escapeHtml(mmc.userId || "")}" placeholder="留空自动读 minimax-agent-cn-config.json" /></label>
            <label class="field">device_id（可留空）<input class="f-mm-device" value="${escapeHtml(mmc.deviceId || "")}" placeholder="留空自动读" /></label>
            <label class="field">代理（可选）<input class="f-mm-proxy" value="${escapeHtml(mmc.proxy || "")}" placeholder="http://127.0.0.1:7890" /></label>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:2px">
              <button class="ghost-btn f-mm-read" type="button">读取本机凭据</button>
            </div>
          </div>` : ""}
          ${key === "huawei" ? `<div class="pv-hw" ${mode === "huawei" ? "" : "hidden"}>
            <label class="field">会话 Cookie（必填）<textarea class="f-hw-cookie" rows="4" placeholder="登录 devcloud.cn-north-4.huaweicloud.com/chat/home 后，F12 → Network → 任一 PromptCenterService 请求 → 复制整条 Cookie">${escapeHtml(hwc.cookie || "")}</textarea></label>
            <label class="field">代理（可选）<input class="f-hw-proxy" value="${escapeHtml(hwc.proxy || "")}" placeholder="http://127.0.0.1:7890" /></label>
            <p class="muted small" style="margin:0 0 4px">${hwc.authCookieReady
              ? "✓ 到期自动续期已就绪：会话失效时会自动静默重登一次，不用重输手机号。"
              : "⚠ 缺 auth.huaweicloud.com 的 Cookie：会话失效后无法自动续期。手机上点「登录华为码道」会自动一起采集。"}</p>
          </div>` : ""}
          ${key === "doubao" ? `<div class="pv-db" ${mode === "doubao" ? "" : "hidden"}>
            <label class="field">网页版 Cookie（必填，含 sessionid）<textarea class="f-db-cookie" rows="4" placeholder="登录 www.doubao.com 后，F12 → Network → 任一带 sessionid 的请求 → 复制整条 Cookie；手机端点上面的「登录主账号」会自动采集">${escapeHtml(dbc.cookie || "")}</textarea></label>
            <label class="field">活动 scene（一般留空）<input class="f-db-scene" value="${escapeHtml(dbc.scene || "")}" placeholder="留空 = 自动从活动页响应里取 campaign_group_code" /></label>
            <label class="field">代理（可选）<input class="f-db-proxy" value="${escapeHtml(dbc.proxy || "")}" placeholder="http://127.0.0.1:7890" /></label>
            <p class="muted small" style="margin:0 0 4px">豆包不需要 a_bogus 签名，也不校验 User-Agent，纯 Cookie 直调即可。Cookie 里必须有 sessionid / sid_tt / uid_tt / sid_guard。签到用的 scene 会自动从活动页响应里取（等于 campaign_group_code），不用手填。</p>
          </div>` : ""}
          ${key === "zhipu" ? `<div class="pv-zp" ${mode === "zhipu" ? "" : "hidden"}>
            <label class="field">整串 Cookie<textarea class="f-zp-cookie" rows="4" placeholder="登录 chatglm.cn 后，F12 → Application → Cookies → https://chatglm.cn → 全选复制成 name=value; name=value；手机端点上面的「登录主账号」会自动采集">${escapeHtml(zpc.cookie || "")}</textarea></label>
            <label class="field">X-Device-Id（可留空）<input class="f-zp-device" value="${escapeHtml(zpc.deviceId || "")}" placeholder="留空 = 自动从 chatglm_token 的 JWT 里解 device_id" /></label>
          </div>` : ""}
          ${key === "qianfan" ? `<div class="pv-qf" ${mode === "qianfan" ? "" : "hidden"}>
            <label class="field">整串 Cookie<textarea class="f-qf-cookie" rows="4" placeholder="登录 console.bce.baidu.com 后，F12 → Application → Cookies → https://console.bce.baidu.com → 全选复制成 name=value; name=value；手机端点上面的「登录主账号」会自动采集">${escapeHtml(qfc.cookie || "")}</textarea></label>
          </div>` : ""}
          <div class="pv-result ${(state.lastTest[key] || {}).cls || ""}" ${state.lastTest[key] ? "" : "hidden"}>${(state.lastTest[key] || {}).html || ""}</div>
          <div class="pv-actions">
            <button class="ghost-btn pv-test">测试签到</button>
            <button class="ghost-btn danger pv-reset" type="button">重置凭据</button>
            <button class="save-btn pv-save">保存</button>
          </div>
         </div>
        </div>
      </div>`;
    }).join("");

    $$(".prov-card", box).forEach(bindProviderCard);
    // 恢复展开状态（含 #openall）与滚动位置
    const wantAll = location.hash.includes("openall");
    $$(".prov-card", box).forEach((c) => {
      if (wantAll || openKeys.indexOf(c.dataset.key) >= 0) {
        c.classList.add("open");
        const b = $(".prov-body", c);
        if (b) b.style.height = "auto";   // 恢复态不做动画，直接给真实高度
      }
    });
    if (scrollY > 0) window.scrollTo({ top: scrollY });
  }

  function bindProviderCard(card) {
    const key = card.dataset.key;
    bindLoginBlock(card);
    $(".prov-head", card).addEventListener("click", () => {
      const willOpen = !card.classList.contains("open");
      card.classList.toggle("open", willOpen);
      foldToggle($(".prov-body", card), willOpen);
    });
    const seg = $(".seg", card);
    let mode = ($(".seg button.on", card) || {}).dataset ? $(".seg button.on", card).dataset.mode : "http";
    $$("button", seg).forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        mode = btn.dataset.mode;
        $$("button", seg).forEach((b) => b.classList.toggle("on", b === btn));
        $(".pv-http", card).hidden = mode !== "http";
        $(".pv-browser", card).hidden = mode !== "browser";
        const wbBox = $(".pv-wb", card);
        if (wbBox) wbBox.hidden = mode !== "wb";
        const trBox = $(".pv-trae", card);
        if (trBox) trBox.hidden = mode !== "trae";
        const qdBox = $(".pv-qoder", card);
        if (qdBox) qdBox.hidden = mode !== "qoder";
        const mmBox = $(".pv-mm", card);
        if (mmBox) mmBox.hidden = mode !== "minimax";
        const hwBox = $(".pv-hw", card);
        if (hwBox) hwBox.hidden = mode !== "huawei";
        const dbBox = $(".pv-db", card);
        if (dbBox) dbBox.hidden = mode !== "doubao";
        const zpBox = $(".pv-zp", card);
        if (zpBox) zpBox.hidden = mode !== "zhipu";
        const qfBox = $(".pv-qf", card);
        if (qfBox) qfBox.hidden = mode !== "qianfan";
      });
    });
    const curlBtn = $(".f-curlparse", card);
    if (curlBtn) {
      curlBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const raw = ($(".f-curl", card).value || "").trim();
        if (!raw) { toast("先粘贴 cURL"); return; }
        const r = parseCurlCommand(raw);
        if (!r.url) { toast("没解析出 URL，确认复制的是 Copy as cURL"); return; }
        $(".f-url", card).value = r.url;
        const mSel = $(".f-method", card);
        if (mSel) {
          const want = r.method.toUpperCase();
          const has = [...mSel.options].some((o) => o.value === want);
          if (!has) {
            const opt = document.createElement("option");
            opt.textContent = want;
            mSel.appendChild(opt);
          }
          mSel.value = want;
          paintSel(mSel);   // 自绘下拉的显示文字要跟着换
        }
        if (Object.keys(r.headers).length) {
          $(".f-headers", card).value = headersToText(r.headers);
        }
        if (r.body) {
          let pretty = r.body;
          try { pretty = JSON.stringify(JSON.parse(r.body), null, 2); } catch (_) {}
          $(".f-body", card).value = pretty;
        }
        toast("已填入，请检查后点「保存」，再点「测试签到」");
      });
    }

    // 豆包已下线（2026-11-20 服务端 710010702），扫码登录/检查凭据的绑定一并移除。

    // Trae：检查凭据（登录入口已统一到 .lg-block，见 loginBlockHTML）
    const traeProbeBtn = $(".f-trae-probe", card);
    const traeState = $(".f-trae-state", card);
    if (traeProbeBtn) {
      traeProbeBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        traeProbeBtn.disabled = true;
        try {
          const p = await api("/api/trae_probe");
          const bits = [];
          if (p.session_ok) bits.push(`session 已就绪（${p.session_len} 字符，可自动续期）`);
          if (p.token_ok) {
            const exp = Number(p.token_exp);
            if (isFinite(exp) && exp > 0) {
              const left = Math.round((exp * 1000 - Date.now()) / 36e5 * 10) / 10;
              bits.push(left > 0 ? `token 剩 ${left} 小时` : "token 已过期（有 session 会自动换新）");
            } else bits.push(`token 已就绪（${p.token_len} 字符）`);
          } else if (!p.session_ok) {
            bits.push("凭据缺失");
          }
          bits.push(p.device_id ? `设备号 ${p.device_id}` + (p.device_from ? `（来自 ${p.device_from}）` : "") : "设备号缺失");
          if (traeState) {
            traeState.textContent = bits.join(" · ");
            const okAuth = p.session_ok || p.token_ok;
            traeState.style.color = (okAuth && p.device_id) ? "var(--ok, #37d67a)" : "";
          }
          if (!p.session_ok && !p.token_ok) toast("还没有凭据，点「登录 Trae 账号」登录一次");
        } catch (err) {
          if (traeState) traeState.textContent = "检查失败：" + err.message;
        } finally { traeProbeBtn.disabled = false; }
      });
    }
    // 电脑端「登录 Trae 账号」已统一到 .lg-block 的主按钮；
    // 那边点不到 App 内登录页时会 toast 提示手动粘贴，这里不再重复绑定。

    // Qoder / MiniMax：一键读取本机客户端登录态回填表单
    const readQd = $(".f-qd-read", card);
    if (readQd) {
      readQd.addEventListener("click", async (e) => {
        e.stopPropagation();
        readQd.disabled = true;
        try {
          const r = await api("/api/local_creds/qoder");
          if (!r.ok) { toast("读取失败：" + (r.error || "未知错误"), 3600); return; }
          const c = r.creds || {};
          if (c.token) $(".f-qd-token", card).value = c.token;
          if (c.refreshToken) $(".f-qd-refresh", card).value = c.refreshToken;
          if (c.uid) $(".f-qd-uid", card).value = c.uid;
          if (c.machineId) $(".f-qd-machine", card).value = c.machineId;
          toast(`已读本机 Qoder CN 登录态（${c.name || c.uid || ""}），记得点「保存」`);
        } catch (err) { toast("读取失败：" + err.message, 3600); }
        finally { readQd.disabled = false; }
      });
    }
    const readMm = $(".f-mm-read", card);
    if (readMm) {
      readMm.addEventListener("click", async (e) => {
        e.stopPropagation();
        readMm.disabled = true;
        try {
          const r = await api("/api/local_creds/minimax");
          if (!r.ok) { toast("读取失败：" + (r.error || "未知错误"), 3600); return; }
          const c = r.creds || {};
          if (c.accessToken) $(".f-mm-token", card).value = c.accessToken;
          if (c.userId) $(".f-mm-uid", card).value = c.userId;
          if (c.deviceId) $(".f-mm-device", card).value = c.deviceId;
          toast(`已读本机 MiniMax Code 登录态（${c.userName || c.userId || ""}），记得点「保存」`);
        } catch (err) { toast("读取失败：" + err.message, 3600); }
        finally { readMm.disabled = false; }
      });
    }

    // 登录入口已统一到 .lg-block（loginBlockHTML / bindLoginBlock），
    // 这里不再为 MiniMax / Qoder 单独绑定 .f-mm-login / .f-qd-login。

    $(".pv-save", card).addEventListener("click", async (e) => {
      e.stopPropagation();
      const enabled = $(".pv-enabled", card).checked;
      const bodyObj = { enabled, mode };
      if (mode === "http") {
        let body = {};
        const bodyText = $(".f-body", card).value.trim();
        if (bodyText) {
          try { body = JSON.parse(bodyText); }
          catch (err) { toast("请求体不是合法 JSON：" + err.message, 3200); return; }
        }
        bodyObj.http = {
          method: $(".f-method", card).value,
          url: $(".f-url", card).value.trim(),
          headers: textToHeaders($(".f-headers", card).value),
          body,
          body_type: "json",
          success_expr: $(".f-expr", card).value.trim() || "code",
          success_value: Number($(".f-sval", card).value.trim() || 0),
        };
      } else if (mode === "wb") {
        bodyObj.wb = {
          region: $(".f-wbregion", card).value,
          accessToken: $(".f-wbtoken", card).value.trim(),
          uid: $(".f-wbuid", card).value.trim(),
          domain: $(".f-wbdomain", card).value.trim(),
        };
      } else if (mode === "trae") {
        bodyObj.trae = {
          region: $(".f-traeregion", card).value,
          host: $(".f-traehost", card).value.trim(),
          deviceId: $(".f-traedevice", card).value.trim(),
          session: ($(".f-traesession", card) || {}).value ? $(".f-traesession", card).value.trim() : "",
          token: $(".f-traetoken", card).value.trim(),
        };
        const newTok = bodyObj.trae.token;
        if (newTok && newTok !== ($(".f-traetoken", card).defaultValue || "")) {
          localStorage.setItem("signhub_trae_token_time", String(Date.now()));
        }
      } else if (mode === "qoder") {
        bodyObj.qoder = {
          region: $(".f-qd-region", card).value,
          token: $(".f-qd-token", card).value.trim(),
          refreshToken: $(".f-qd-refresh", card).value.trim(),
          uid: $(".f-qd-uid", card).value.trim(),
          machineId: $(".f-qd-machine", card).value.trim(),
          proxy: $(".f-qd-proxy", card).value.trim(),
        };
      } else if (mode === "minimax") {
        bodyObj.minimax = {
          accessToken: $(".f-mm-token", card).value.trim(),
          userId: $(".f-mm-uid", card).value.trim(),
          deviceId: $(".f-mm-device", card).value.trim(),
          proxy: $(".f-mm-proxy", card).value.trim(),
        };
      } else if (mode === "doubao") {
        bodyObj.doubao = {
          cookie: $(".f-db-cookie", card).value.trim(),
          scene: $(".f-db-scene", card).value.trim(),
          proxy: $(".f-db-proxy", card).value.trim(),
        };
      } else if (mode === "zhipu") {
        bodyObj.zhipu = {
          cookie: $(".f-zp-cookie", card).value.trim(),
          deviceId: $(".f-zp-device", card).value.trim(),
          proxy: "",
        };
      } else if (mode === "qianfan") {
        bodyObj.qianfan = {
          cookie: $(".f-qf-cookie", card).value.trim(),
          proxy: "",
        };
      } else if (mode === "huawei") {
        bodyObj.huawei = {
          cookie: $(".f-hw-cookie", card).value.trim(),
          // portalCookie 已不再需要（积分改读签到同域接口），这里恒传空串
          // 让老配置里的历史值在下一次保存时被自然清掉。
          portalCookie: "",
          proxy: $(".f-hw-proxy", card).value.trim(),
        };
      } else {
        bodyObj.browser = {
          login_url: $(".f-login", card).value.trim(),
          sign_url: $(".f-sign", card).value.trim(),
          click_selectors: $(".f-selectors", card).value.split(",").map((s) => s.trim()).filter(Boolean),
          headless: $(".f-headless", card).checked,
        };
      }
      try {
        await api("/api/config", { method: "POST", body: JSON.stringify({ providers: { [key]: bodyObj } }) });
        toast(LABELS[key] + " 配置已保存");
        // 用户 m14001：配置保存成功后，签到页的卡片要能直接看到积分。
        // 只 loadAll() 是不够的 —— /api/status 的 total_points 来自「当天签到记录」，
        // 今天还没签过时 rec 为 None，卡片上就是「—」。所以这里补一次余额刷新：
        // refresh_totals 会真打上游把「当前持有积分」写进当天记录，再 loadAll 就能读到。
        // silent=true 避免和上面「配置已保存」抢同一条提示条（hint 只有一个位置）。
        await loadAll();
        // 刷新余额（silent 避免和「配置已保存」抢同一条提示条）。
        // 用 autoRefreshing 闸门把 loadAll 里的「登录新 agent 自动刷新」挡掉 ——
        // 否则保存一次会打两轮四家上游（refreshBalances 内部还会再 loadAll 一次）。
        if (!state.autoRefreshing) {
          state.autoRefreshing = true;
          refreshBalances({ silent: true })
            .catch(() => {})
            .finally(() => { state.autoRefreshing = false; });
        }
      } catch (err) { toast("保存失败：" + err.message, 3200); }
    });
    $(".pv-test", card).addEventListener("click", async (e) => {
      e.stopPropagation();
      const btn = $(".pv-test", card);
      if (btn.disabled) return;
      const out = $(".pv-result", card);
      out.hidden = false;
      out.className = "pv-result";
      out.textContent = "测试中…（正在请求上游，约 1 秒）";
      btn.disabled = true;
      btn.textContent = "测试中…";
      try {
        const r = await api("/api/test/" + key, { method: "POST" });
        const ok = r.status === "success" || r.status === "already";
        const html = `<strong>${ok ? "成功" : "失败"}</strong> · ${escapeHtml(r.status)} · 用时 ${r.elapsed}s<br>${escapeHtml(r.message || "")}${r.detail ? `<br><span style="opacity:.7">${escapeHtml(String(r.detail).slice(0, 300))}</span>` : ""}`;
        // 存下来，重建列表后仍能显示（否则会被冲掉，看起来像"闪退回设置页"）
        state.lastTest[key] = { cls: ok ? "ok" : "bad", html };
        out.className = "pv-result " + (ok ? "ok" : "bad");
        out.innerHTML = html;
        await loadAll();
      } catch (err) {
        const html = "测试失败：" + escapeHtml(err.message);
        state.lastTest[key] = { cls: "bad", html };
        out.className = "pv-result bad";
        out.innerHTML = html;
      }
      const btn2 = $(".pv-test", card);
      if (btn2) { btn2.disabled = false; btn2.textContent = "测试签到"; }
    });

    // 重置凭据（用户 m21385）：登录一次之后凭据清不掉，换号时只能卸载重装。
    //
    // 根因是「掩码占位」：前端用 `dt-***lU` 表示「已保存」，保存时跳过含 *** 的值 ——
    // 于是清空输入框再点保存等于「什么都不改」，凭据永远清不掉。
    // 这里给一个显式动作：二次确认后调 /api/reset_creds，再重新拉配置把输入框清空。
    $(".pv-reset", card).addEventListener("click", async (e) => {
      e.stopPropagation();
      const btn = $(".pv-reset", card);
      if (btn.disabled) return;
      const name = LABELS[key] || key;
      const ok = await askConfirm({
        title: "重置「" + name + "」的凭据？",
        message: "会清空已保存的登录凭据，并退出本机网页登录状态（下次要重新登录）。\n区域 / 代理 / 模式等设置会保留。",
        okText: "重置",
        danger: true,
      });
      if (!ok) return;
      btn.disabled = true;
      btn.textContent = "重置中…";
      try {
        // 手机端：先清原生凭据 + 该站 WebView 登录态（否则网页里还是已登录，
        // 重新点「登录主账号」会直接被抓成同一个号）。
        if (window.SignHubApi && window.SignHubApi.resetCreds) {
          try { await window.SignHubApi.resetCreds(key); } catch (_) {}
        }
        await api("/api/reset_creds", { method: "POST", body: JSON.stringify({ key }) });
        toast(name + " 凭据已重置，可以重新登录了", 3000);
        await loadAll();
        // 清空必须放在 loadAll() **之后**：loadAll 会按 /api/config 重渲染整片卡片，
        // 先清后渲染等于白清（后端若还没落盘、或响应被 SW 缓存，输入框里仍是旧掩码）。
        const CRED_FIELDS = {
          workbuddy: [".f-wbtoken", ".f-wbuid", ".f-wbdomain"],
          traesolo: [".f-traetoken", ".f-traesession", ".f-traedevice"],
          qoder: [".f-qd-token", ".f-qd-refresh", ".f-qd-uid", ".f-qd-machine"],
          minimax: [".f-mm-token", ".f-mm-uid", ".f-mm-device"],
          huawei: [".f-hw-cookie"],
          zhipu: [".f-zp-cookie", ".f-zp-device"],
          qianfan: [".f-qf-cookie"],
        };
        const card2 = $(`.prov-card[data-key="${key}"]`);
        (CRED_FIELDS[key] || []).forEach((sel) => {
          const el = card2 ? $(sel, card2) : null;
          if (el) { el.value = ""; el.defaultValue = ""; }
        });
      } catch (err) {
        toast("重置失败：" + err.message, 3200);
      }
      const b2 = $(".pv-reset", card);
      if (b2) { b2.disabled = false; b2.textContent = "重置凭据"; }
    });
  }

  /* ============================================================
     口令 / 连接
     ============================================================ */
  on("#saveToken", "click", async () => {
    const v = $("#newToken").value.trim();
    if (!v) { toast("请输入新口令"); return; }
    if (v.length < 6) { toast("口令至少 6 位"); return; }
    try {
      await api("/api/config", { method: "POST", body: JSON.stringify({ server: { api_token: v } }) });
      TOKEN = v;
      try { sessionStorage.setItem("signhub_token", TOKEN); } catch (_) {}
      $("#apiToken").value = v;
      $("#newToken").value = "";
      toast("口令已设置");
      await loadAll();
    } catch (err) { toast("设置失败：" + err.message, 3200); }
  });
  on("#clearToken", "click", async () => {
    try {
      await api("/api/config", { method: "POST", body: JSON.stringify({ server: { api_token: "" } }) });
      TOKEN = "";
      try { sessionStorage.removeItem("signhub_token"); } catch (_) {}
      localStorage.removeItem("signhub_token");
      $("#apiToken").value = "";
      toast("口令已清除");
      await loadAll();
    } catch (err) { toast("清除失败：" + err.message, 3200); }
  });

  on("#saveConn", "click", async () => {
    const url = $("#serverUrl").value.trim();
    BASE = /^https?:\/\//i.test(url) ? url : "";
    if (BASE) localStorage.setItem("signhub_base", BASE);
    else localStorage.removeItem("signhub_base");
    TOKEN = $("#apiToken").value.trim();
    try {
      if (TOKEN) sessionStorage.setItem("signhub_token", TOKEN);
      else sessionStorage.removeItem("signhub_token");
    } catch (_) {}
    localStorage.removeItem("signhub_token");
    toast("已保存，正在连接…");
    await loadAll();
  });
  on("#retryBtn", "click", () => loadAll());

  /* ============================================================
     定时 / 通知
     ============================================================ */
  // 滑块填充：轨道用 --jit-fill 控制已填充比例
  function paintJitter(v) {
    const val = Math.max(0, Math.min(240, parseInt(v || "0", 10) || 0));
    const el = $("#schedJitter");
    if (el) {
      el.style.setProperty("--jit-fill", (val / 240 * 100).toFixed(2) + "%");
      el.value = String(val);
    }
    const lab = $("#jitterVal");
    if (lab) lab.textContent = String(val);
  }
  on("#schedJitter", "input", () => paintJitter($("#schedJitter").value));

  /* 自动签到诊断：把「跑没跑 / 下次几点 / 凭据齐不齐」摊开给用户看 */
  function renderAutoDiag(a) {
    const box = $("#autoDiag");
    const tag = $("#autoStateTag");
    if (!box) return;
    if (!a || a.ok === false) {
      box.innerHTML = `<div class="auto-row"><span class="auto-k">状态</span><span class="auto-v">${escapeHtml((a && a.error) || "读取失败")}</span></div>`;
      if (tag) tag.textContent = "";
      return;
    }
    const rows = [];
    const at = Number(a.last_at);
    const when = isFinite(at) && at > 0
      ? new Date(at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
      : "";
    if (a.ran) {
      rows.push(["上次运行", when + "　" + (a.last_title || "")]);
    } else {
      rows.push(["上次运行", "从未运行过", "warn"]);
    }
    if (a.queued) {
      rows.push(["下次排期", `已排上（${a.queued_state}${a.queued_attempts ? " · 重试 " + a.queued_attempts : ""}）`]);
    } else {
      rows.push(["下次排期", `没有待执行任务（${a.queued_state}）`, "warn"]);
    }
    rows.push(["定时开关", a.enabled ? `开 · 每天 ${a.time}${a.jitter_min > 0 ? ` + 0~${a.jitter_min} 分钟` : "（准点）"}` : "关", a.enabled ? "ok" : "warn"]);
    // 五家的就绪明细（用户 m21921）：老接口只回 wb_ready / trae_ready，
    // 新接口回 providers 数组 —— 两套都兼容，字段缺了就退回单条。
    const provs = Array.isArray(a.providers) ? a.providers : [];
    if (provs.length) {
      provs.forEach((p) => {
        if (!p || !p.label) return;
        rows.push([p.label, p.detail || (p.ready ? "就绪" : "未就绪"), p.ready ? "ok" : "warn"]);
      });
    } else {
      rows.push(["WorkBuddy", a.wb_ready ? "凭据就绪" : "未登录（会跳过）", a.wb_ready ? "ok" : "warn"]);
      rows.push(["Trae Solo", a.trae_ready
        ? `就绪${a.trae_has_session ? " · 有 session 可续期" : a.trae_has_token ? " · 仅 token（8 小时后失效）" : ""}`
        : `未就绪（device_id=${a.trae_device_id || "空"}）`, a.trae_ready ? "ok" : "warn"]);
    }

    box.innerHTML = rows.map(([k, v, cls]) =>
      `<div class="auto-row"><span class="auto-k">${escapeHtml(k)}</span><span class="auto-v ${cls || ""}">${escapeHtml(v)}</span></div>`
    ).join("") + (a.ran && a.last_body
      ? `<pre class="auto-body">${escapeHtml(a.last_body)}</pre>` : "");

    if (tag) {
      tag.textContent = a.ran ? "有运行记录" : "从未运行";
      tag.style.color = a.ran ? "var(--ok)" : "var(--warn,#ffb74d)";
    }
  }

  async function loadAutoDiag() {
    try {
      const r = await api("/api/auto_status");
      renderAutoDiag(r);
    } catch (err) {
      renderAutoDiag({ ok: false, error: err.message || "本地模式不支持" });
    }
  }
  if ($("#autoRefresh")) on("#autoRefresh", "click", (e) => { e.preventDefault(); loadAutoDiag(); });
  if ($("#autoRunNow")) on("#autoRunNow", "click", (e) => {
    e.preventDefault();
    if (window.SignHubApi && window.SignHubApi.runAutoNow) {
      window.SignHubApi.runAutoNow();
      toast("已提交后台签到，完成后会有通知，稍后点「刷新」看结果");
      setTimeout(loadAutoDiag, 6000);
    } else {
      toast("仅 App 内可用");
    }
  });
  on("#saveSched", "click", async () => {
    const h = String(state.wheelHour.get()).padStart(2, "0");
    const m = String(state.wheelMin.get()).padStart(2, "0");
    const jit = parseInt($("#schedJitter").value || "0", 10) || 0;
    try {
      await api("/api/config", { method: "POST", body: JSON.stringify({ scheduler: { enabled: $("#autoOn").checked, times: [`${h}:${m}`], jitter_min: jit } }) });
      const tail = jit > 0 ? `（随机浮动 0~${jit} 分钟）` : "（准点）";
      toast(`已保存：每天 ${h}:${m} ${$("#autoOn").checked ? "自动签到" : "（定时已关闭）"}${tail}`);
      applyWheelLock(true);        // 存完自动锁回去
      await loadAll();
    } catch (err) { toast("保存失败：" + err.message); }
  });

  /* 「设置定时」：解锁 / 取消修改。锁定态点它 → 解锁；解锁态点它 → 还原到已保存时间并锁定。 */
  on("#editSched", "click", async () => {
    if (!state.wheelLocked) {
      const t = (state.status && state.status.scheduler && state.status.scheduler.times[0]) || "08:30";
      const [hh, mm] = t.split(":").map((x) => parseInt(x, 10));
      state.wheelHour.setTo(isNaN(hh) ? 8 : hh);
      state.wheelMin.setTo(isNaN(mm) ? 30 : mm);
      // 随机浮动也一起还原到已保存值，再锁上
      paintJitter((state.status && state.status.scheduler && state.status.scheduler.jitter_min) || 0);
      applyWheelLock(true);
      toast("已取消修改");
      return;
    }
    applyWheelLock(false);
    toast("滚轮和随机浮动已解锁，可以调了");
  });
  on("#saveNotify", "click", async () => {
    try {
      await api("/api/config", {
        method: "POST",
        body: JSON.stringify({
          notify: {
            enabled: true,
            kind: $("#notifyKind").value,
            webhook_url: $("#webhook").value.trim(),
            secret: $("#notifySecret").value.trim(),
          },
        }),
      });
      toast("通知已保存");
      renderNotifyHint();
    } catch (err) { toast("保存失败：" + err.message); }
  });
  on("#testNotify", "click", async () => {
    const btn = $("#testNotify");
    const out = $("#notifyResult");
    btn.disabled = true;
    out.textContent = "发送中…";
    out.style.color = "";
    try {
      // 先保存再测，避免「改了没存 → 测的是旧配置」
      await api("/api/config", {
        method: "POST",
        body: JSON.stringify({
          notify: {
            enabled: true,
            kind: $("#notifyKind").value,
            webhook_url: $("#webhook").value.trim(),
            secret: $("#notifySecret").value.trim(),
          },
        }),
      });
      const r = await api("/api/test_notify", { method: "POST" });
      if (r.ok) {
        out.textContent = "✓ 已发送，去群里看看";
        out.style.color = "var(--ok, #37d67a)";
        toast("测试通知已发送");
      } else {
        out.textContent = "✗ " + (r.error || "发送失败");
        out.style.color = "var(--danger, #ff6b6b)";
        toast("通知发送失败");
      }
    } catch (err) {
      out.textContent = "✗ " + err.message;
      out.style.color = "var(--danger, #ff6b6b)";
      toast("失败：" + err.message);
    } finally { btn.disabled = false; }
  });
  on("#notifyKind", "change", renderNotifyHint);
  const webhookInp = $("#webhook");
  if (webhookInp) webhookInp.addEventListener("input", renderNotifySummary);

  /* 通知面板：默认收起，只露标题行 + 摘要，点标题再弹出信息框 */
  const ntToggle = $("#notifyToggle");
  if (ntToggle) {
    ntToggle.addEventListener("click", () => {
      const panel = $("#notifyPanel");
      const fold = $("#notifyFold");
      if (!panel || !fold) return;
      const willOpen = !panel.classList.contains("open");
      panel.classList.toggle("open", willOpen);
      foldToggle(fold, willOpen);
    });
  }
  /* 自动签到状态：同样默认收起（用户 m14411），点标题栏下拉全部信息 */
  const atToggle = $("#autoToggle");
  if (atToggle) {
    atToggle.addEventListener("click", () => {
      const panel = $("#autoPanel");
      const fold = $("#autoFold");
      if (!panel || !fold) return;
      const willOpen = !panel.classList.contains("open");
      panel.classList.toggle("open", willOpen);
      foldToggle(fold, willOpen);
      // 展开时才去问后台状态：默认收起时不该白跑一次 /api/auto_status
      if (willOpen) loadAutoDiag();
    });
  }
  on("#refreshAll2", "click", () => loadAll());

  /* Token 健康：默认收起（用户 m06523）。加了智谱/千帆后一共 7 行，
     全部铺开会把「签到配置」挤到很下面，所以只露标题行，点标题展开。
     折叠机制与「通知」「自动签到」同一套 .fold + foldToggle()。 */
  const thToggle = $("#thToggle");
  if (thToggle) {
    thToggle.addEventListener("click", () => {
      const panel = $("#thPanel");
      const fold = $("#thFold");
      if (!panel || !fold) return;
      const willOpen = !panel.classList.contains("open");
      panel.classList.toggle("open", willOpen);
      foldToggle(fold, willOpen);
    });
  }

  /* 积分资产 */
  // 刷新余额的统一实现：拉 WorkBuddy 资产卡 → 让后端重查两家「当前持有积分」并落库 → 整页回读。
  // 顺序有讲究：先 refresh_totals 把新数字写进当天记录，再 loadAll 才能读到新值；
  // 最后再 force 拉一次资产卡，避免 loadAll 里 cached_only 的缓存把刚拿到的新值盖回去。
  async function refreshBalances(opts) {
    const silent = !!(opts && opts.silent);
    let okFlag = false;
    try {
      // 三段里 refresh_totals（后端落库新积分）与 wb_assets?refresh=1（拉资产卡）
      // 互不依赖，同时发出去；等两者都回来再 loadAll，保证它读到的是刚写库的数字。
      // 原来串行 ≈ 0.6 + 0.5 + 1.2 = 2.3s，并行后 ≈ max(0.6, 1.2) + 0.5 = 1.7s。
      // P1-2：refresh_totals 会真打四家上游并写库，必须用 POST（GET 会被浏览器预取、
      // SW 重放、地址栏直接访问误触发）。
      const pTotals = api("/api/refresh_totals", { method: "POST" }).catch(() => null);
      const pAssets = api("/api/wb_assets?refresh=1").catch(() => null);
      const fresh = await pTotals;
      if (fresh && fresh.ok === false) okFlag = false; else okFlag = true;
      await loadAll({ light: true });
      const a = await pAssets;
      if (a) renderAssets(a);          // loadAll 里的 cached_only 可能比它旧，这里覆盖回来
      if (a && a.ok === false) okFlag = false;
      if (!silent) {
        // 提示里要报「所有已登录的 agent」，不能只报 WB 和 Trae ——
        // /api/refresh_totals 其实四家都回（workbuddy/traesolo/qoder/minimax），
        // 但只列前两家会让用户以为 Qoder、MiniMax 没刷到。
        // 口径（用户 m13804）：每家只报**主账号**（account_key == "default"）那一个数，
        // 多账号不求和 —— 多号相加出来的数字和资产卡、首页头卡都对不上，只会让人更困惑。
        const got = (fresh && fresh.totals) || {};
        const provs = (state.status && state.status.providers) || [];
        const parts = [];
        if (provs.length) {
          provs.forEach((p) => {
            if (!p || !p.key) return;
            // 没登录（ready !== true）的不报：这不是「刷到了 0」，而是压根没配
            if (p.ready !== true) return;
            parts.push(`${p.label || p.key} ${fmtNum(mainAcctPoints(p))}`);
          });
        } else {
          const KEY_NAME = { workbuddy: "WorkBuddy", traesolo: "Trae Solo", qoder: "Qoder CN", minimax: "MiniMax Code", huawei: "华为码道" };
          Object.keys(KEY_NAME).forEach((k) => {
            if (typeof got[k] === "number") parts.push(`${KEY_NAME[k]} ${got[k]}`);
          });
        }
        if (parts.length) toast("已更新：" + parts.join(" · "), 3000);
        else if (a && a.ok === false && a.error) toast(String(a.error));
        else if (fresh && fresh.ok === false) toast("刷新失败");
      }
    } catch (err) {
      okFlag = false;
      if (!silent) toast("刷新失败：" + err.message, 3000);
    }
    const th = $("#thState");
    if (th) th.textContent = "本地凭据";
    return okFlag;
  }

  async function refreshAssetsClick(btn) {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "查询中…";
    const label = $("#assetState");
    if (label) label.textContent = "查询中…";
    await refreshBalances({ silent: false });
    btn.disabled = false;
    btn.textContent = old;
  }
  const ra1 = $("#refreshAssets");
  if (ra1) ra1.addEventListener("click", () => refreshAssetsClick(ra1));
  // 设置页「Token 健康」里也放了一个刷新按钮，两处行为一致
  const ra2 = $("#refreshAssets2");
  if (ra2) ra2.addEventListener("click", () => refreshAssetsClick(ra2));

  /* 成长中心 */
  on("#growthLoad", "click", async () => {
    const btn = $("#growthLoad");
    btn.disabled = true;
    $("#growthState").textContent = "查询中…";
    await loadGrowth(true);
    btn.disabled = false;
  });
  /* 账号切换（用户 m14411） */
  on("#growthAcctSwitch", "click", async () => {
    // 用户 m05253：点之前总是重拉一次。原来只在 state.wbAccounts 为空时才拉，
    // 而它首次进页面就被填成「1 个账号」的快照，之后登录第二个账号再也不会更新，
    // 于是永远弹「当前只有 1 个 WorkBuddy 账号」。/api/wb_accounts 是本地毫秒级接口。
    await loadWbAccounts();
    openWbAcctSheet();
  });
  on("#growthAcctManage", "click", () => {
    // 管理（增删账号）走原生多账号页；网页端没有桥时给个提示
    if (window.SignHubApi && window.SignHubApi.openAccounts) {
      window.SignHubApi.openAccounts();
    } else {
      toast("请到「配置 → 签到配置 → WorkBuddy」里管理账号");
    }
  });

  async function growthRun(url, btnSel, doneText) {
    const btn = $(btnSel);
    const out = $("#growthResult");
    btn.disabled = true;
    out.textContent = "执行中，请稍候…";
    out.style.color = "";
    try {
      const r = await api(url, { method: "POST" });
      const okN = Number(r.done != null ? r.done : r.claimed || 0);
      const tot = Number(r.total || 0);
      out.textContent = `✓ ${doneText}：${okN}/${tot}` + (r.error ? " · " + r.error : "");
      out.style.color = "var(--ok, #37d67a)";
      toast(`${doneText}完成 ${okN}/${tot}`);
      await loadGrowth(true);
    } catch (err) {
      out.textContent = "✗ " + err.message;
      out.style.color = "var(--danger, #ff6b6b)";
      toast("执行失败：" + err.message);
    } finally { btn.disabled = false; }
  }

  on("#growthRun", "click", () => growthRun("/api/growth/room", "#growthRun", "任务完成"));
  on("#growthClaim", "click", () => growthRun("/api/growth/claim", "#growthClaim", "领取奖励"));
  on("#growthDailyRun", "click", () => growthRun("/api/growth/daily/run", "#growthDailyRun", "每日任务"));

  // 豆包任务面板已下线（服务端 710010702，无客户端侧修复空间），不再绑定按钮。

  /* 一键全部签到 */
  on("#oneKey", "click", async () => {
    const btn = $("#oneKey");
    if (btn.classList.contains("busy")) return;
    btn.classList.add("busy");
    $(".ok-ico", btn).innerHTML = `<span class="spin"></span>`;
    // 签到时各家是并行发的，按钮上给出「正在签到…」而不是一直显示「一键全部签到」，
    // 否则用户看不出点没点上（原来只有图标转，文字不动，很容易被当成没反应）。
    const txt = $(".ok-text", btn);
    const txtOld = txt ? txt.textContent : "";
    if (txt) txt.textContent = "正在签到…";
    try {
      const res = await api("/api/checkin/all", { method: "POST" });
      // 今天已经全签完：后端直接短路返回，不再打一遍上游（用户 m09631）。
      // 这里只弹提示、不动卡片，也不触发余额刷新（本来就没签到，数字不会变）。
      if (res && res.skipped) {
        toast(res.message || "你今天已经完成全部签到啦！", 3200);
        if (txt) txt.textContent = txtOld || "一键全部签到";
        $(".ok-ico", btn).innerHTML = `<svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5 L9.5 18 L20 6" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
        btn.classList.remove("busy");
        return;
      }
      const keys = Object.keys(res.results || {});
      for (let i = 0; i < keys.length; i++) {
        // 同一 provider 可能有多张账号卡（多账号堆叠），全部一起更新
        const svcs = $$(`.svc[data-key="${keys[i]}"]`);
        if (!svcs.length) continue;
        const r = res.results[keys[i]];
        svcs.forEach((svc) => {
          const b = $(".ckbtn", svc);
          // 一键签到也要按真实结果上终态（用户 m22039：失败不能静默打勾）。
          // 多账号的后继卡本来就没有 .ckbtn，$() 会返回 null，这里天然跳过。
          if (b) {
            b.classList.remove("busy", "fail", "done");
            if (r.status === "fail") {
              b.classList.add("fail");
            } else {
              b.classList.add("done");
              setTimeout(() => burst(b), 120);
            }
          }
          markSvcResult(svc, r);
        });
        await new Promise((r2) => setTimeout(r2, 170));
      }
      // 一键签到后统一重排：成功的堆起来，失败的继续展开。
      // 先清掉所有手动展开/收起的显式值 —— 否则用户之前点开过某组，
      // 签完之后它还摊着，与「签完就叠起来」的预期相反（用户 m18704）。
      state.stackOpen = {};
      stackLayout();
      const ok = keys.filter((k) => ["success", "already"].includes(res.results[k].status)).length;
      toast(`完成 ${ok}/${keys.length} 个服务`);
      const succKeys = keys.filter((k) => res.results[k].status === "success");
      if (succKeys.length) { confettiBurst(succKeys.length >= 2); feedPet(true); }
      // 签到完顺手把余额拉一遍（各家刚签完，数字都变了），但别拖住按钮复原
      loadAll({ light: true }).catch(() => {});
    } catch (err) { toast("一键签到失败：" + err.message, 3200); }
    $(".ok-ico", btn).innerHTML = `<svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5 L9.5 18 L20 6" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    if (txt) txt.textContent = txtOld || "一键全部签到";
    btn.classList.remove("busy");
  });

  /* ============================================================
     加载 / 连接状态
     ============================================================ */
  function setConn(ok, msg) {
    const dot = $("#connDot");
    if (dot) { dot.classList.toggle("ok", ok); dot.classList.toggle("bad", !ok); }
    const box = $("#connError");
    if (box) {
      box.hidden = ok;
      if (!ok && msg) $("#connErrorMsg").textContent = msg;
    }
  }

  const NOTIFY_HINTS = {
    wecom: ["企业微信群机器人", "群设置 → 群机器人 → 添加 → 复制 Webhook 地址"],
    dingtalk: ["钉钉群机器人", "群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook"],
    feishu: ["飞书群机器人", "群设置 → 群机器人 → 添加 → 自定义机器人 → 复制 Webhook"],
    pushplus: ["PushPlus", "pushplus.plus 微信扫码登录 → 复制 token → 地址填 https://www.pushplus.plus/send/<token>（也可只填 token）"],
    serverchan: ["Server酱", "sct.ftqq.com 微信扫码登录 → 复制 SendKey → 地址填 https://sctapi.ftqq.com/<SendKey>.send"],
    bark: ["Bark（iOS）", "iPhone 装 Bark App → 首页复制那串 key → 地址填 https://api.day.app/<key>（也可只填 key）"],
    wxpusher: ["WxPusher", "wxpusher.zjiecode.com 微信扫码登录 → 建应用拿 appToken → 地址填 https://wxpusher.zjiecode.com/api/send/message/<appToken>；「密钥」栏填你的 UID（多个逗号分隔）"],
    generic: ["通用 / 自建", "会 POST JSON：{title, text, content}"],
  };

  const NOTIFY_SHORT = {
    wecom: "企业微信", dingtalk: "钉钉", feishu: "飞书", pushplus: "PushPlus",
    serverchan: "Server酱", bark: "Bark", wxpusher: "WxPusher", generic: "自定义",
  };

  /* 收起状态下标题行右侧的摘要：渠道 + 是否已配置，不用展开就能看明白 */
  function renderNotifySummary() {
    const el = $("#notifySummary");
    if (!el) return;
    const cfg = (state.config && state.config.notify) || {};
    const sel = $("#notifyKind");
    const inp = $("#webhook");
    const kind = (sel && sel.value) || cfg.kind || "wecom";
    const name = NOTIFY_SHORT[kind] || "通知";
    const url = (inp ? inp.value : (cfg.webhook_url || "")) || "";
    const has = !!String(url).trim();
    el.textContent = has ? `${name} · 已配置` : `${name} · 未填地址`;
    el.style.color = has ? "var(--muted)" : "var(--warn, #ffb020)";
  }

  function renderNotifyHint() {
    const kind = ($("#notifyKind") || {}).value || "wecom";
    const [name, how] = NOTIFY_HINTS[kind] || NOTIFY_HINTS.generic;
    const hint = $("#notifyHint");
    if (hint) hint.textContent = `${name}：${how}`;
    const wrap = $("#notifySecretWrap");
    // 钉钉 = 加签密钥；WxPusher = 收件人 UID（多个逗号分隔）
    if (wrap) wrap.hidden = kind !== "dingtalk" && kind !== "wxpusher";
    const secLabel = wrap ? wrap.firstChild : null;
    if (secLabel && secLabel.nodeType === 3) {
      secLabel.textContent = kind === "wxpusher" ? "WxPusher 收件人 UID（必填）" : "钉钉加签密钥（可选）";
    }
    const secInp = $("#notifySecret");
    if (secInp) {
      secInp.placeholder = kind === "wxpusher"
        ? "UID_xxx，多个用英文逗号分隔"
        : "SEC 开头的加签密钥；机器人没开加签就留空";
    }
    const ph = {
      wecom: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
      dingtalk: "https://oapi.dingtalk.com/robot/send?access_token=...",
      feishu: "https://open.feishu.cn/open-apis/bot/v2/hook/...",
      pushplus: "https://www.pushplus.plus/send/<你的token>",
      serverchan: "https://sctapi.ftqq.com/<SendKey>.send",
      bark: "https://api.day.app/<你的key>",
      wxpusher: "https://wxpusher.zjiecode.com/api/send/message/<appToken>",
      generic: "https://你的服务/webhook",
    }[kind] || "";
    const inp = $("#webhook");
    if (inp && ph) inp.placeholder = ph;
    renderNotifySummary();
  }

  /* ============================================================
     积分资产 / 过期预警
     ============================================================ */
  function fmtNum(v) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    return Number.isInteger(n) ? String(n) : n.toFixed(1);
  }

  /**
   * 某一家「主账号」的持有积分。
   * 用户 m13804：刷新余额的提示只报 4 个主账号的数，不要多账号求和 ——
   * 求和出来的数字既跟资产卡对不上，也跟首页头卡对不上。
   * 主账号 = /api/status 的 provider.accounts 里 account_key === "default" 那一条
   * （手机端 LocalApi.providerRow() 恒把它排在第一位）。
   * WorkBuddy 的账号键可能是它自己的账号 id 而不是 "default"，所以再退一步取
   * accounts[0]（后端已按「主账号在前」排过序）。没有 accounts 的老后端才用 total_points。
   */
  function mainAcctPoints(p) {
    if (!p) return "—";
    const accts = Array.isArray(p.accounts) ? p.accounts : [];
    const main = accts.find((a) => a && a.account_key === "default") || accts[0] || null;
    const v = main ? main.total_points : p.total_points;
    const n = Number(v);
    return (v === null || v === undefined || !isFinite(n) || n < 0) ? "—" : fmtNum(n);
  }

  /** 卡片右侧展示：持有积分优先（更稳），没有才显示当日增量。 */
  function svcPointsHTML(p) {
    // 注意：后端「读不到」时给的是 null，而 Number(null) === 0 会被误当成「0 积分」。
    // 必须先排除 null / undefined / 空串，再判断数值。
    // 另外 Qoder 给 "100 Credits"、MiniMax 给 "连签 1 天"，Number() 是 NaN，
    // 这种带单位的字符串要原样显示，不能退化成「只剩今日增量」。
    const raw = p.total_points;
    const empty = (raw === null || raw === undefined || raw === "");
    const total = empty ? NaN : Number(raw);
    if (isFinite(total) && total >= 0) {
      const sub = p.points ? `<i class="svc-delta">${escapeHtml(p.points)}</i>` : "";
      return `<span class="svc-points has">${fmtNum(total)}${sub}</span>`;
    }
    if (!empty && typeof raw === "string" && raw.trim()) {
      const sub = p.points ? `<i class="svc-delta">${escapeHtml(p.points)}</i>` : "";
      return `<span class="svc-points has">${escapeHtml(raw.trim())}${sub}</span>`;
    }
    // p.points 来自后端（含上游响应片段），必须转义后再拼进 innerHTML
    return `<span class="svc-points ${p.points ? "has" : ""}">${escapeHtml(p.points || "")}</span>`;
  }

  /**
   * 资产卡翻转：点一下从「当前可用」翻到「累计已用」，再点翻回来。
   *
   * 动画要求（用户 m06822）：以原来大小稍微变大 → 随机左右翻转 → 缩回原尺寸。
   * 实现方式是给 .asset-flip 挂一段 keyframes，旋转角度由 --rot-from/--rot-to 两个
   * 自定义属性喂进去；每次点击累加「整圈 + 半圈」，所以奇偶次点击正好在正/反面之间切换，
   * 而整圈数（1~2 圈）与方向（左/右）随机，翻起来不呆板。
   */
  /**
   * 资产卡交互（用户 m14411 重定义）：
   *   单击 → 翻面，**停在当前面不动**（不再每次随机多转几圈，用户嫌晃）
   *   双击 → 进入「同一 AGENT 的多账号宫格」；已在宫格里则双击退出
   *
   * 单击/双击判定：浏览器 dblclick 在移动 WebView 上不可靠（部分 ROM 不派发），
   * 所以自己用时间窗判定 —— 220ms 内第二次点同一张卡算双击，第一次的翻转要回滚。
   * 回滚方式是「反向翻回去」，比直接清 transform 平滑，不会闪。
   */
  const DBL_MS = 240;
  let lastTapSig = "";
  let lastTapAt = 0;
  let tapTimer = 0;

  function flipAssetCard(card, back) {
    const flip = card.querySelector(".asset-flip");
    if (!flip) return;
    if (card.dataset.flipping === "1") return;   // 动画途中不叠第二次，否则角度会跳
    const from = Number(flip.dataset.rot || 0);
    // 目标面：back=true 翻到背面，back=false 翻到正面；undefined = 在两面之间切换
    let toBack;
    if (typeof back === "boolean") toBack = back;
    else toBack = Math.abs(((from % 360) + 360) % 360 - 180) > 1;
    // 只转半圈（180°），方向固定「向右」——用户要求「停在当前面不动」，
    // 不要原来那种随机方向 + 1~2 整圈的花活，翻完像没停下一样晃眼。
    const dir = 1;
    const norm = ((from % 360) + 360) % 360;
    const targetNorm = toBack ? 180 : 0;
    const delta = (targetNorm - norm + 360) % 360;
    const finalTo = from + dir * delta;
    if (NOANIM) {
      flip.dataset.rot = String(finalTo);
      flip.style.setProperty("--rot", finalTo + "deg");
      card.classList.toggle("show-back", toBack);
      return;
    }
    card.dataset.flipping = "1";
    flip.style.setProperty("--rot-from", from + "deg");
    flip.style.setProperty("--rot-to", finalTo + "deg");
    flip.classList.remove("spin");
    void flip.offsetWidth;      // 强制回流，连点两次也能重新起动画
    flip.classList.add("spin");
    const done = () => {
      flip.classList.remove("spin");
      flip.dataset.rot = String(finalTo);
      flip.style.setProperty("--rot", finalTo + "deg");
      card.classList.toggle("show-back", toBack);
      delete card.dataset.flipping;
    };
    flip.addEventListener("animationend", done, { once: true });
    // 兜底：WebView 里 animationend 偶发丢失，900ms 后强制落位
    setTimeout(() => { if (card.dataset.flipping === "1") done(); }, 900);
  }

  function bindAssetFlip(grid) {
    // 只绑一次，绑在 document 上（v2.20）。
    //
    // 旧实现把 click 绑在 grid 元素上，每次进宫格又 `grid.dataset.flipBound = ""`
    // 清掉去重标记再绑一次 —— 而 #assetMultiGrid 是**同一个 DOM 节点**，
    // 于是第二次进宫格后它身上挂了两个 click 监听：
    //   第 1 个：记下 lastTapCard，启动 240ms 定时器
    //   第 2 个：看到 lastTapCard 就是自己、间隔 0ms < 240ms → 判定「双击」→ exitAssetMulti()
    // 症状就是用户报的「双击进多账号页，单击一下就退出」。
    //
    // 现在改成 document 级委托 + 模块级 once 标记，节点被清空重填也不会重复挂。
    if (bindAssetFlip.done) return;
    bindAssetFlip.done = true;
    const onTap = (target) => {
      const card = target && target.closest ? target.closest(".asset-card") : null;
      if (!card) return;
      const key = card.dataset.key || "";
      const now = performance.now();
      // 双击判定：同一张卡（按 data-key + data-acct 认，DOM 节点可能被重建）、间隔 < DBL_MS
      const sig = key + "|" + (card.dataset.acct || "");
      if (lastTapSig === sig && now - lastTapAt < DBL_MS) {
        clearTimeout(tapTimer);
        lastTapSig = "";
        lastTapAt = 0;
        // 双击时把第一次单击已经翻过去的动作收回来，再进/出宫格
        const multi = $("#assetMulti");
        const inMulti = !!(multi && !multi.hidden);
        if (inMulti) { exitAssetMulti(); return; }
        openAssetMulti(key);
        return;
      }
      lastTapSig = sig;
      lastTapAt = now;
      // 延后到窗口结束再翻：双击时不能留下「先翻一下又弹回去」的闪动
      clearTimeout(tapTimer);
      tapTimer = setTimeout(() => {
        if (lastTapSig === sig && performance.now() - lastTapAt >= DBL_MS - 20) {
          const alive = document.querySelector(
            `.asset-card[data-key="${key}"][data-acct="${card.dataset.acct || ""}"]`);
          flipAssetCard(alive || card);
        }
      }, DBL_MS);
    };
    document.addEventListener("click", (e) => {
      const card = e.target && e.target.closest ? e.target.closest(".asset-card") : null;
      if (!card) return;
      // 只认资产相关的两个网格，别的 .asset-card（如果以后有）不受影响
      const grid = card.closest("#assetGrid, #assetMultiGrid");
      if (!grid) return;
      onTap(card);
    });
    // 键盘可达：Enter / 空格
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      const card = e.target && e.target.closest ? e.target.closest(".asset-card") : null;
      if (!card) return;
      if (!card.closest("#assetGrid, #assetMultiGrid")) return;
      e.preventDefault();
      flipAssetCard(card);
    });
  }

  /* ============================================================
     资产宫格（用户 m14411）
     双击某张卡 → 只显示「同一 AGENT」的账号卡，一张卡一个账号。
     只有主账号时提示「你当前尚未配置多账号。」并退出宫格。
     ============================================================ */
  const ASSET_LABEL = { workbuddy: "WorkBuddy", traesolo: "Trae Solo", qoder: "Qoder CN", minimax: "MiniMax Code", huawei: "华为码道", zhipu: "智谱清言", qianfan: "百度千帆" };
  // BUGS-v2.63 P3-5：这里以前只有五家，智谱/千帆落到 `|| "wb"` 兜底 —— 多账号宫格会把
  // 它们的账号图标画成 WorkBuddy 的绿配色。配色类本身早就在 app.css 里
  // （`.asset-ico.zp` / `.asset-ico.qf`），只是这张表没登记，所以只需要补两行。
  const ASSET_CLS = { workbuddy: "wb", traesolo: "tr", qoder: "qd", minimax: "mm", huawei: "hw", zhipu: "zp", qianfan: "qf" };

  function openAssetMulti(key) {
    const box = $("#assetMulti");
    const grid = $("#assetMultiGrid");
    const title = $("#assetMultiTitle");
    if (!box || !grid) return;
    const prov = ((state.status && state.status.providers) || []).find((p) => p && p.key === key);
    const accts = (prov && Array.isArray(prov.accounts)) ? prov.accounts : [];
    const n = Math.max(accts.length, (prov && prov.ready) ? 1 : 0);
    if (n <= 1) {
      toast("你当前尚未配置多账号。");
      return;
    }
    const cls = ASSET_CLS[key] || "wb";
    const label = ASSET_LABEL[key] || (prov && prov.label) || key;
    if (title) title.textContent = `${label} · ${n} 个账号`;
    // 每个账号一张卡：正面当前可用（账号行 total_points），背面累计已用（used_points）
    grid.innerHTML = accts.map((a, i) => {
      const cur = Number(a && a.total_points);
      const used = Number(a && a.used_points);
      const okCur = isFinite(cur) && cur >= 0;
      const okUsed = isFinite(used) && used >= 0;
      const done = a && (a.status === "success" || a.status === "already");
      const cap = okCur ? "当前可用" : (done ? "已签到" : "待刷新");
      // 卡名固定用「账号 N」（用户 m16637 定案）：主页与宫格都是每天扫一眼的场景，
      // 编号最短、扫视成本最低；手机号打码串只出现在「配置 → 多账号管理」里
      // （那里是管理场景，需要一眼认出是哪个号）。
      const aname = "账号 " + (i + 1);
      return `<div class="asset-card" role="button" tabindex="0" data-key="${escapeHtml(key)}" data-acct="${escapeHtml(String(a && a.account_key || i))}" aria-label="${escapeHtml(aname)}" style="--am-i:${i}">` +
             `<div class="asset-flip">` +
             `<div class="asset-face front"><span class="asset-tag"><i class="asset-ico ${cls}"></i>${escapeHtml(aname)}</span>` +
             `<span class="asset-num">${okCur ? escapeHtml(String(Math.floor(cur))) : "—"}</span>` +
             `<span class="asset-cap">${escapeHtml(cap)}</span></div>` +
             `<div class="asset-face back"><span class="asset-tag"><i class="asset-ico ${cls}"></i>${escapeHtml(aname)}</span>` +
             `<span class="asset-num">${okUsed ? escapeHtml(String(Math.floor(used))) : "—"}</span>` +
             `<span class="asset-cap">累计已用</span></div>` +
             `</div></div>`;
    }).join("");
    bindAssetFlip(grid);
    // 入场过渡（用户 m16320）：宫格整体淡入 + 轻微上浮放大，卡片逐个错峰浮起。
    // 主网格是直接 hidden 的（display:none 无法播动画），所以只让「进入的那一层」动，
    // 视觉上就是一次干净的替换，不会两层叠在一起。
    box.hidden = false;
    const main = $("#assetGrid");
    if (main) main.hidden = true;
    if (!NOANIM) {
      box.classList.remove("am-in", "am-out");
      void box.offsetWidth;
      box.classList.add("am-in");
    }
  }

  function exitAssetMulti() {
    const box = $("#assetMulti");
    if (!box || box.hidden) return;
    const grid = $("#assetMultiGrid");
    const main = $("#assetGrid");
    const finish = () => {
      box.hidden = true;
      box.classList.remove("am-in", "am-out");
      if (grid) grid.innerHTML = "";
      if (main) {
        main.hidden = false;
        if (!NOANIM) {
          // 回到主网格也给一次「浮起」，让往返都有落点
          main.classList.remove("am-back");
          void main.offsetWidth;
          main.classList.add("am-back");
        }
      }
    };
    if (NOANIM) { finish(); return; }
    // 退场：宫格淡出 + 微缩 + 下沉，动画结束再换回主网格
    box.classList.remove("am-in");
    box.classList.add("am-out");
    setTimeout(finish, 240);
  }

  // 卡片是否已经画过一次（首屏占位卡 vs 真实数据）。
  // assetsPainted：画过就算（防止同一轮重复 bindAssetFlip）。
  // assetsReal：画的是真实数字（不是占位）—— 一旦为 true，后续「没数据」的分支
  // 就不许再用占位卡把它冲掉，否则刷新失败时用户会看到数字突然变「—」。
  let assetsPainted = false;
  let assetsReal = false;

  function renderAssets(a) {
    const grid = $("#assetGrid");
    const st = $("#assetState");
    const exp = $("#assetExpiring");
    if (!grid) return;
    // 与官网一致：可用积分向下取整（531.99 → 531），不做四舍五入
    const asInt = (v) => {
      const n = Number(v);
      return isFinite(n) ? String(Math.floor(n)) : "—";
    };
    // 各家额度：WorkBuddy 来自 /api/wb_assets（含分组明细），其余三家来自 /api/status
    // 的 total_points / used_points（读不到是 -1，不能显示成 0）。
    const provOf = (k) => {
      const list = (state.status && state.status.providers) || [];
      return list.find((p) => p && p.key === k) || null;
    };
    // 正面「当前可用」，背面「累计已用」；点一下翻转（放大 → 随机左右翻转 → 缩回原尺寸）
    // capOverride：没数据时的副标题。默认态用「待刷新」，避免显示成「读取失败」（那是在报错，
    // 而首屏本来就没查过，报错会误导用户以为坏了）。
    const assetCard = (cls, label, cur, used, ready, capOverride, key, cardCls) => {
      const okCur = typeof cur === "number" && cur >= 0;
      const okUsed = typeof used === "number" && used >= 0;
      const notReady = ready === false;
      const frontNum = okCur ? asInt(cur) : "—";
      const backNum = okUsed ? asInt(used) : "—";
      // 副标题优先级：有数 → 「当前可用」；调用方给了更具体的说法就用它；
      // 最后才是通用的「未配置 / 读取失败」。
      // 把 capOverride 提到 notReady 之前，是为了让「填了 Cookie 但被证实未登录」
      // 这类状态说得出原因（「未登录 · 重新登录」），而不是笼统的「未配置」
      // —— 用户看到「未配置」会以为凭据没保存，实际是登录态失效（BUGS-v2.46 P2）。
      const frontCap = okCur ? "当前可用"
        : (capOverride || (notReady ? "未配置" : "读取失败"));
      // 背面口径固定「累计已用」；数字读不到就显示「—」，不要写成「读取失败」——
      // 这个数字要等第一次「刷新余额」才落库，首屏未知是正常的。
      const backCap = "累计已用";
      const dk = key ? ` data-key="${escapeHtml(key)}"` : "";
      const ck = cardCls ? " " + cardCls : "";
      return `<div class="asset-card${ck}" role="button" tabindex="0"${dk} aria-label="${escapeHtml(label)}">` +
             `<div class="asset-flip">` +
             `<div class="asset-face front"><span class="asset-tag"><i class="asset-ico ${cls}"></i>${escapeHtml(label)}</span>` +
             `<span class="asset-num">${escapeHtml(frontNum)}</span><span class="asset-cap">${escapeHtml(frontCap)}</span></div>` +
             `<div class="asset-face back"><span class="asset-tag"><i class="asset-ico ${cls}"></i>${escapeHtml(label)}</span>` +
             `<span class="asset-num">${escapeHtml(backNum)}</span><span class="asset-cap">${escapeHtml(backCap)}</span></div>` +
             `</div></div>`;
    };
    // 四张卡一律画出来 —— 不管有没有数据。用户 m13271：默认态就要有 4 个卡片，
    // 而不是一片空白等「刷新余额」。数字读不到显示「—」，副标题说明是待刷新还是未配置。
    // placeholder=true 表示「还没拿到 /api/status」，此时 ready 一律当未知（不判未配置）——
    // 首屏还没问过后端，写成「未配置」是冤枉用户。
    const paintCards = (wbCur, wbUsed, capOverride, placeholder) => {
      // 四家的 ready 统一从 /api/status 取，不要给 WorkBuddy 开特例 ——
      // 否则首屏会出现「WorkBuddy 待刷新、其余三家未配置」这种前后不一致的副标题。
      const pWb = placeholder ? null : provOf("workbuddy");
      const pTrae = placeholder ? null : provOf("traesolo");
      const pQoder = placeholder ? null : provOf("qoder");
      const pMm = placeholder ? null : provOf("minimax");
      const pHw = placeholder ? null : provOf("huawei");
      // 用户 m06523：智谱清言与百度千帆也要有自己的资产卡。
      const pZp = placeholder ? null : provOf("zhipu");
      const pQf = placeholder ? null : provOf("qianfan");
      // BUGS-v2.63-288 P2：智谱 / 千帆只接进了手机端 LocalApi，电脑端 Python 后端
      // 没有这两家。硬画会多出两张永远「未配置」的假卡，点一下还会被
      // jumpToProvider 带着去找一张不存在的配置卡（renderProviderEditors 已按
      // knownKeys 过滤掉了它们）。所以按 /api/status 里有没有这一家决定画不画；
      // placeholder=true 表示还没拿到 status，此时未知，先画着避免布局跳动。
      const hasZp = placeholder || !!pZp;
      const hasQf = placeholder || !!pQf;
      grid.innerHTML = [
        assetCard("wb", "WorkBuddy", wbCur, wbUsed,
          pWb ? pWb.ready : (placeholder ? null : false), capOverride, "workbuddy"),
        assetCard("tr", "Trae Solo",
          pTrae ? pTrae.total_points : -1, pTrae ? pTrae.used_points : -1,
          pTrae ? pTrae.ready : (placeholder ? null : false), capOverride, "traesolo"),
        assetCard("qd", "Qoder CN",
          pQoder ? pQoder.total_points : -1, pQoder ? pQoder.used_points : -1,
          pQoder ? pQoder.ready : (placeholder ? null : false), capOverride, "qoder"),
        assetCard("mm", "MiniMax Code",
          pMm ? pMm.total_points : -1, pMm ? pMm.used_points : -1,
          pMm ? pMm.ready : (placeholder ? null : false), capOverride, "minimax"),
        // 智谱 / 千帆都没有「累计已用」口径（member_info 只给 left_score，DuMate 只给
        // totalPoints），所以背面永远是「—」，不硬编一个 0 冒充。
        ...(hasZp ? [assetCard("zp", "智谱清言",
          pZp ? pZp.total_points : -1, -1,
          pZp ? pZp.ready : (placeholder ? null : false), capOverride, "zhipu")] : []),
        ...(hasQf ? [assetCard("qf", "百度千帆",
          pQf ? pQf.total_points : -1, -1,
          pQf ? pQf.ready : (placeholder ? null : false), capOverride, "qianfan")] : []),
        // 华为码道放在最后：2 列网格里它会独占最后一行的左半格，
        // 用 .asset-card.wide 让它横跨两列，视觉上收尾（见 app.css）。
        // 积分接口就在签到同域（/chat/snap-manager/v1/statistics/plugin），
        // 用签到那份 cookie 就能读 —— 所以只有「会话失效」这一种读不到的情况。
        assetCard("hw", "华为码道",
          pHw ? pHw.total_points : -1, pHw ? pHw.used_points : -1,
          pHw ? pHw.ready : (placeholder ? null : false),
          // 会话失效（portal_ok=false）或接口没给数时，说清是「会话过期」而不是
          // 笼统的「读取失败」—— 前者是可行动的（重登一次）。
          // BUGS-v2.46 P2：不要再要求 ready===true —— 「被证实未登录」时 ready 就是 false，
          // 加了那个条件反而退回「未配置」，用户会以为凭据没保存。
          (pHw && (pHw.portal_ok === false || (pHw.ready && Number(pHw.total_points) < 0)))
            ? "会话已失效 · 重新登录" : capOverride,
          "huawei", "wide"),
      ].join("");
      bindAssetFlip(grid);
      assetsPainted = true;
    };
    // 有没有拿到过 /api/status —— 决定「未配置」这个副标题能不能说。
    // 没拿到时一律当未知（说「待刷新」），拿到之后才敢说「未配置」。
    const hasProvs = !!(state.status && state.status.providers && state.status.providers.length);
    if (!a || a.ok === false && a.pending) {
      // 首屏（还没查过）：画出 4 张占位卡，副标题「待刷新」。
      // 判据用 assetsReal 而不是 assetsPainted：已经画过真实数字时不许被占位冲掉。
      if (!assetsReal) paintCards(-1, -1, "待刷新", !hasProvs);
      if (st) st.textContent = "未查询";
      if (exp) exp.textContent = "点「刷新余额」拉取 WorkBuddy 积分（含赠送包到期时间）。";
      return;
    }
    if (!a || a.ok === false) {
      // 查询失败：卡片保留（有旧数据就继续显示旧数据），只是不覆盖成占位。
      if (!assetsReal) paintCards(-1, -1, "待刷新", !hasProvs);
      if (st) st.textContent = "查询失败";
      if (exp) exp.textContent = (a && a.error) ? String(a.error) : "WorkBuddy 未配置或接口异常。";
      return;
    }
    if (st) {
      let when = "";
      const ts = Number(a.updated_at);
      if (a.updated_at && isFinite(ts) && ts > 1e11) {
        const d = new Date(ts);
        const p = (n) => String(n).padStart(2, "0");
        when = `更新于 ${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
      } else if (typeof a.updated_at === "string" && a.updated_at) {
        when = "更新于 " + a.updated_at;
      }
      st.textContent = when || "已刷新";
    }
    // 真数据回来了：用同一套 paintCards 重画（四张卡结构完全一致）
    paintCards(Number(a.remaining), Number(a.used_total), null, false);
    assetsReal = true;
    // 官方汇总口径的分组（套餐基础 / 套餐赠送 / 购买 / 平台奖励）—— 只有拿到才显示
    const gr = Array.isArray(a.groups) ? a.groups.filter((g) => g && Number(g.capacity) > 0) : [];
    const planTag = a.plan_name ? ` · ${escapeHtml(a.plan_name)}` : "";
    const planLine = `<div class="asset-plan muted small">官方口径合计 ${escapeHtml(asInt(a.official_total || a.remaining))}${planTag}</div>`;
    const groupLine = gr.length
      ? '<div class="asset-groups">' + gr.map((g) => {
          const hi = Number(g.remain) > 0 ? " on" : "";
          return `<div class="asset-grow${hi}"><span class="asset-gname">${escapeHtml(g.name || g.kind)}</span>` +
                 `<span class="asset-gnum">${escapeHtml(asInt(g.remain))}</span>` +
                 `<span class="asset-gcap">/ ${escapeHtml(asInt(g.capacity))}</span></div>`;
        }).join("") + "</div>"
      : "";
    const grid2 = $("#assetGroups");
    if (grid2) grid2.innerHTML = groupLine ? planLine + groupLine : "";
    if (!exp) return;
    const ex = a.expiring;
    if (!ex) {
      exp.textContent = "暂无赠送包，或到期信息获取失败（不做臆测）。";
      exp.style.color = "";
      return;
    }
    const d7 = Number(ex.d7 || 0);
    const d30 = Number(ex.d30 || 0);
    const next = ex.next;
    if (d7 > 0 && next) {
      exp.textContent = `⏳ 7 天内将过期 ${fmtNum(d7)} 积分；最近一档 ${next.date} 到期 ${fmtNum(next.amount)}。`;
      exp.style.color = "var(--warn, #ffb74d)";
    } else if (d30 > 0) {
      exp.textContent = `⏳ 30 天内将过期 ${fmtNum(d30)} 积分。`;
      exp.style.color = "var(--warn, #ffb74d)";
    } else {
      exp.textContent = "✓ 近期没有即将过期的积分。";
      exp.style.color = "var(--ok, #37d67a)";
    }
  }

  async function loadAssets(force) {
    try {
      const a = await api("/api/wb_assets" + (force ? "?refresh=1" : ""));
      renderAssets(a);
      return a;
    } catch (err) {
      renderAssets({ ok: false, error: err.message });
      return null;
    }
  }

  /* ============================================================
     WorkBuddy 成长中心
     ============================================================ */
  function growthCls(s) {
    if (s === "claimed") return "g-done";
    if (s === "completed") return "g-claimable";
    if (s === "in_progress" || s === "accepted") return "g-todo";
    return "g-lock";
  }

  function renderGrowth(g) {
    const box = $("#growthTasks");
    const st = $("#growthState");
    if (!box) return;
    if (!g) {
      box.innerHTML = '<p class="muted small" style="margin:0">点「查看任务」加载 WorkBuddy 成长中心任务列表。</p>';
      if (st) st.textContent = "未查询";
      return;
    }
    if (g.ok === false) {
      box.innerHTML = `<p class="muted small" style="margin:0">${escapeHtml(g.error || "WorkBuddy 未配置或接口异常。")}</p>`;
      if (st) st.textContent = "查询失败";
      return;
    }
    const done = Number(g.completed || 0), total = Number(g.total || 0);
    if (st) st.textContent = `${done}/${total} 已完成` + (g.claimable ? ` · ${g.claimable} 可领` : "");
    const rows = Array.isArray(g.tasks) ? g.tasks : [];
    box.innerHTML = rows.map((t) => {
      const cls = growthCls(t.status);
      const prog = (t.target > 0) ? ` <span class="g-prog">${t.current}/${t.target}</span>` : "";
      const tag = t.can_claim ? '<i class="g-claim">可领</i>' : (t.not_auto ? '<i class="g-skip">跳过</i>' : "");
      const reward = t.reward ? ` <span class="g-reward">+${escapeHtml(String(t.reward))}</span>` : "";
      return `<div class="g-row ${cls}" title="${escapeHtml(t.hint || "")}"><span class="g-name">${escapeHtml(t.title || t.code)}${prog}${reward}</span>${tag}</div>`;
    }).join("");
  }

  async function loadGrowth(force) {
    try {
      const g = await api("/api/growth" + (force ? "?refresh=1" : ""));
      state.growthLoaded = true;
      renderGrowth(g);
      return g;
    } catch (err) {
      state.growthLoaded = true;
      renderGrowth({ ok: false, error: err.message });
      return null;
    }
  }

  /* ---- 成长中心账号切换（用户 m14411）----
     原来成长中心恒用「主账号」，其它账号根本做不了任务。
     现在顶部有一条账号栏，点开是选择器；切换后清空任务列表重新拉。 */
  async function loadWbAccounts() {
    try {
      const r = await api("/api/wb_accounts");
      state.wbAccounts = r;
      state.wbAccountsErr = "";
      paintGrowthAcct(r);
      return r;
    } catch (err) {
      // 用户 m05253：拉失败时不能把 state.wbAccounts 置 null ——
      // 置 null 之后 paintGrowthAcct 会画成「未登录 WorkBuddy」、
      // openWbAcctSheet 会弹「还没有登录任何 WorkBuddy 账号」，
      // 明明本地有账号却谎报没登录。保留上一次成功的结果，只把错误记下来。
      state.wbAccountsErr = err && err.message ? err.message : "读取账号列表失败";
      return null;
    }
  }

  function paintGrowthAcct(r) {
    const nameEl = $("#growthAcctName");
    if (!nameEl) return;
    const list = (r && Array.isArray(r.list)) ? r.list : [];
    const cur = list.find((a) => a && a.current) || list[0] || null;
    if (!cur) {
      nameEl.textContent = "未登录 WorkBuddy";
      return;
    }
    const n = list.length;
    nameEl.textContent = n > 1 ? `${cur.name}（共 ${n} 个）` : cur.name;
  }

  function openWbAcctSheet() {
    const r = state.wbAccounts;
    const list = (r && Array.isArray(r.list)) ? r.list : [];
    if (!list.length) {
      // 区分「真的没登录」与「接口读失败」：后者不能说成没登录（用户 m05253）
      if (state.wbAccountsErr) toast("读取账号列表失败：" + state.wbAccountsErr, 3200);
      else toast("还没有登录任何 WorkBuddy 账号");
      return;
    }
    if (list.length === 1) {
      toast("当前只有 1 个 WorkBuddy 账号；再登录一个就能在这里切换");
      return;
    }
    // 用底部弹层做选择器，避免再引一个 Activity
    let sheet = $("#wbAcctSheet");
    if (!sheet) {
      sheet = document.createElement("div");
      sheet.className = "wb-acct-mask";
      sheet.id = "wbAcctSheet";
      sheet.innerHTML = '<div class="wb-acct-panel"><div class="wb-acct-head"><b>切换 WorkBuddy 账号</b>' +
        '<button class="wb-acct-x" type="button">关闭</button></div><div class="wb-acct-list" id="wbAcctList"></div></div>';
      document.body.appendChild(sheet);
      sheet.addEventListener("click", (e) => {
        if (e.target === sheet || (e.target.closest && e.target.closest(".wb-acct-x"))) {
          sheet.classList.remove("show");
          setTimeout(() => { if (!sheet.classList.contains("show")) sheet.hidden = true; }, 260);
        }
      });
    }
    const box = $("#wbAcctList", sheet);
    if (box) {
      box.innerHTML = list.map((a) => {
        return `<button class="wb-acct-item ${a.current ? "on" : ""}" type="button" data-id="${escapeHtml(String(a.id || ""))}">` +
               `<span class="wa-name">${escapeHtml(a.name || "账号")}</span>` +
               `<span class="wa-uid muted small">${escapeHtml(String(a.uid || ""))}</span>` +
               (a.current ? '<span class="wa-cur">当前</span>' : "") +
               `</button>`;
      }).join("");
      box.querySelectorAll(".wb-acct-item").forEach((b) => {
        b.addEventListener("click", async () => {
          const id = b.dataset.id;
          if (!id) return;
          b.disabled = true;
          try {
            await api("/api/wb_accounts", { method: "POST", body: JSON.stringify({ id }) });
            sheet.classList.remove("show");
            setTimeout(() => { if (!sheet.classList.contains("show")) sheet.hidden = true; }, 260);
            toast("已切换账号，正在重新读取成长任务…");
            await loadWbAccounts();
            state.growthLoaded = false;
            renderGrowth(null);
            // 切换账号后余额要跟着换（用户 m18105）：资产卡与首页卡片读的都是
            // 「当前选中账号」的读数，不刷新就会继续显示上一个账号的数字。
            refreshBalances({ silent: true }).catch(() => {});
            await loadGrowth(true);
          } catch (err) {
            toast("切换失败：" + err.message, 3200);
          } finally {
            b.disabled = false;
          }
        });
      });
    }
    sheet.hidden = false;
    requestAnimationFrame(() => sheet.classList.add("show"));
  }

  // ── 豆包已下线（2026-11-20，服务端 710010702 封禁）──────────────────
  // 相关常量与空壳函数（DB_STATUS_CN / dbTaskCls / loadDbTasks / loadDbQuota /
  // renderDbTasks）已在 V2.25 清理删除：/api/status 早已不再返回 doubao，
  // 前端也没有任何调用点，留着只是噪音。

  /* 「最新一次 loadAll」的序号：晚回来的旧请求不许覆盖新数据（用户 m16810）。
     25 秒轮询与「从多账号管理页返回」的刷新会并发，轮询那一次可能先发出、后返回。 */
  let loadSeq = 0;

  async function loadAll(opts) {
    // light=true：让 /api/status 跳过 qoder/minimax 的上游体检（各 ~250ms）。
    // 只有「下拉刷新余额」这类只关心数字的场景才用，常规进页面仍走完整体检。
    const light = !!(opts && opts.light);
    // 并发护栏（用户 m16810）：25 秒轮询与「从多账号管理页返回」的刷新会撞车。
    // 轮询那一次可能在「新增账号之前」就发出了请求，却比刷新那一次更晚返回 ——
    // 于是它拿着旧数据（账号少一个）把刚渲染好的新卡片覆盖掉，用户看到的就是
    // 「添加了账号但签到页没变」，而且下一次轮询要再等 25 秒。
    // 用单调递增的序号标记「谁是最新一次」，旧请求回来时直接丢弃。
    const seq = ++loadSeq;
    const stale = () => seq !== loadSeq;
    try {
      renderVersion();
      // 四个接口互不依赖，一起发出去。原来 status → config → logs → stats 串行，
      // 每次切换/刷新都要多等 3 个 RTT；并行后总耗时 ≈ 最慢的那个（status）。
      const pStatus = api("/api/status" + (light ? "?light=1" : ""));
      const pConfig = api("/api/config").catch(() => null);
      const pLogs = api("/api/logs?limit=60" + (state.logFilter ? "&provider=" + state.logFilter : "")).catch(() => null);
      const pStats = api("/api/stats").catch(() => null);

      const st = await pStatus;
      // 已经有更新的一次跑完了 → 这份数据是过期的，不要覆盖它
      if (stale()) return false;
      // P2 N2（BUGS-v2.3）：接口返回业务错误时优先报「服务端出错」。
      // 否则后面 renderStack 之类的地方会先抛 TypeError，被 catch 成
      // 「无法连接 …（确认电脑端已启动、地址正确、口令一致）」—— 用户去查网络、
      // 查地址、查口令，而真因（服务端 500 / 内部异常）一个字都看不到。
      if (st && (st.__http_status >= 400 || (st.error && !st.providers))) {
        setConn(false, "服务端出错：" + (st.error || ("HTTP " + st.__http_status)));
        toast("服务端出错：" + (st.error || ("HTTP " + st.__http_status)), 4000);
        return false;
      }
      state.status = st;
      setConn(true);
      $("#dayLabel").textContent = "今日 " + st.day;
      renderStack();
      ringShown = 0; $("#ringNum").textContent = "0";
      requestAnimationFrame(() => refreshProgress());
      $("#streakNum").textContent = st.streak != null ? st.streak : 0;
      renderWeek();
      renderCalHero();

      state.config = await pConfig;
      if (stale()) return false;
      // 本地模式（安卓）下 /api/config 偶尔会返回 {"error": "..."}（DataStore 读写竞争等）。
      // 这时 state.config.providers 是 undefined → renderProviderEditors() 直接 return →
      // 设置页签到配置整片空白。所以这里重试一次，仍失败就明确提示，不要留白。
      if (!state.config || !state.config.providers) {
        await new Promise((r) => setTimeout(r, 350));
        try { state.config = await api("/api/config"); } catch (_) { state.config = null; }
      }
      if (!state.config || !state.config.providers) {
        const msg = (state.config && state.config.error) || "接口没有返回 providers";
        toast("读取配置失败：" + msg + "（下拉刷新可重试）", 4000);
      }
      if (state.config && state.config.providers) {
        $("#webhook").value = (state.config.notify && state.config.notify.webhook_url) || "";
        $("#notifyKind").value = (state.config.notify && state.config.notify.kind) || "wecom";
        paintSel($("#notifyKind"));   // 自绘下拉的文字要跟配置同步
        const nsec = $("#notifySecret");
        if (nsec) nsec.value = (state.config.notify && state.config.notify.secret) || "";
        renderNotifyHint();
        const srv = state.config.server || {};
        $("#tokenState").textContent = srv.api_token_set ? "已设置" : "未设置";
      }
      // P2 N3（BUGS-v2.3）：refreshCredsAlert() 从 if 块里提出来，无条件调一次。
      // 它内部本来就优先读 /api/status 顶层（那里也带 creds_lost / crypto_ok），
      // 不需要 config —— 而 config 挂掉（DataStore 竞争）恰恰是最可能伴随凭据异常的
      // 场景，放在 if 里就等于「最需要提示的时候不提示」。
      refreshCredsAlert();
      if (!state.wheelHour) initWheels();
      const dLogs = await pLogs;
      cachedLogs = (dLogs && dLogs.logs) || [];
      renderLogs();
      const dStats = await pStats;
      state.stats = dStats || null;
      renderStats();
      renderStatsView();
      renderProviderEditors();
      // 资产卡优先用 /api/status 带回的缓存，避免首屏就打上游接口。
      // 没拿到资产（首装 / 未配置 / 查询失败）也照样调一次 —— renderAssets 内部会画
      // 4 张占位卡，用户 m13271 要求「默认态就存在 4 个卡片」，不能是一片空白。
      renderAssets((st.assets && st.assets.ok !== false) ? st.assets : null);
      // 登录了新 agent（某家 ready 从 false 变 true）→ 立刻刷一次余额。
      // 不 await：refreshBalances 内部会再走一遍 loadAll，await 会递归；
      // 让它自己在后台跑，资产卡刷新完自然出现。
      const gained = readyGained();
      if (gained.length && !state.autoRefreshing) {
        state.autoRefreshing = true;
        refreshBalances({ silent: true }).finally(() => { state.autoRefreshing = false; });
      }
      if (!state.growthLoaded) renderGrowth(null);
      // 成长中心「账号切换」条要显示当前选中的 WorkBuddy 账号。
      // 用户 m05253：必须每次 loadAll 都重拉 —— 原来写成 `if (!state.wbAccounts)`，
      // 而 state.wbAccounts 首次进页面就被填成当时的快照，之后在多账号页新登录的
      // 账号永远不会出现在这条栏里（首页卡片会更新，因为它读 /api/status）。
      // /api/wb_accounts 在 APK 内是纯本地读 SharedPreferences，毫秒级，不必省。
      // 不 await —— 它只是给按钮补个名字，失败时保持上一次的名字即可。
      loadWbAccounts();
      // 豆包任务面板已下线（服务端 710010702 封禁），这里不再拉 /api/doubao_tasks。
      return true;
    } catch (err) {
      if (location.hash.includes("debug")) document.title = "ERR:" + err.message + " | " + String(err.stack || "").slice(0, 400);
      setConn(false, "无法连接 " + baseUrl() + "：" + err.message + "（确认电脑端已启动、地址正确、口令一致）");
      toast("连接失败：" + err.message, 4000);
      return false;
    }
  }

  /* 初始化 */
  $("#apiToken").value = TOKEN;
  $("#serverUrl").value = BASE || "";
  bindCredsAlert();
  // 先把 4 张占位卡画出来（用户 m13271：默认态就要有 4 个卡片）。
  // 放在 loadAll 之前 —— 后端连不上时 loadAll 会直接抛，那时也得有卡片撑着，
  // 否则整块「积分资产」是一片空白，看着像功能坏了。
  renderAssets(null);
  loadAll().then(() => {
    const h = location.hash || "";
    // 没有 hash 时回到上次停留的 tab（登录页返回、WebView 被回收后重载都走这里）
    let saved = "";
    try { saved = localStorage.getItem("signhub_view") || ""; } catch (_) {}
    const want = h.includes("prefs") ? "prefs"
      : h.includes("settings") ? "settings"
      : h.includes("logs") ? "logs"
      : h.includes("stats") ? "stats"
      : (saved === "prefs" || saved === "settings" || saved === "logs" || saved === "stats") ? saved : "home";
    if (want !== "home") switchView(want, window.innerWidth / 2, window.innerHeight / 2);
    else movePill("home", true);        // 首屏胶囊直接落位，不做动画
    scr.metricsDirty = true; cacheHero(); renderHero();
  });
  // 滚动：驱动头图视差 / 压暗 / 标题飞入 + 导航栏毛玻璃
  window.addEventListener("scroll", onScroll, { passive: true });
  // 窗口尺寸变化后重算位置（横竖屏切换等）
  window.addEventListener("resize", () => {
    const act = $$(".tab").find((t) => t.classList.contains("active"));
    if (act) movePill(act.dataset.view, true);
    scr.metricsDirty = true;
    updateScroll();
  });
  window.addEventListener("hashchange", () => {
    const h = location.hash || "";
    if (h.includes("prefs")) switchView("prefs", window.innerWidth / 2, window.innerHeight / 2);
    else if (h.includes("settings")) switchView("settings", window.innerWidth / 2, window.innerHeight / 2);
    else if (h.includes("logs")) switchView("logs", window.innerWidth / 2, window.innerHeight / 2);
    else if (h.includes("stats")) switchView("stats", window.innerWidth / 2, window.innerHeight / 2);
    else if (h === "" || h === "#" || h.includes("noanim")) {
      switchView("home", window.innerWidth / 2, window.innerHeight / 2);
    }
  });
  // 轮询（BUGS-v2.25 的 P3-2）：原来固定 25 秒，后端挂掉时每轮都会重打 4 个接口、
  // 各弹一次「连接失败」，既没用又刷屏。改成失败退避：25s → 50s → 100s → … 封顶 5 分钟，
  // 成功后立刻复位到 25s。用 setTimeout 自调度而不是 setInterval，才能动态改间隔。
  let pollTimer = 0;
  let pollFail = 0;
  function schedulePoll() {
    const gap = Math.min(25000 * Math.pow(2, pollFail), 300000);
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      const ok = await loadAll().catch(() => false);
      pollFail = ok ? 0 : Math.min(pollFail + 1, 5);
      schedulePoll();
    }, gap);
  }
  schedulePoll();

  /* ============================================================
     ⑧ 庆祝彩带：全屏 canvas confetti
     ============================================================ */
  let confettiCtx = null, confettiRaf = 0, confettiBits = [];
  function initConfetti() {
    const cv = $("#confettiCv");
    if (!cv) return;
    confettiCtx = cv.getContext("2d");
    const fit = () => { cv.width = window.innerWidth; cv.height = window.innerHeight; };
    fit();
    window.addEventListener("resize", fit);
  }
  function confettiBurst(big) {
    if (!confettiCtx || NOANIM) return;
    const cv = $("#confettiCv");
    cv.classList.add("on");
    const colors = ["#37e0a6", "#6c8cff", "#9b6cff", "#ffcf5c", "#4bd6ff", "#ff8fb3"];
    const n = big ? 120 : 60;
    for (let i = 0; i < n; i++) {
      confettiBits.push({
        x: cv.width * (0.25 + Math.random() * 0.5),
        y: -14 - Math.random() * 60,
        vx: (Math.random() - 0.5) * 3.2,
        vy: 2 + Math.random() * 3.4,
        rot: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.25,
        w: 5 + Math.random() * 6, h: 8 + Math.random() * 7,
        c: colors[i % colors.length],
        wob: Math.random() * Math.PI * 2,
      });
    }
    cancelAnimationFrame(confettiRaf);
    const step = () => {
      const c = confettiCtx;
      c.clearRect(0, 0, cv.width, cv.height);
      confettiBits = confettiBits.filter((p) => p.y < cv.height + 24 && p.h > 0);
      confettiBits.forEach((p) => {
        p.wob += 0.09;
        p.x += p.vx + Math.sin(p.wob) * 0.7;
        p.y += p.vy;
        p.rot += p.vr;
        p.vy = Math.min(p.vy + 0.02, 5.2);
        c.save();
        c.translate(p.x, p.y);
        c.rotate(p.rot);
        c.fillStyle = p.c;
        c.fillRect(-p.w / 2, -p.h / 2, p.w, p.h * Math.abs(Math.sin(p.wob * 0.8)) + 2);
        c.restore();
      });
      if (confettiBits.length) {
        confettiRaf = requestAnimationFrame(step);
      } else {
        c.clearRect(0, 0, cv.width, cv.height);
        cv.classList.remove("on");
      }
    };
    confettiRaf = requestAnimationFrame(step);
  }

  /* ============================================================
     ⑨ 主题系统：星夜 / 晨雾 / 云朵 / 跟随系统
     ============================================================ */
  const THEMES = [
    { id: "dark",   name: "星夜",   dot: "linear-gradient(135deg,#141a34,#6c8cff)", dark: 1 },
    { id: "light",  name: "晨雾",   dot: "linear-gradient(135deg,#eef3fb,#9db8ff)", dark: 0 },
    { id: "mint",   name: "云朵",   dot: "linear-gradient(135deg,#e9f7f0,#3ec9a0)", dark: 0 },
    /* 跟随系统不是第四个配色，而是一个「选择模式」：它自己不上 data-theme，
       实际生效的永远是 dark 或 light。dot 画成上深下浅，一眼看出是跟随。
       用户 m27113：系统浅色 → 晨雾，系统深色 → 星夜。 */
    { id: "auto",   name: "跟随系统", dot: "linear-gradient(180deg,#141a34 0%,#141a34 50%,#eef3fb 50%,#eef3fb 100%)", dark: 0, sys: 1 },
  ];
  const THEME_KEY = "signhub_theme";
  const THEME_DEFAULT = "dark";
  /* 旧版本可能把主题存成 oled（纯黑，已下线），读到就静默回落到星夜。
     注意 auto 已于 2026-11-24 恢复（用户 m27113），不能再列在这里。 */
  const RETIRED_THEMES = { oled: 1 };
  /**
   * 系统当前是不是深色。取不到任何信号时按深色处理（与默认主题一致）。
   *
   * **判据优先级（用户 m27260 的真机实测逼出来的）**：
   *   1. `SignHubApi.sysDark()` —— 安卓侧读原生 `resources.configuration.uiMode`。
   *      这是唯一可信的：Manifest 声明了 `uiMode`，Activity 自己吃掉 config change，
   *      WebView 内部那份 Configuration 不会跟着更新，matchMedia 会**一直返回旧值**。
   *      症状就是「切深色不变星夜、关深色不变晨雾」—— 两个方向都不变。
   *   2. `matchMedia('(prefers-color-scheme: dark)')` —— 电脑端浏览器与 iOS 走这条。
   */
  function sysDark() {
    try {
      const api = window.SignHubApi;
      if (api && typeof api.sysDark === "function") {
        const v = String(api.sysDark() || "");
        if (v === "1") return true;
        if (v === "0") return false;
      }
    } catch (_) {}
    try {
      return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    } catch (_) {
      return true;
    }
  }
  /** 把「用户选的主题」解析成「真正要应用的配色 id」。auto → dark / light。 */
  function resolveThemeId(id) {
    if (id === "auto") return sysDark() ? "dark" : "light";
    return THEMES.some((t) => t.id === id) ? id : THEME_DEFAULT;
  }
  function applyTheme(id) {
    const root = document.documentElement;
    const pick = (id === "auto" || THEMES.some((t) => t.id === id)) ? id : THEME_DEFAULT;
    const realId = resolveThemeId(pick);
    const t = THEMES.find((x) => x.id === realId) || THEMES[0];
    /* 把已下线的旧值（oled）就地改写成回落值，否则 localStorage 里一直躺着
       一个不存在的 id —— 每次启动都要走一遍回落，且用户看不到「当前选中项」。 */
    try {
      const raw = localStorage.getItem(THEME_KEY);
      if (raw && raw !== pick && !THEMES.some((x) => x.id === raw)) {
        localStorage.setItem(THEME_KEY, pick);
      }
    } catch (_) {}
    root.setAttribute("data-theme", t.id);
    root.classList.toggle("theme-light", !t.dark);
    const metaColor = { dark: "#0a0b10", light: "#eef3fb", mint: "#e9f7f0" };
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", metaColor[t.id] || "#0a0b10");
    // 安卓 WebView：同步系统栏图标颜色（浅色主题配深色图标）
    try {
      if (window.SignHubApi && window.SignHubApi.setSystemBars) window.SignHubApi.setSystemBars(!t.dark);
    } catch (_) {}
    $$(".theme-cell").forEach((c) => c.classList.toggle("on", c.dataset.t === pick));
    const st = $("#themeState");
    if (st) st.textContent = pick === "auto" ? ("跟随系统 · " + t.name) : t.name;
  }
  function setTheme(id) {
    if (RETIRED_THEMES[id]) id = THEME_DEFAULT;
    try { localStorage.setItem(THEME_KEY, id); } catch (_) {}
    applyTheme(id);
  }
  function currentThemeId() {
    const v = localStorage.getItem(THEME_KEY);
    return (v && THEMES.some((t) => t.id === v)) ? v : THEME_DEFAULT;
  }
  /**
   * 系统深浅色变化时自动重算。
   *
   * 为什么需要单独监听：`prefers-color-scheme` 变了不会触发任何 DOM 事件，
   * 只有 matchMedia 的 change 会。只在当前选的是 auto 时才重绘 —— 用户明确选了
   * 「星夜」就不该因为系统变亮而被改成晨雾。
   *
   * 但**安卓上这条监听基本不会触发**：Manifest 给 WebUiActivity 声明了 `uiMode`，
   * Activity 自己吃掉 config change，WebView 的媒体查询值不更新、change 也不来。
   * 真正生效的是原生侧的两处主动推送：`onConfigurationChanged()` 与 `onResume()`
   * （见 WebUiActivity.kt 的 pushTheme()），它们调 `applyTheme(currentThemeId())`。
   * 这里保留 matchMedia 监听只为电脑端浏览器与 iOS。
   */
  function bindSystemTheme() {
    try {
      const mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
      if (!mq) return;
      const onChange = () => { if (currentThemeId() === "auto") applyTheme("auto"); };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    } catch (_) {}
  }
  function themeGridHTML() {
    const cur = currentThemeId();
    return THEMES
      .map((t) => `<button class="theme-cell ${cur === t.id ? "on" : ""}" data-t="${t.id}">
        <span class="theme-dot" style="background:${t.dot}"></span><span class="theme-name">${t.name}</span></button>`).join("");
  }
  function renderThemeGrids() {
    const g1 = $("#themeGrid"), g2 = $("#themeGridSheet");
    if (g1) g1.innerHTML = themeGridHTML();
    if (g2) g2.innerHTML = themeGridHTML();
    $$(".theme-cell").forEach((c) => {
      c.addEventListener("click", () => { setTheme(c.dataset.t); renderThemeGrids(); });
    });
  }
  function openThemeSheet() {
    const mask = $("#themeMask"), sheet = $("#themeSheet");
    if (!mask) return;
    renderThemeGrids();
    mask.hidden = false;
    requestAnimationFrame(() => mask.classList.add("show"));
    if (sheet) sheet.querySelector(".theme-grid");  // 保持节点查询引用（无副作用）
  }
  function closeThemeSheet() {
    const mask = $("#themeMask");
    if (!mask) return;
    mask.classList.remove("show");
    setTimeout(() => { if (!mask.classList.contains("show")) mask.hidden = true; }, 300);
  }
  function bindThemeEntry() {
    renderThemeGrids();
    const entry = $("#themeEntry");
    if (entry) entry.addEventListener("click", openThemeSheet);
    const closeBtn = $("#themeClose");
    if (closeBtn) closeBtn.addEventListener("click", closeThemeSheet);
    const mask = $("#themeMask");
    if (mask) mask.addEventListener("click", (e) => { if (e.target === mask) closeThemeSheet(); });
  }
  /**
   * 把 Android 侧读到的系统栏真实高度写进 CSS 变量 `--inset-t` / `--inset-b`。
   *
   * 为什么需要（用户 m26465，华为鸿蒙）：
   * - 我们开了 edge-to-edge（`setDecorFitsSystemWindows(false)`），内容延伸进状态栏；
   * - 网页靠 `env(safe-area-inset-top/bottom)` 让出系统栏位置；
   * - **鸿蒙的 WebView 对这两个 env() 返回 0**，于是顶栏钻进状态栏里（右上齿轮被盖住点不到）、
   *   底栏被系统手势小横条压住。
   *
   * CSS 那边用的是 `max(var(--inset-t), env(safe-area-inset-top))`，
   * 所以这里写 0 也不会把 Android 原生 WebView 的行为改坏。
   *
   * 电脑端没有这个桥（`SignHubApi` 不存在），直接跳过 —— CSS 变量保持 0px。
   */
  function syncInsets() {
    const bridge = window.SignHubApi;
    if (!bridge || typeof bridge.insets !== "function") return;
    let info = null;
    try {
      info = JSON.parse(bridge.insets() || "{}");
    } catch (_) {
      return;
    }
    if (!info) return;
    const top = Number(info.top) || 0;
    const bottom = Number(info.bottom) || 0;
    if (top > 0) document.documentElement.style.setProperty("--inset-t", top + "px");
    if (bottom > 0) document.documentElement.style.setProperty("--inset-b", bottom + "px");
  }

  /**
   * 反复对齐系统栏占位：首次调用时 Activity 可能还没拿到 insets（鸿蒙上尤其明显，
   * 返回 0 之后要等一次布局才有值），旋转屏幕/系统栏显隐也会变。
   * 只在拿到非 0 值时才写变量，所以多调几次没有副作用。
   */
  function syncInsetsRetry() {
    syncInsets();
    [120, 400, 900, 1800].forEach((ms) => setTimeout(syncInsets, ms));
  }
  window.addEventListener("resize", syncInsets);
  window.addEventListener("orientationchange", syncInsetsRetry);

  function initTheme() { applyTheme(currentThemeId()); bindSystemTheme(); }  /* 齿轮入口（用户 m14411 / m15092）：点它进独立的「设置页」 */
  function bindGear() {
    const g = $("#gearEntry");
    if (!g) return;
    g.addEventListener("click", () => {
      const r = g.getBoundingClientRect();
      switchView("prefs", r.left + r.width / 2, r.top + r.height / 2);
    });
  }
  /* 设置页左上角返回：回到「配置」页 */
  function bindPrefsBack() {
    const b = $("#prefsBack");
    if (!b) return;
    b.addEventListener("click", () => {
      const r = b.getBoundingClientRect();
      switchView("settings", r.left + r.width / 2, r.top + r.height / 2);
    });
  }
  /* 资产宫格的「退出」按钮（用户 m14411：双击卡片 / 点任意 tab 也能退出） */
  function bindAssetMultiExit() {
    const b = $("#assetMultiExit");
    if (b) b.addEventListener("click", exitAssetMulti);
  }

  /* ============================================================
     ⑩ 水滴宠物：喂水 / 清洁 / 饱食度 / 进化
     ============================================================ */
  const PET_KEY = "signhub_pet_v1";
  const PET_STAGES = [
    { min: 0,  name: "水珠", tag: "Lv.1 水珠" },
    { min: 15, name: "小水滴", tag: "Lv.2 小水滴" },
    { min: 40, name: "大水滴", tag: "Lv.3 大水滴" },
  ];
  const PET_MAX = 75;
  function petLoad() {
    try {
      const o = JSON.parse(localStorage.getItem(PET_KEY) || "{}");
      if (typeof o.feed !== "number") o.feed = 50;
      if (typeof o.clean !== "number") o.clean = 100;
      if (typeof o.ts !== "number") o.ts = Date.now();
      if (!o.name) o.name = "水滴";
      return o;
    } catch (_) { return { feed: 50, clean: 100, ts: Date.now(), name: "水滴" }; }
  }
  function petSave(p) { localStorage.setItem(PET_KEY, JSON.stringify(p)); }
  function petDecay(p) {
    const hours = Math.max(0, (Date.now() - p.ts) / 36e5);
    p.feed = clamp(Math.round(p.feed - hours * 6), 0, PET_MAX);   // 每小时饿 6 点
    p.clean = clamp(Math.round(p.clean - hours * 3), 0, 100);     // 每 2 小时脏一点
    p.ts = Date.now();
    return p;
  }
  function petStage(p) {
    let idx = 0;
    PET_STAGES.forEach((s, i) => { if (p.feed >= s.min) idx = i; });
    return idx;
  }
  let petObj = petDecay(petLoad());
  /**
   * 设置页的「使用说明」（用户 m24258：把配置页每个 AGENT 卡里的说明段搬到这里，
   * 配置页只留字段和状态，页面干净不乱）。
   *
   * 内容与配置页强相关：每条都对应一个 `data-key`，点击标题可跳到配置页对应卡片
   * 并自动展开（复用 jumpToProvider）。这样「看说明」和「去填」是连着的，
   * 不会出现「说明搬走了，反而找不到在哪配」。
   */
  const GUIDE = [
    {
      key: "workbuddy",
      title: "WorkBuddy",
      rows: [
        '点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '在弹出来的网页里用你的 WorkBuddy 账号登录，手机号或邮箱都行。',
        '登录成功后网页会自己关掉，凭据自动填好，卡片上出现<span class="sh-ok">「就绪」</span>就算配好了。',
        '还有第二个号：点<span class="sh-blue">蓝色按钮</span>「多账号管理」→ 再点「＋ 添加账号」→ 重复第 2、3 步。',
        '想每天自动签：去「配置 → 每日自动签到」，打开开关、选个时间就行。',
      ],
    },
    {
      key: "traesolo",
      title: "Trae Solo",
      rows: [
        '先去 <b>trae.cn 官网</b>注册账号，并正常登录用一会儿（写几行代码、开一次会话都算）。<b>新号当天注册就拿来签到，很容易被 Trae 记一笔风控</b>。',
        '回到本软件点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '网页里输入手机号，点「获取验证码」，把收到的验证码填进去登录。',
        '登录成功后网页自己关掉，凭据和设备号都会自动填好，卡片上出现<span class="sh-ok">「就绪」</span>就成了。',
        '怎么判断自己被风控：点「签到」看到红字里有 <b>9074</b> 就是（官网和客户端都不会提示，只有这里能看出来）。等 12~24 小时再试，期间别反复点、也别改设备号。',
        '还有第二个号：点<span class="sh-blue">蓝色按钮</span>「多账号管理」→ 再点「＋ 添加账号」→ 重复第 2~4 步。',
      ],
    },
    {
      key: "qoder",
      title: "Qoder CN",
      rows: [
        '点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '网页里点「使用阿里云登录」，输入手机号，收验证码登录。',
        '登录成功后网页自己关掉，凭据自动填好，卡片上出现<span class="sh-ok">「就绪」</span>就成了。',
        'Qoder 每天 <b>10:00 才开一次领取窗口</b>，一次 100 积分。10 点前卡片会写「每日 10:00 后可领」，到点了再点签到即可。',
        '还有第二个号：点<span class="sh-blue">蓝色按钮</span>「多账号管理」→ 再点「＋ 添加账号」→ 重复第 2、3 步。',
      ],
    },
    {
      key: "minimax",
      title: "MiniMax Code",
      rows: [
        '点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '网页里输入手机号，收验证码登录（有账号密码也可以直接登）。',
        '登录成功后网页自己关掉，凭据自动填好，卡片上出现<span class="sh-ok">「就绪」</span>就成了。',
        'MiniMax 是 <b>7 天一轮</b>，每天 400 分左右，连签还有额外奖励。',
        '还有第二个号：点<span class="sh-blue">蓝色按钮</span>「多账号管理」→ 再点「＋ 添加账号」→ 重复第 2、3 步。',
      ],
    },
    {
      key: "huawei",
      title: "华为码道",
      rows: [
        '点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '网页里用华为账号登录，手机号 + 验证码，或者账号密码都行。',
        '登录成功、页面回到码道首页后，软件会自动保存，网页自己关掉，卡片上出现<span class="sh-ok">「就绪」</span>就成了。',
        '华为的登录状态比较短，<b>半小时到一小时</b>就会过期。软件在签到前会自己静默重登一次（约 2~4 秒，不会弹窗）；重登也续不上时才提示「会话已失效」，点「登录主账号」手动重登一次 —— 已经签到的记录不会因此变失败。',
        '还有第二个号：点<span class="sh-blue">蓝色按钮</span>「多账号管理」→ 再点「＋ 添加账号」→ 重复第 2、3 步。',
      ],
    },
    {
      key: "zhipu",
      title: "智谱清言",
      rows: [
        '点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '网页里点右上角「登录」，用手机号 + 验证码登录，或者扫码登录都行。',
        '登录成功后网页自己关掉，凭据自动填好，卡片上出现<span class="sh-ok">「就绪」</span>就成了。',
        '智谱<b>没登录也有一整套 token</b>，所以只认「真登录」：卡片写「游客态」就说明那次是没登录的状态，回去重登一次。',
        '登录一次就够，不用反复登 —— 软件每次签到前会用 refresh_token 自动换一个新的 access_token，而这个 refresh_token 本身不会过期轮换。',
      ],
    },
    {
      key: "qianfan",
      title: "百度千帆",
      rows: [
        '点<span class="sh-ok">绿色按钮</span>「登录主账号」。',
        '网页里用百度账号登录，手机号 + 验证码，或者账号密码都行。',
        '登录成功、页面回到百度智能云控制台后，软件会自动保存，网页自己关掉，卡片上出现<span class="sh-ok">「就绪」</span>就成了。',
        '判据是登录态 Cookie：卡片写「游客态」说明那次没真登录上，回去重登一次。',
        '这是<b>百度搭子（DuMate）</b>的每日登录奖励，一天一次，领的是搭子积分。',
      ],
    },
    {
      key: "",
      title: "通用",
      rows: [
        '先在「配置」页把要用的 AGENT 一个个登录好，卡片上出现<span class="sh-ok">「就绪」</span>才算配好。',
        '配好后回「签到」页，点「一键全部签到」，把今天能领的一次性领完。',
        '想每天自动签：去「配置 → 每日自动签到」，打开开关、选时间，到点手机会自己签。',
        '卡片上的「测试签到」只是试一下通不通，<b>不写记录</b>；「一键全部签到」才会写记录、推进度条。',
        '账号凭据<b>不上传任何服务器</b>，只加密存在这台手机里。卸载或者换手机，要重新登录一次。',
      ],
    },
  ];

  /**
   * 设置页的「使用说明」（用户 m24258 把配置页每个 AGENT 卡里的说明段搬到这里）。
   *
   * 用户 m24951：「将设置页的使用说明隐藏，点击卡片后展开就跟配置页那些卡片一致」
   * → 不再一次性铺开 30 条，改成**与配置页同一套折叠卡片**：
   *   `.prov-card > .prov-head(.pv-ico/.pv-name/.pv-caret) + .prov-body > .pv-body-inner`
   *   直接复用 app.css 里 `.prov-*` 那一整套（圆角、箭头旋转、`height:0↔auto` 过渡），
   *   以及 app.js 的 `foldToggle()`，所以两处的展开动画、手感、配色完全一致。
   *
   * 标题右侧的「去配置 →」保留：看说明和去填字段是连着的，不会出现
   * 「说明搬走了反而找不到在哪配」。点箭头区域是展开/收起，点「去配置」才是跳转，
   * 两者用 stopPropagation 分开。
   */
  function renderGuide() {
    const box = $("#guideList");
    if (!box) return;
    // 记住哪些卡片是展开的：重进设置页不该把用户刚展开的又收回去
    const openKeys = $$(".prov-card.open", box).map((c) => c.dataset.key);
    box.innerHTML = GUIDE.map((g) => {
      const k = g.key || "general";
      const ready = !g.key;
      const badge = ready ? `<span class="pv-badge off">通用</span>` : `<span class="pv-badge ok">${g.rows.length} 条</span>`;
      const go = g.key
        ? `<button class="guide-go" type="button" data-goto="${escapeHtml(g.key)}">去配置 →</button>`
        : "";
      return `<div class="prov-card guide-card" data-key="${escapeHtml(k)}">
        <div class="prov-head">
          <div class="pv-ico${g.key && hasLogo(g.key) ? " has-logo" : ""}">${g.key ? iconHTML(g.key) : "📘"}</div>
          <div class="pv-name"><span class="pv-gt">${escapeHtml(g.title)}</span> ${badge}${go}</div>
          <svg class="pv-caret" width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div class="prov-body">
          <div class="pv-body-inner">
            <ol class="guide-rows">${g.rows.map((r) => `<li>${r}</li>`).join("")}</ol>
          </div>
        </div>
      </div>`;
    }).join("");

    $$(".prov-card", box).forEach((card) => {
      const head = $(".prov-head", card);
      const body = $(".prov-body", card);
      // 「去配置 →」在标题里，点它不能顺带把卡片展开/收起
      const goBtn = $("[data-goto]", card);
      if (goBtn) {
        goBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const k = goBtn.dataset.goto;
          if (k) jumpToProvider(k);
        });
      }
      head.addEventListener("click", () => {
        const willOpen = !card.classList.contains("open");
        card.classList.toggle("open", willOpen);
        foldToggle(body, willOpen);
      });
      if (openKeys.indexOf(card.dataset.key) >= 0) {
        card.classList.add("open");
        if (body) body.style.height = "auto";   // 恢复态不做动画，直接给真实高度
      }
    });

    const st = $("#guideState");
    if (st) st.textContent = (GUIDE.length - 1) + " 个 AGENT";
  }

  /**
   * 「后台可靠性」面板（V2.51）。
   *
   * 为什么需要它：定时签到从 WorkManager 换成 AlarmManager 精确闹钟后，
   * 「准点」这件事第一次真的可能失败 —— 而且是用户完全看不见的那种失败：
   *   · Android 14+ 全新安装默认不给精确闹钟权限 → 闹钟被推迟几小时
   *   · AOSP Doze 之外，国产 ROM 的省电策略会连前台服务一起杀
   *   · 各家「自启动管理」默认关闭第三方应用
   * 面板把这三项的真实状态摊开，并给出一键跳转，用户不用去系统设置里翻。
   *
   * 三项判据都来自原生（Bridge.bgStatus），不是前端猜的。
   */
  function renderBgPanel() {
    const box = $("#bgList");
    if (!box) return;
    const bridge = window.SignHubApi;

    // 电脑端 / 浏览器打开时没有原生桥：整块退化成提示，不显示一堆假的「未授权」
    if (!bridge || typeof bridge.bgStatus !== "function") {
      box.innerHTML = `<p class="muted small" style="margin:0">电脑端没有这些限制：定时由电脑自己的任务计划保证。这一项只在手机上生效。</p>`;
      const st0 = $("#bgState");
      if (st0) st0.textContent = "仅手机端";
      return;
    }

    let info = {};
    try {
      info = JSON.parse(bridge.bgStatus() || "{}");
    } catch (_) {
      info = {};
    }
    const sdk = Number(info.sdk || 0);
    const exactOk = !!info.exact_ok;
    const batteryOk = !!info.battery_ok;
    const nextPick = Number(info.next_pick || 0);

    // 精确闹钟：Android 12+ 才有这个概念；更老的系统天然准点，直接算通过
    const exactNeed = sdk >= 31;
    const exactPass = !exactNeed || exactOk;

    const rows = [
      {
        id: "exact",
        name: "精确定时",
        pass: exactPass,
        on: "已允许 · 息屏也能准点唤醒",
        off: sdk >= 31 ? "未允许 · 定时会被推迟几小时" : "",
        btn: exactPass ? "" : "去允许",
        act: "exact",
      },
      {
        id: "battery",
        name: "电池优化白名单",
        pass: batteryOk,
        on: "已加入 · 后台不会被限制",
        off: "未加入 · 系统可能提前杀掉签到进程",
        btn: batteryOk ? "" : "去设置",
        act: "battery",
      },
      {
        id: "auto",
        name: "自启动 / 后台运行",
        pass: null,   // 系统没有可查询的 API，只能给跳转让用户自己确认
        on: "各家 ROM 默认关闭，建议进去把本应用设为允许",
        off: "",
        btn: "去查看",
        act: "auto",
      },
    ];

    box.innerHTML = rows.map((r) => {
      const dot = r.pass === true ? "ok" : (r.pass === false ? "bad" : "warn");
      const text = r.pass === true ? r.on : (r.pass === false ? r.off : r.on);
      return `<div class="bg-row ${dot}">
        <span class="bg-dot"></span>
        <div class="bg-main">
          <div class="bg-name">${escapeHtml(r.name)}</div>
          <div class="bg-desc">${escapeHtml(text)}</div>
        </div>
        ${r.btn ? `<button class="bg-btn" type="button" data-bg="${r.act}">${escapeHtml(r.btn)}</button>` : ""}
      </div>`;
    }).join("");

    // 下次排期：让用户能验证「它真的排上了」，而不是只有一个开关
    if (nextPick > 0) {
      const d = new Date(nextPick);
      const pad = (n) => String(n).padStart(2, "0");
      const when = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      box.insertAdjacentHTML("beforeend",
        `<p class="muted small" style="margin:10px 0 0">下次自动签到：<b>${when}</b></p>`);
    } else {
      box.insertAdjacentHTML("beforeend",
        `<p class="muted small" style="margin:10px 0 0">还没排期 —— 去「配置 → 每日自动签到」打开开关。</p>`);
    }

    $$("[data-bg]", box).forEach((b) => {
      b.addEventListener("click", () => {
        const act = b.dataset.bg;
        try {
          if (act === "exact") bridge.openExactAlarmSettings();
          else if (act === "battery") bridge.openBatterySettings();
          else if (act === "auto") bridge.openAutoStartSettings();
        } catch (e) {
          toast("打开设置失败：" + e.message, 3000);
        }
      });
    });

    const st = $("#bgState");
    if (st) {
      const passN = rows.filter((r) => r.pass === true).length;
      st.textContent = passN + "/2 已就绪";
    }
  }

  function renderPetCard() {
    const card = $("#petCard");
    if (!card) return;
    const st = petStage(petObj);
    const next = PET_STAGES[st + 1];
    const hunger = Math.round((petObj.feed / PET_MAX) * 100);
    card.className = "pet-card pet-stage-" + (st + 1) + (petObj.feed <= 0 ? " pet-dead" : "");
    card.innerHTML = `
      <div class="pet-visual"><div class="pet-body"></div></div>
      <div class="pet-info">
        <div class="pet-name">${escapeHtml(petObj.name)} <span class="pet-stage-tag">${PET_STAGES[st].tag}</span></div>
        <div class="pet-hunger-row"><span class="muted">饱食度</span>
          <div class="pet-hunger-bar ${hunger < 30 ? "low" : ""}"><i style="width:${hunger}%"></i></div>
          <span class="muted">${hunger}%</span></div>
        <div class="pet-actions">
          <button class="pet-btn" id="petFeed">喂水 +18</button>
          <button class="pet-btn ghost" id="petClean">擦亮</button>
        </div>
        <div class="pet-diet">每次成功签到自动喂水一次${next ? `；喂到 ${next.min} 次进化成「${next.name}」` : "；已是最终形态，继续陪伴吧"}</div>
      </div>`;
    on("#petFeed", "click", () => feedPet(false));
    on("#petClean", "click", () => {
      petObj.clean = 100;
      petSave(petObj);
      toast("已擦亮，闪闪发光 ✨");
      renderPetCard();
    });
    const sum = $("#petSummary");
    if (sum) sum.textContent = petObj.feed <= 0 ? "快饿扁了！" : `${PET_STAGES[st].name} · 饱食 ${hunger}%`;
  }
  function petEvolvePop(fromStage, toStage) {
    const pop = document.createElement("div");
    pop.className = "pet-evolve-pop";
    pop.innerHTML = `<div class="pev-box">
      <div class="pev-emoji">💧</div>
      <div class="pev-title">进化啦！</div>
      <div class="pev-sub">${PET_STAGES[fromStage].name} → ${PET_STAGES[toStage].name}</div>
    </div>`;
    document.body.appendChild(pop);
    if (!NOANIM) confettiBurst(false);
    setTimeout(() => pop.remove(), 1900);
  }
  function feedPet(auto) {
    petObj.feed = clamp(petObj.feed + 18, 0, PET_MAX);
    petObj.clean = clamp(petObj.clean + 10, 0, 100);
    const from = petStage(petObj);
    petSave(petObj);
    renderPetCard();
    if (!auto) toast("喂了一口水，水滴开心地晃了晃 💧");
    const now = petStage(petObj);
    if (now > from) petEvolvePop(from, now);
    updatePetFloat();
  }
  function updatePetFloat() {
    const el = $("#petFloatBtn");
    if (!el) return;
    const showOn = localStorage.getItem("signhub_pet_float") !== "0";
    const hungry = petObj.feed <= 12;
    el.hidden = !(showOn && petObj.feed > 0);
    el.innerHTML = "💧" + (hungry ? `<span class="pf-count">!</span>` : "");
  }
  function initPet() {
    renderPetCard();
    const float = $("#petFloatBtn");
    if (float) {
      float.addEventListener("click", () => {
        switchView("settings", window.innerWidth / 2, window.innerHeight / 2);
        setTimeout(() => { const c = $("#petCard"); if (c) c.scrollIntoView({ behavior: NOANIM ? "auto" : "smooth", block: "center" }); }, 420);
      });
    }
    const sw = $("#petFloat");
    if (sw) {
      sw.checked = localStorage.getItem("signhub_pet_float") !== "0";
      sw.addEventListener("change", () => {
        localStorage.setItem("signhub_pet_float", sw.checked ? "1" : "0");
        updatePetFloat();
      });
    }
    updatePetFloat();
    setInterval(() => { petDecay(petObj); petSave(petObj); renderPetCard(); updatePetFloat(); }, 5 * 60 * 1000);
  }

  /* ============================================================
     ⑪ 热力图 + 趋势 + 数字卡 + 勋章 + 年度报告（统计页渲染）
     ============================================================ */
  function buildDayMap() {
    const map = {};
    ((state.status && state.status.week) || []).forEach((w) => {
      if (!w || !w.day) return;
      if (!map[w.day]) map[w.day] = {};
      (w.statuses && typeof w.statuses === "object") && Object.keys(w.statuses).forEach((k) => {
        if (w.statuses[k] === "success" || w.statuses[k] === "already") map[w.day][k] = 1;
      });
    });
    (cachedLogs || []).forEach((l) => {
      if (!l.day || (l.status !== "success" && l.status !== "already")) return;
      if (!map[l.day]) map[l.day] = {};
      map[l.day][l.provider] = 1;
    });
    return map;
  }
  function renderStatsView() {
    renderNumCards();
    renderHeatmap();
    renderTrend();
    renderBadges();
    renderAnnual();
    renderTokenHealth();
    // 热力图优先用官网连登数据：本地 SQLite 只记本机签过的日子，
    // 重装/换机后是空的（会出现「连登 23 天但热力图全灰」）。异步补，不阻塞转场。
    if (!state.heat && !state.heatTried) {
      loadHeat().then((h) => {
        if (h && h.ok) { renderNumCards(); renderHeatmap(); renderTrend(); }
      });
    }
  }
  /** 拉官网连登热力图（失败不阻断，统计页照常用本地数据；失败只试一次，避免每次切页都打上游） */
  async function loadHeat() {
    state.heatTried = true;
    try {
      const d = await api("/api/heat");
      state.heat = d || null;
    } catch (_) {
      state.heat = null;
    }
    return state.heat;
  }
  function renderNumCards() {
    const box = $("#numCards");
    if (!box) return;
    const tot = (state.stats && state.stats.totals) || {};
    const streak = state.status && state.status.streak != null ? state.status.streak : 0;
    const rate = tot.total ? Math.round(((tot.success || 0) / tot.total) * 100) : 0;
    // 官网连登天数：本地库只记本机签过的日子，重装后「累计成功 4 / 成功率 100%」
    // 会与「连续 23 天」自相矛盾。有官数据时第二张卡改用它，口径才一致。
    const remote = state.heat && state.heat.ok && Array.isArray(state.heat.cells) ? state.heat.cells : null;
    let dayCount = 0;
    if (remote) remote.forEach((c) => { if (c && (Number(c.score) || 0) > 0) dayCount++; });
    const card2 = remote
      ? { n: dayCount, l: "连登天数", i: "✅" }
      : { n: tot.success || 0, l: "累计成功", i: "✅" };
    const cards = [
      { n: streak, l: "连续天数", i: "🔥" },
      card2,
      { n: tot.fail || 0, l: "失败次数", i: "❌" },
      { n: rate + "%", l: "成功率", i: "📈" },
    ];
    box.innerHTML = cards.map((c) => `<div class="num-card">
      <span class="nc-ico">${c.i}</span><span class="nc-num">${c.n}</span><span class="nc-label">${c.l}</span></div>`).join("");
  }
  function renderHeatmap() {
    const el = $("#heatmap");
    if (!el) return;
    const dayMap = buildDayMap();
    // 官网连登数据（有则优先，本地库只作兜底 —— 重装/换机后本地是空的）
    const remote = state.heat && state.heat.ok && Array.isArray(state.heat.cells) ? state.heat.cells : null;
    const remoteMap = {};
    if (remote) remote.forEach((c) => { if (c && c.date) remoteMap[c.date] = Math.max(0, Number(c.score) || 0); });
    const enabled = ((state.status && state.status.providers) || []).map((p) => p.key);
    const total = Math.max(1, enabled.length);
    const today = new Date();
    const end = new Date(today.getTime()); end.setHours(0, 0, 0, 0);
    const start = new Date(end.getTime() - 181 * 864e5);          // 近半年（26 周）
    start.setDate(start.getDate() - ((start.getDay() + 6) % 7)); // 对齐到周一
    const days = Math.round((end - start) / 864e5) + 1;
    const cols = Math.ceil(days / 7);
    const todayIso = fmtYMD(end);
    let html = "", done = 0;
    // 分档：0 / 1 / 2 / 3 及以上（原来 0/1/3/4 跳级，中间的 lv2 永远用不到 —— 这就是「看不懂」的一部分）
    const lvOf = (n) => (n <= 0 ? 0 : n >= total ? 4 : Math.min(3, n + 1));
    for (let c = 0; c < cols; c++) {
      html += `<div class="hm-col">`;
      for (let r = 0; r < 7; r++) {
        const idx = c * 7 + r;
        if (idx >= days) { html += `<i class="hm-cell fut"></i>`; continue; }
        const dt = new Date(start.getTime() + idx * 864e5);
        const iso = fmtYMD(dt);
        const future = dt > end;
        // 本地记录按「几个服务签过」算；官网热力图按 score 算，两者取大
        const local = future ? 0 : (enabled.filter((k) => dayMap[iso] && dayMap[iso][k]).length);
        const rScore = remoteMap[iso] || 0;
        const n = Math.max(local, rScore > 0 ? 1 : 0);
        if (!future) done += n;
        const lv = lvOf(n);
        const isToday = iso === todayIso;
        const names = future ? "" : enabled.filter((k) => dayMap[iso] && dayMap[iso][k])
          .map((k) => (LABELS[k] || k)).join("、");
        const tip = future ? iso : `${iso} · ${n}/${total} 个服务${names ? "（" + names + "）" : ""}`;
        html += `<i class="hm-cell lv${future ? "" : lv}${future ? " fut" : ""}${isToday ? " today" : ""}"` +
                ` data-day="${future ? "" : iso}" title="${escapeHtml(tip)}"></i>`;
      }
      html += `</div>`;
    }
    el.innerHTML = html;
    const sum = $("#hmSummary");
    if (sum) {
      const src = remote ? "官网连登 + 本地记录" : "本地记录";
      sum.textContent = `近 26 周 · 共完成 ${done} 次（${src}）`;
    }
    const lg = $("#hmLegendTxt");
    if (lg) {
      lg.textContent = total >= 4 ? "0 / 1 / 2 / 3 / 全部" : `0 / 1 / 2 / 全部（共 ${total} 家）`;
    }
    // 点格子看当天详情（手机没有 hover，title 提示等于看不到）
    if (el.dataset.detailBound !== "1") {
      el.dataset.detailBound = "1";
      el.addEventListener("click", (e) => {
        const cell = e.target.closest ? e.target.closest(".hm-cell[data-day]") : null;
        if (!cell || !cell.dataset.day) return;
        showHeatDetail(cell.dataset.day);
        $$(".hm-cell.sel", el).forEach((c) => c.classList.remove("sel"));
        cell.classList.add("sel");
      });
    }
  }

  /** 热力图单日详情：这一天每家签没签（用户 m14411：原来只有颜色，看不懂） */
  function showHeatDetail(iso) {
    const box = $("#hmDetail");
    if (!box || !iso) return;
    const dayMap = buildDayMap();
    const rec = dayMap[iso] || {};
    const provs = (state.status && state.status.providers) || [];
    const rows = provs.map((p) => {
      const st = rec[p.key];
      const ok = st === "success" || st === "already";
      const txt = ok ? "已签到" : (st === "fail" ? "签到失败" : (st ? "未签到" : "无记录"));
      return `<div class="hd-row"><span>${escapeHtml(p.label || p.key)}</span>` +
             `<b class="${ok ? "hd-ok" : (st === "fail" ? "hd-no" : "")}">${txt}</b></div>`;
    }).join("");
    const d = new Date(iso + "T00:00:00");
    const wk = "日一二三四五六"[d.getDay()];
    box.innerHTML = `<div class="hd-day">${iso} · 周${wk}</div>` + (rows || '<div class="hd-row">暂无服务</div>');
    box.hidden = false;
  }
  function renderTrend() {
    const svg = $("#trendSvg");
    if (!svg) return;
    const dayMap = buildDayMap();
    const enabled = ((state.status && state.status.providers) || []).map((p) => p.key);
    const remote = state.heat && state.heat.ok && Array.isArray(state.heat.cells) ? state.heat.cells : null;
    const remoteMap = {};
    if (remote) remote.forEach((c) => { if (c && c.date) remoteMap[c.date] = Math.max(0, Number(c.score) || 0); });
    const end = new Date(); end.setHours(0, 0, 0, 0);
    const pts = [];
    for (let i = 29; i >= 0; i--) {
      const dt = new Date(end.getTime() - i * 864e5);
      const iso = fmtYMD(dt);
      const local = enabled.filter((k) => dayMap[iso] && dayMap[iso][k]).length;
      // 官网连登只有「这天签过没」的 0/1 粒度，取大即可：
      // 重装/换机后本地库是空的，只看本地会画出一条几乎为零的直线。
      const n = Math.max(local, (remoteMap[iso] || 0) > 0 ? 1 : 0);
      pts.push({ d: dt, n: Math.min(n, 3) });
    }
    const W = 300, H = 100, max = 3;
    const px = (i) => 6 + (i / (pts.length - 1)) * (W - 12);
    const py = (n) => H - 8 - (n / max) * (H - 20);
    let path = "";
    pts.forEach((p, i) => { path += (i ? " L" : "M") + px(i).toFixed(1) + "," + py(p.n).toFixed(1); });
    const defs = `<defs><linearGradient id="tgrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#6c8cff" stop-opacity=".35"/>
      <stop offset="100%" stop-color="#6c8cff" stop-opacity="0"/></linearGradient></defs>`;
    const area = path + ` L${px(pts.length - 1).toFixed(1)},${H} L${px(0).toFixed(1)},${H} Z`;
    const dots = pts.map((p, i) => p.n > 0 && p.n < max ? `<circle class="trend-dot" cx="${px(i).toFixed(1)}" cy="${py(p.n).toFixed(1)}" r="2.6"/>` : "").join("");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = defs + `<path class="trend-area" d="${area}"/><path class="trend-line" d="${path}"/>` + dots;
    const f = $("#trendFrom"), t = $("#trendTo");
    if (f) f.textContent = fmtYMD(pts[0].d).slice(5);
    if (t) t.textContent = "今天";
  }
  const BADGES = [
    { id: "s7",   emoji: "🌱", name: "初来乍到", desc: "连续签到 7 天",          test: (s) => s.streak >= 7 },
    { id: "s30",  emoji: "🌳", name: "全勤一月", desc: "连续签到 30 天",        test: (s) => s.streak >= 30 },
    { id: "s100", emoji: "👑", name: "百日王者", desc: "连续签到 100 天",       test: (s) => s.streak >= 100 },
    { id: "t50",  emoji: "🎯", name: "神枪手",   desc: "累计成功 50 次",        test: (s) => s.success >= 50 },
    { id: "t200", emoji: "🏆", name: "百战不殆", desc: "累计成功 200 次",       test: (s) => s.success >= 200 },
    { id: "rate", emoji: "🛡️", name: "稳健选手", desc: "成功率 ≥ 95%",          test: (s) => s.total >= 20 && s.success / s.total >= 0.95 },
    { id: "wb",   emoji: "🧩", name: "WB 老友",  desc: "WorkBuddy 累计 30 天",  test: (s) => s.wb >= 30 },
    { id: "tr",   emoji: "🚀", name: "Trae 飞行员", desc: "Trae 累计 30 天",    test: (s) => s.trae >= 30 },
    { id: "hw",   emoji: "☁️", name: "码道同行",   desc: "华为码道累计 30 天", test: (s) => s.hw >= 30 },
    { id: "all",  emoji: "🌈", name: "全家桶",   desc: "两服务同日全勤（近7天内）", test: (s) => s.allOk },
    { id: "night",emoji: "🦉", name: "夜猫子",   desc: "23点后成功签过一次",    test: (s) => s.nightOwl },
    { id: "early",emoji: "🌅", name: "早鸟",     desc: "7点前成功签过一次",     test: (s) => s.earlyBird },
  ];
  function computeBadgeCtx() {
    const tot = (state.stats && state.stats.totals) || {};
    const per = {};
    let nightOwl = false, earlyBird = false;
    // created_at 形如 "2026-02-14 08:30:21" 或 ISO
    (cachedLogs || []).forEach((l) => {
      if (l.status !== "success" && l.status !== "already") return;
      per[l.provider] = (per[l.provider] || 0) + 1;
      const h = Number((l.created_at || "").slice(11, 13)) || Number((l.time || "").slice(11, 13)) || 0;
      if (h >= 23) nightOwl = true;
      if (h > 0 && h < 7) earlyBird = true;
    });
    let allOk = false;
    ((state.status && state.status.week) || []).forEach((w) => {
      if (w && w.ok) allOk = true;
    });
    return { streak: (state.status && state.status.streak) || 0, success: tot.success || 0, total: tot.total || 0,
      wb: per.workbuddy || 0, trae: per.traesolo || 0, hw: per.huawei || 0,
      allOk, nightOwl, earlyBird };
  }
  function renderBadges() {
    const wall = $("#badgeWall");
    if (!wall) return;
    const ctx = computeBadgeCtx();
    const got = BADGES.filter((b) => b.test(ctx)).length;
    wall.innerHTML = BADGES.map((b) => `<button class="badge-cell ${b.test(ctx) ? "on" : ""}" data-b="${b.id}">
      <span class="badge-emoji">${b.emoji}</span><span class="badge-name">${b.name}</span></button>`).join("");
    $$(".badge-cell", wall).forEach((cell) => {
      cell.addEventListener("click", () => {
        const b = BADGES.find((x) => x.id === cell.dataset.b);
        if (b) toast(`${b.emoji} ${b.name}：${b.desc}`, 2600);
      });
    });
    const sum = $("#badgeSummary");
    if (sum) sum.textContent = `${got} / ${BADGES.length} 已点亮`;
  }
  function renderAnnual() {
    const box = $("#annualBody");
    if (!box) return;
    const year = new Date().getFullYear();
    const logs = cachedLogs || [];
    const inYear = logs.filter((l) => (l.day || "").startsWith(String(year)) && (l.status === "success" || l.status === "already"));
    const monthCnt = {};
    inYear.forEach((l) => { const m = l.day.slice(5, 7); monthCnt[m] = (monthCnt[m] || 0) + 1; });
    const best = Object.keys(monthCnt).sort((a, b) => monthCnt[b] - monthCnt[a])[0];
    const daysCnt = new Set(inYear.map((l) => l.day)).size;
    const pts = inYear.reduce((acc, l) => acc + (parseInt(String(l.points).replace(/\D/g, ""), 10) || 0), 0);
    const rate = logs.length ? Math.round((inYear.length / Math.max(1, inYear.length)) * 100) : 0;
    const tot = (state.stats && state.stats.totals) || {};
    box.innerHTML = `<div class="yr-grid">
      <div class="yr-item"><div class="yi-num">${inYear.length}</div><div class="yi-label">${year} 年签到次数</div></div>
      <div class="yr-item"><div class="yi-num">${daysCnt}</div><div class="yi-label">打卡天数</div></div>
      <div class="yr-item"><div class="yi-num">${pts}</div><div class="yi-label">累计积分</div></div>
      <div class="yr-item"><div class="yi-num">${best ? best + "月" : "—"}</div><div class="yi-label">最勤月份</div></div>
    </div>
    <div class="yr-best">${best ? `${year} 年 ${parseInt(best, 10)} 月是你的巅峰时刻，单月签到 ${monthCnt[best]} 次。` : "今年的故事才刚刚开始，去签下第一笔吧！"} 全部成功 ${tot.success || 0} 次 · 连续 ${(state.status && state.status.streak) || 0} 天。</div>`;
    const rng = $("#yrRange"); if (rng) rng.textContent = `${year} 年`;
    const share = $("#yrShare");
    if (share && !share.dataset.bound) {
      share.dataset.bound = "1";
      share.addEventListener("click", () => {
        const txt = `📈 我的签到年度报告（${year}）：签到 ${inYear.length} 次 · 打卡 ${daysCnt} 天 · 累计 ${pts} 积分 · 连续 ${(state.status && state.status.streak) || 0} 天 —— SigninHub 签到中心`;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(txt).then(() => toast("分享文案已复制 📋"), () => toast(txt, 4200));
        } else toast(txt, 4200);
      });
    }
  }

  /* ============================================================
     ⑫ Token 健康页
     ============================================================ */
  function initTokenHealth() {}
  function renderTokenHealth() {
    const box = $("#tokenHealth");
    if (!box) return;
    const s = (state.config && state.config.providers) || {};
    const wb = (s.workbuddy && s.workbuddy.wb) || {};
    const tr = (s.traesolo && s.traesolo.trae) || {};
    const provs = (state.status && state.status.providers) || [];
    const provOf = (k) => provs.find((x) => x.key === k) || {};
    const traeTime = localStorage.getItem("signhub_trae_token_time") || "";

    // 积分行：优先账号持有积分（total_points），回退今日获得（points）
    const ptsHTML = (key) => {
      const p = provOf(key);
      const tp = p.total_points;
      const pts = (p.points || "").trim();
      if (typeof tp === "number" && tp >= 0) {
        return `<div class="th-pts">当前积分 ${tp}${pts ? ` · 今日 ${escapeHtml(pts)}` : ""}</div>`;
      }
      // "100 Credits" / "连签 1 天" 这类带单位的字符串：原样显示，别当成没数据
      if (typeof tp === "string" && tp.trim()) {
        return `<div class="th-pts">当前积分 ${escapeHtml(tp.trim())}${pts ? ` · 今日 ${escapeHtml(pts)}` : ""}</div>`;
      }
      if (pts) return `<div class="th-pts">今日积分 ${escapeHtml(pts)}</div>`;
      // 华为码道：积分接口与签到同域，会话失效时必然读不到数 ——
      // 说「积分稍后刷新」等于让用户一直等一个不会来的数（m21642）。
      // 必须排在 streak_days 之前：华为恒有连签天数，否则永远走不到这一条。
      if (key === "huawei" && p.portal_ok === false) {
        return `<div class="th-pts mute">会话已失效 · 点「登录华为码道」重新采集（不影响签到）</div>`;
      }
      // 没有积分概念的服务（MiniMax 只有连签天数）走这条：别硬说「积分稍后刷新」
      const sd = p.streak_days;
      if (typeof sd === "number" && sd > 0) {
        return `<div class="th-pts">连签 ${sd} 天${pts ? ` · 今日 ${escapeHtml(pts)}` : ""}</div>`;
      }
      // 未配置凭据优先于「签到失败」：没填凭据导致的 fail 不是真的签到失败，
      // 显示成红字「今日签到失败」会让用户以为服务挂了。
      if (p.ready === false) return `<div class="th-pts mute">未配置凭据</div>`;
      const stt = p.status || "idle";
      if (stt === "already" || stt === "success") {
        return `<div class="th-pts mute">已签到，积分稍后刷新 · 点「刷新余额」</div>`;
      }
      if (stt === "fail") return `<div class="th-pts bad">今日签到失败</div>`;
      return `<div class="th-pts mute">今日未签 · 签到后自动读取账号积分</div>`;
    };

    let rows = "";
    // 用户 m14411：未配置的行点一下直接跳到对应配置卡并展开。
    // 行上带 data-key，点击时去找 .prov-card[data-key=…]。
    const rowCls = (ready) => "th-row" + (ready ? "" : " clickable");
    const wbReady = wb.accessToken || provOf("workbuddy").ready === true;
    rows += `<div class="${rowCls(wbReady)}" data-key="workbuddy"><span class="th-ico has-logo">${iconHTML("workbuddy")}</span><div class="th-main">
      <div class="th-name">WorkBuddy <span class="th-badge ${wbReady ? "ok" : "bad"}">${wbReady ? "已配置" : "未配置"}</span></div>
      ${ptsHTML("workbuddy")}
      <div class="th-sub">refreshToken 自动续期 · 长期有效</div></div></div>`;
    let badge = "bad", label = "未配置", sub = "点「登录 Trae 账号」获取 session";
    if (tr.session) {
      // 有 session（约 14 天）就不用担心 8 小时 token 过期，签到前会自动换新
      badge = "ok"; label = "已配置";
      sub = "session 自动续期 · token 到期会自动换新，无需重登";
    } else if (tr.token) {
      badge = "warn"; label = "仅 token";
      sub = "token 约 8 小时过期 · 建议补填 X-Cloudide-Session 才能自动续期";
      if (traeTime) {
        const age = Date.now() - (parseInt(traeTime, 10) || 0);
        const hours = age / 36e5;
        if (hours >= 8) { badge = "bad"; label = "已过期"; sub = `已 ${Math.floor(hours)} 小时 · 请重新登录`; }
        else if (hours >= 6.5) { badge = "warn"; label = "即将过期"; sub = `约 ${Math.max(1, 8 - Math.floor(hours))} 小时内过期 · 建议补填 session`; }
        else sub = `${(8 - hours).toFixed(1)} 小时后过期 · 建议补填 session 才能续期`;
      }
    }
    rows += `<div class="${rowCls(badge === "ok")}" data-key="traesolo"><span class="th-ico has-logo">${iconHTML("traesolo")}</span><div class="th-main">
      <div class="th-name">Trae Solo <span class="th-badge ${badge}">${label}</span></div>
      ${ptsHTML("traesolo")}
      <div class="th-sub">${sub}</div></div></div>`;

    // ---- Qoder CN ----
    const qd = (s.qoder && s.qoder.qoder) || {};
    const qdReady = provOf("qoder").ready === true;
    let qb = "bad", ql = "未配置", qs = "点「网页登录 Qoder」走官方授权，或粘 refreshToken";
    if (qdReady && qd.refreshToken) {
      qb = "ok"; ql = "已配置";
      qs = "refreshToken 自动续期 · 有效期一年，token 过期自动换新";
    } else if (qdReady) {
      qb = "warn"; ql = "仅 token";
      qs = "accessToken 约 30 天过期 · 建议补填 refreshToken 才能自动续期";
    } else if (qd.token || qd.refreshToken) {
      qb = "warn"; ql = "已填写";
      qs = "凭据已填但未通过校验 · 点「测试」看具体原因";
    }
    rows += `<div class="${rowCls(qb === "ok")}" data-key="qoder"><span class="th-ico has-logo">${iconHTML("qoder")}</span><div class="th-main">
      <div class="th-name">Qoder CN <span class="th-badge ${qb}">${ql}</span></div>
      ${ptsHTML("qoder")}
      <div class="th-sub">${qs}</div></div></div>`;

    // ---- MiniMax Code ----
    const mm = (s.minimax && s.minimax.minimax) || {};
    const mmReady = provOf("minimax").ready === true;
    let mb = "bad", ml = "未配置", ms = "点「网页登录 MiniMax」自动读取 _token";
    if (mmReady) {
      mb = "ok"; ml = "已配置";
      ms = "accessToken 长期有效 · 过期后在网页重新登录一次";
    } else if (mm.accessToken) {
      mb = "warn"; ml = "已填写";
      ms = "凭据已填但未通过校验 · 点「测试」看具体原因";
    }
    rows += `<div class="${rowCls(mb === "ok")}" data-key="minimax"><span class="th-ico has-logo">${iconHTML("minimax")}</span><div class="th-main">
      <div class="th-name">MiniMax Code <span class="th-badge ${mb}">${ml}</span></div>
      ${ptsHTML("minimax")}
      <div class="th-sub">${ms}</div></div></div>`;

    // ---- 华为码道 ----
    const hw = (s.huawei && s.huawei.huawei) || {};
    const hwReady = provOf("huawei").ready === true;
    let hb = "bad", hl = "未配置", hs = "点「登录华为码道」在站内登录一次即可自动采集 Cookie";
    if (hwReady) {
      hb = "ok"; hl = "已配置";
      hs = "会话 Cookie 静置 30~60 分钟会失效 · 失效后重新登录一次";
    } else if (hw.cookie) {
      hb = "warn"; hl = "已填写";
      hs = "凭据已填但未通过校验 · 点「测试」看具体原因";
    }
    rows += `<div class="${rowCls(hb === "ok")}" data-key="huawei"><span class="th-ico has-logo">${iconHTML("huawei")}</span><div class="th-main">
      <div class="th-name">华为码道 <span class="th-badge ${hb}">${hl}</span></div>
      ${ptsHTML("huawei")}
      <div class="th-sub">${hs}</div></div></div>`;

    // ---- 智谱清言（V2.76 接入，纯签名）----
    // 用户 m06523：Token 健康也要有它。判据只看 refresh_token 在不在：
    // 游客态也有整套 token，所以真正的「已登录」由后端 ready（is_guest 校验）给出。
    //
    // BUGS-v2.63-288 P2：智谱 / 千帆只接进了手机端 LocalApi，电脑端 Python 后端
    // 没有这两家。以前这里无条件追加两行，连电脑端也会显示成「未配置」，
    // 而那两行带 jumpToProvider 跳转，点进去找的是被 renderProviderEditors
    // 的 knownKeys 过滤掉、根本不存在的配置卡 → 假入口。
    // 判据与 renderProviderEditors 保持一致：配置里有，或 status 里有。
    const hasProv = (k) => !!(s[k]) || provs.some((x) => x && x.key === k);
    if (hasProv("zhipu")) {
    const zp = (s.zhipu && s.zhipu.zhipu) || {};
    const zpReady = provOf("zhipu").ready === true;
    let zb = "bad", zl = "未配置", zs = "点「登录智谱清言」在站内登录一次即可自动采集 Cookie";
    if (zpReady) {
      zb = "ok"; zl = "已配置";
      zs = "refresh_token 不轮换 · 每次签到前自动换一次 access_token";
    } else if (zp.cookie) {
      zb = "warn"; zl = "游客态";
      zs = "Cookie 是游客态（JWT 里 is_guest=true）· 请重新登录一次";
    }
    rows += `<div class="${rowCls(zb === "ok")}" data-key="zhipu"><span class="th-ico has-logo">${iconHTML("zhipu")}</span><div class="th-main">
      <div class="th-name">智谱清言 <span class="th-badge ${zb}">${zl}</span></div>
      ${ptsHTML("zhipu")}
      <div class="th-sub">${zs}</div></div></div>`;
    }

    // ---- 百度千帆（DuMate）----
    // 千帆没有 refresh 通道：Cookie 失效就只能重新登录，所以副标题直说这一点。
    if (hasProv("qianfan")) {
    const qf = (s.qianfan && s.qianfan.qianfan) || {};
    const qfReady = provOf("qianfan").ready === true;
    let fb = "bad", fl = "未配置", fs = "点「登录百度智能云」在站内登录一次即可自动采集 Cookie";
    if (qfReady) {
      fb = "ok"; fl = "已配置";
      fs = "Cookie 直连 · 失效后需重新登录（千帆没有续期接口）";
    } else if (qf.cookie) {
      fb = "warn"; fl = "已失效";
      fs = "Cookie 里没有 BDUSS 或 bce-user-info · 请重新登录一次";
    }
    rows += `<div class="${rowCls(fb === "ok")}" data-key="qianfan"><span class="th-ico has-logo">${iconHTML("qianfan")}</span><div class="th-main">
      <div class="th-name">百度千帆 <span class="th-badge ${fb}">${fl}</span></div>
      ${ptsHTML("qianfan")}
      <div class="th-sub">${fs}</div></div></div>`;
    }

    box.innerHTML = rows;
    // 未配置的行可点：跳到对应签到配置卡并展开（用户 m14411）
    if (box.dataset.jumpBound !== "1") {
      box.dataset.jumpBound = "1";
      box.addEventListener("click", (e) => {
        const row = e.target.closest ? e.target.closest(".th-row.clickable") : null;
        if (!row) return;
        jumpToProvider(row.dataset.key);
      });
    }
    const st = $("#thState");
    if (st) st.textContent = "本地凭据";
  }

  /**
   * 从 Token 健康跳到某家的签到配置卡并展开（用户 m14411）。
   * 配置卡是 renderProviderEditors() 动态生成的，所以不能直接绑事件 ——
   * 每次跳转都重新查一遍 DOM，卡片不存在就先把设置页渲染一次。
   */
  function jumpToProvider(key) {
    if (!key) return;
    // 从「设置 → 使用说明」跳过来时当前在 prefs 页，配置卡在 #viewSettings 里，
    // 不先切页的话 scrollIntoView 会滚到一个 hidden 的容器（页面纹丝不动）。
    const cur = $("#viewSettings");
    if (cur && cur.hidden) {
      const r = $("#gearEntry") ? $("#gearEntry").getBoundingClientRect() : { left: 0, top: 0, width: 0, height: 0 };
      switchView("settings", r.left + r.width / 2, r.top + r.height / 2);
    }
    const findCard = () => document.querySelector(`.prov-card[data-key="${key}"]`);
    let card = findCard();
    if (!card) {
      renderProviderEditors();
      card = findCard();
    }
    if (!card) { toast("没有找到这家的配置卡"); return; }
    if (!card.classList.contains("open")) {
      card.classList.add("open");
      foldToggle(card.querySelector(".prov-body"), true);
    }
    card.scrollIntoView({ behavior: NOANIM ? "auto" : "smooth", block: "center" });
    // 高亮一下，让用户一眼看到跳到了哪张卡
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 1400);
  }

  /* ============================================================
     水波反馈（用户 m14034）
     要求原文：「点在哪波就从哪散开，反馈跟着手指走，不跟着控件走」。
     所以不能用每个按钮自己的 :active 伪类，也不在按钮内部插节点 ——
     那样圆心只能取控件中心，还会被控件的 overflow / 圆角裁掉。
     做法：document 级一次委托，波纹是 body 上的 position:fixed 元素，
     圆心 = 指针坐标，pointermove 时圆心继续跟随手指。
     ============================================================ */
  /* 用户 m14954：水波只给「按键」用 —— 之前把 .svc / .asset-card / .notify-head
     这些整块卡片也算了进去，而 .svc 一张就 380×78、四张叠起来几乎铺满首页，
     于是「随便长按屏幕哪里都有反馈」。现在只保留真正的可点击控件：
     原生 button（含 .onekey/.save-btn/.ghost-btn/.chip/.tab/.ckbtn/.pv-test/
     .pv-save/.sched-edit/.theme-entry/.ts-close/.creds-alert-x/.am-exit 全是 button），
     外加 Android 注入的 .sh-multi-login。卡片本体不再出波。 */
  const RIPPLE_HOST = "button,.sh-multi-login";

  function initRipple() {
    if (NOANIM) return;                    // #noanim 下整站关动画，波纹也不生成
    let cur = null, clip = null, pid = null;

    function drop() {
      if (!clip) return;
      const el = clip, wave = cur;
      clip = null; cur = null;
      if (wave) { wave.classList.remove("on"); wave.classList.add("out"); }
      // 裁切层跟着一起淡出，避免内部圆形先消失、裁切层还挡着一块矩形
      el.classList.add("gone");
      setTimeout(() => el.remove(), 380);
    }

    document.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button > 0) return;   // 只响应主键 / 触摸
      const t = e.target instanceof Element ? e.target : null;
      if (!t) return;
      const host = t.closest(RIPPLE_HOST);
      if (!host || host.disabled) return;
      if (clip) { clip.remove(); clip = null; cur = null; }

      const r = host.getBoundingClientRect();
      const x = e.clientX, y = e.clientY;
      // 用户 m06176：光只填满按键内部，不再外溢。裁切层尺寸/圆角抄控件，
      // 所以半径取「触点到控件四角的最远距离」就够覆盖整块，不用再乘系数。
      const far = Math.max(
        Math.hypot(x - r.left, y - r.top),
        Math.hypot(x - r.right, y - r.top),
        Math.hypot(x - r.left, y - r.bottom),
        Math.hypot(x - r.right, y - r.bottom),
      );
      const size = Math.max(72, Math.round(far * 2));
      const cs = getComputedStyle(host);
      const box = document.createElement("div");
      box.className = "ripple-clip";
      box.style.left = r.left + "px";
      box.style.top = r.top + "px";
      box.style.width = r.width + "px";
      box.style.height = r.height + "px";
      box.style.borderRadius = cs.borderRadius;
      const el = document.createElement("div");
      el.className = "ripple";
      el.style.width = size + "px";
      el.style.height = size + "px";
      // 坐标换算成裁切层内部坐标（减去控件左上角）
      el.style.left = (x - r.left - size / 2) + "px";
      el.style.top = (y - r.top - size / 2) + "px";
      box.appendChild(el);
      document.body.appendChild(box);
      pid = e.pointerId;
      clip = box;
      cur = el;
      void el.offsetWidth;                 // 强制回流，让 scale(.34)→1 的过渡真正跑起来
      el.classList.add("on");
    }, { passive: true });

    document.addEventListener("pointermove", (e) => {
      if (!cur || !clip || e.pointerId !== pid) return;
      const size = parseFloat(cur.style.width) || 96;
      const r = clip.getBoundingClientRect();
      cur.style.left = (e.clientX - r.left - size / 2) + "px";
      cur.style.top = (e.clientY - r.top - size / 2) + "px";
    }, { passive: true });

    const up = (e) => {
      if (!clip || (pid != null && e.pointerId !== pid)) return;
      drop();
      pid = null;
    };
    document.addEventListener("pointerup", up, { passive: true });
    document.addEventListener("pointercancel", up, { passive: true });
  }

  window.__signhub = { switchView, loadAll, refreshBalances, renderProviderEditors, renderLogs, renderStats, renderStatsView, feedPet, setTheme, openThemeSheet, renderStack, stackLayout, toggleStack, toast, openAssetMulti, exitAssetMulti, applyTheme, resolveThemeId, currentThemeId, sysDark };
  initTheme();
  syncInsetsRetry();
  initRipple();
  initSelPicker();
  initPet();
  initConfetti();
  bindThemeEntry();
  initTokenHealth();
  bindGear();
  bindPrefsBack();
  bindAssetMultiExit();
  syncGear((location.hash || "").indexOf("prefs") >= 0 ? "prefs"
    : (location.hash || "").indexOf("settings") >= 0 ? "settings" : "home");

  /* PWA：注册 Service Worker */
  if ("serviceWorker" in navigator && location.protocol.indexOf("http") === 0) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }
})();
