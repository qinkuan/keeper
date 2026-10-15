/**
 * keeper 扩展的 service worker：唯一持有内核 WebSocket 的地方。
 *
 * 为什么必须有它
 * --------------
 * content script 跑在**隔离世界**里，它自己不能发 WebSocket 去连内核——不是权限
 * 问题，是 content script 根本不该管网络。SW 是扩展里的常驻后台，也是唯一能同时拿到
 * chrome.tabs / chrome.tabCapture 的地方。所有跨进程通信都收敛到这一个 SW：
 *
 *     content script ──chrome.runtime.sendMessage──▶ SW ──WebSocket──▶ 内核
 *            ▲                                              │
 *            └────────chrome.tabs.sendMessage────────────────┘
 *
 * 为什么 WebSocket 不是 chrome.runtime.connectNative
 * ----------------------------------------------
 * native messaging 要在系统里注册一个可执行文件（写 NativeMessagingHosts 目录），
 * 属于装机动作。WS 只要内核在 127.0.0.1 上开个口，配合 token 校验就够用了。
 *
 * token 从哪来：**用户手动给**。地址在每次启动时都重新分配，扩展自己猜不出来，
 * 所以 keeper「浏览器」页给出一段连接码（地址 + token + 扩展 id 打包在一起），
 * 用户粘贴进浮球面板，解析后存进 chrome.storage（见 keeper:save-code）。
 * 曾经这里是"内核用 Playwright 伸手进来写"，但那只对内核自带的 Chromium 成立——
 * 扩展装在用户自己的 Chrome 里时内核根本够不到那个 service worker。
 *
 * 生命周期陷阱：MV3 的 SW 空闲 30 秒就被杀，WS 随之断开。所以每次 content script
 * 发消息时都先 ensureConnected()，断了就重连，而不是指望它一直活着。
 */

const RECONNECT_MS = 1500;

/** @type {WebSocket|null} */
let ws = null;
/** @type {Promise<WebSocket|null>|null} */
let connecting = null;
/** @type {any} */
let retryTimer = null;
/** id → {resolve, reject, timer} */
const pending = new Map();
let seq = 0;

/** 内核基址。开发时可用 chrome.storage 里手动改；正常由内核写进来。 */
let baseUrl = '';

async function loadConfig() {
  const got = await chrome.storage.local.get(['kernelUrl', 'keeperToken']);
  baseUrl = got.kernelUrl || baseUrl;
  return got;
}

function ensureConnected() {
  if (ws && ws.readyState === WebSocket.OPEN) return Promise.resolve(ws);
  if (connecting) return connecting;
  connecting = (async () => {
    const cfg = await loadConfig();
    // 下面两条 return null **必须**先把 connecting 清掉。
    //
    // 踩过的坑：漏清的话，connecting 就永远指向这个「已经 resolve 成 null 的
    // Promise」，于是每次 ensureConnected 都在上面那行 if (connecting) 直接返回它 ——
    // 这个 SW 从此终身连不上内核，浮球点什么都失败，而外面看浏览器在跑、扩展也
    // 加载了，什么异常都没有。
    //
    // 什么时候会命中：内核是在浏览器起来**之后**才把地址/token 写进 storage 的，
    // 而第一个被 document_start 注入的 content script 会在十几毫秒内叫醒我，那
    // 会儿 config 还没到。于是「早醒的那次」把连接能力永久废掉了。
    if (!baseUrl) {
      connecting = null;
      console.warn('[keeper] 还没拿到内核地址，等内核下发');
      return null;
    }
    if (!cfg.keeperToken) {
      connecting = null;
      console.warn('[keeper] 还没拿到 token，等内核下发');
      return null;
    }
    const url = `${baseUrl.replace(/^http/, 'ws')}/agent-bridge` +
      `?token=${encodeURIComponent(cfg.keeperToken)}` +
      `&ext=${chrome.runtime.id}`;
    return new Promise((resolve) => {
      let sock;
      try {
        sock = new WebSocket(url);
      } catch (e) {
        connecting = null;
        resolve(null);
        return;
      }
      const failTimer = setTimeout(() => {
        try { sock.close(); } catch (e) {}
        connecting = null;
        resolve(null);
      }, 4000);
      sock.onopen = () => {
        clearTimeout(failTimer);
        ws = sock;
        connecting = null;
        console.log('[keeper] 已连内核', baseUrl);
        resolve(sock);
      };
      sock.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.event === 'result' || msg.event === 'denied') {
          const p = pending.get(msg.id);
          if (p) {
            pending.delete(msg.id);
            clearTimeout(p.timer);
            if (msg.error) p.reject(new Error(msg.error));
            else p.resolve(msg.result);
          }
        }
      };
      sock.onclose = () => {
        clearTimeout(failTimer);
        if (ws === sock) ws = null;
        connecting = null;
        // 所有在等的请求直接失败，别让它们挂到超时
        pending.forEach((p) => {
          clearTimeout(p.timer);
          p.reject(new Error('与内核的连接断了'));
        });
        pending.clear();
      };
      sock.onerror = () => { /* onclose 会接手 */ };
    });
  })();
  return connecting;
}

/**
 * config 还没到时的退避重连。
 *
 * 为什么必须有：内核是在浏览器起来**之后**才把地址/token 写进来的，而 SW 往往
 * 在那之前就醒了（页面一打开就被 content script 叫醒）。第一次读不到地址就放弃的
 * 话，除非用户再导航一次，这个 SW 会永远连不上 —— 表现是浮球在、点下去全失败。
 *
 * content script 改成 document_start 注入之后这个窗口更容易撞上：页面在 10ms 内就
 * 叫醒了 SW，而内核写 config 要等它自己那次退避（最早 300ms）。
 *
 * 上限约 20 秒：再晚内核也该就绪了；而且 MV3 的 SW 空闲 30 秒本来就会被杀，届时
 * 下次 content script 发消息又会重新唤醒它，这里反复重试没有意义。
 */
function scheduleReconnect() {
  if (retryTimer) return;
  let n = 0;
  const tick = async () => {
    retryTimer = null;
    if (await ensureConnected()) return;
    if (++n >= 8) return;
    retryTimer = setTimeout(tick, 400 + n * 600);
  };
  retryTimer = setTimeout(tick, 300);
}

/** content script 的入口：发一条请求，等结果。 */
async function request(req) {
  const sock = await ensureConnected();
  if (!sock) throw new Error('没连上内核');
  const id = req.id || `r${++seq}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('内核 30s 没回'));
    }, 30000);
    pending.set(id, { resolve, reject, timer });
    sock.send(JSON.stringify({ ...req, id }));
  });
}

// ---- 就地取数（chrome.scripting）----
//
// 这一段是「扩展装在用户自己的 Chrome 里也能抓到页面」的全部关键。
//
// 原来的链路是让**内核**去浏览器里取：内核用 Playwright 找这个页面、再对它求值。
// 可内核的 Playwright 只够得着 keeper 自带的那一个 Chromium（CDP 管道接在那个进程
// 上），用户 Chrome 是另一个进程、没有那根管道，于是永远匹配不上——表现就是
// 「没有可抓取的页面（当前标签页：['about:blank']）」。
//
// 换个方向：扩展**本来就装在这个浏览器里**，它要的文字就在手边。所以由扩展自己取，
// 随请求一起发给内核，内核只处理数据、不再碰浏览器。
//
// 为什么用 chrome.scripting 而不是 chrome.debugger
// ----------------------------------------------
// 两者都能读到页面内容，但 debugger 是「真调试」：attach 的瞬间 Chrome 顶部会弹
// 「XX 扩展正在调试此浏览器」，而且它与用户自己按的 F12 独占同一个调试器——开着
// DevTools 的标签页就读不了。
//
// scripting 是「注入一次函数」：**没有任何提示**，不与 DevTools 抢，权限 manifest
// 里本来就有（scripting + <all_urls>），不需要额外授权。代价是拿不到 MAIN world 的
// JS 变量——但抓文字读的是 DOM，隔离世界照样读得到（DOM 是共享的）。
//
// 取不到时**静默跳过**：请求照旧发给内核，内核对自带浏览器的老路径仍然有效。
// 两条路并存，所以这次改动不会让原来能用的场景失效。

/** 在目标页里跑一次取数函数，把 title/url/value 带回 SW。取不到返回 null。 */
async function readPage(tabId, payload) {
  const op = payload.op || "text";
  const arg = {
    // eval 走 new Function，所以用户可以照旧写 return {...}，不必改成表达式
    script: op === "eval" ? String(payload.script || "") : "",
    selector: op === "text" ? String(payload.selector || "") : "",
  };

  const out = await chrome.scripting.executeScript({
    target: { tabId },
    args: [arg],
    func: (a) => {
      const head = { title: document.title, url: location.href };
      try {
        if (a.script) {
          return Object.assign(head, { value: new Function(a.script)(), ok: true });
        }
        const el = a.selector ? document.querySelector(a.selector) : null;
        const node = el || document.body || document.documentElement;
        return Object.assign(head, {
          value: (node ? node.innerText : "") || "",
          ok: true,
        });
      } catch (e) {
        // 用户脚本写错不该连累「抓文字」——带上错误原文退回内核，好过静默失败
        return Object.assign(head, { value: null, ok: false,
                                     error: String((e && e.message) || e) });
      }
    },
  });

  const got = out && out[0] && out[0].result;
  if (!got || got.ok !== true) return null;
  return { title: got.title || "", url: got.url || payload.url || "", value: got.value };
}

/** 给 payload 附上扩展自取的页面数据。取不到就什么都不做。 */
async function attachPageData(tabId, payload) {
  const op = payload.op || "text";
  if (typeof tabId !== "number" || tabId < 0) return;

  if (op === "text" || op === "eval") {
    try {
      const data = await readPage(tabId, payload);
      if (!data) return;
      payload.self_served = true;
      payload.page_data = data;
    } catch (e) {
      /* chrome://、PDF、Chrome 应用商店、受限页面注入不进去；退回内核老路。 */
    }
    return;
  }

  if (op === "shot") {
    /* captureVisibleTab 不需要注入也不需要调试权限，所以压根没有提示条。代价是
       只能截**当前可见**的那个标签页（也就是用户正看着的），且没有 full_page。
       这里把 full_page 标成 false 而不是假装支持——给一张视口图却声称整页，
       比直接说做不到更糟。 */
    try {
      const fmt = payload.image_format === "png" ? "png" : "jpeg";
      const q = Math.max(10, Math.min(100, Number(payload.quality) || 70));
      const dataUrl = await chrome.tabs.captureVisibleTab(
        typeof payload.windowId === "number" ? { windowId: payload.windowId } : undefined,
        { format: fmt, quality: fmt === "jpeg" ? q : undefined }
      );
      payload.self_served = true;
      payload.page_data = {
        title: "",
        url: payload.url || "",
        data_url: dataUrl,
        full_page: false,
      };
    } catch (e) {
      /* 窗口不可见 / 没有 activeTab 权限时失败，退回内核老路。 */
    }
  }
}

// ---- 与 content script 的消息 ----

// ---- 连接码 ----

/**
 * 解析连接码：`keeper1:` + base64url(JSON)。
 *
 * 为什么要打包成一个码而不是让用户分填四个字段：token 是几十位随机串，手打或
 * 逐个复制最容易在末尾被截掉一个字符，而表现是"保存了但连不上"——用户无从判断
 * 是错在哪。这个格式还带自我描述性前缀，将来换端口方案时可以并存。
 *
 * base64url → base64 的两处替换不能省：urlsafe 变体把 `+` 换成 `-`、`/` 换成
 * `_`，`atob` 不认。补 `=` 是因为内核那边用了 `rstrip("=")`（避免复制时被换行截断）。
 *
 * **这里只认连接码**，用户不需要知道地址和 token 是什么——它们都打包在里面了。
 * 浮球面板的输入框和（曾经存在的）选项页都走这一个入口。
 */
function parseCode(raw) {
  const s = String(raw == null ? "" : raw).trim();
  if (!s.startsWith("keeper1:")) return null;
  let b64 = s.slice("keeper1:".length).replace(/-/g, "+").replace(/_/g, "/");
  while (b64.length % 4) b64 += "=";
  try {
    const obj = JSON.parse(atob(b64));
    if (obj && obj.u && typeof obj.u === "string") {
      return { kernelUrl: obj.u, token: String(obj.t || ""), extId: String(obj.e || "") };
    }
  } catch (e) {
    /* 落到调用方给出的错误提示 */
  }
  return null;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // 用户在浮球面板粘了连接码：解析 → 存 → 立刻用新配置重连。
  //
  // 为什么放在这里而不是让 content script 自己写 storage：解析只有这一份实现，
  // 而存完立刻重连是 SW 的职责（它本来就管连接），省掉一次"存完还得叫醒 SW 重连"
  // 的往返，也免得两个地方各写一份解析、以后改格式只改一处。
  if (msg && msg.type === 'keeper:save-code') {
    const parsed = parseCode(msg.code);
    if (!parsed) {
      sendResponse({ ok: false, error: '这段连接码看不懂，前缀应当是 keeper1: 开头。' });
      return true;
    }
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    chrome.storage.local.set({ kernelUrl: parsed.kernelUrl, keeperToken: parsed.token })
      .then(() => {
        ws = null; connecting = null;   // 让下一次 ensureConnected 用新配置重连
        baseUrl = parsed.kernelUrl;
        // 存完就**立刻**响应，连接放后台跑。
        //
        // 为什么不 await ensureConnected：它内部有 4 秒 failTimer，连不上时要等满
        // 4 秒才 resolve。让用户按着按钮干等 4 秒才知道成没成，是把一个"存个配置"
        // 的动作伪装成了"同步请求"。存配置是瞬时的，连接是异步的——两者的反馈节奏
        // 本来就不该一样。连上没连上由面板顶部那条状态条负责播报。
        sendResponse({
          ok: true,
          saved: true,
          url: parsed.kernelUrl,
          // 连接码里带着"该装出来的扩展 id"。装错目录时 id 会对不上，而内核会
          // 在握手时按 Origin 拒绝——所以这里当场比对，比等内核静默拒绝更好定位。
          extMismatch: !!parsed.extId && parsed.extId !== chrome.runtime.id,
          wantExt: parsed.extId,
          gotExt: chrome.runtime.id,
        });
        ensureConnected();          // 不 await，失败也没关系，会走退避重连
      })
      .catch((e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
    return true;
  }

  // 面板顶部那条连接状态用。**刻意不触发重连**：它的语义是"看一眼现在什么状态"，
  // 而不是"去把连接建立起来"（那是 keeper:connect 的活）。两者混在一起的话，
  // MV3 的 SW 每次被唤醒都会顺手重连，而重连要几百毫秒——状态条就会在断线时
  // 一直闪「连接中…」，反而看不出到底是断了还是配置不对。
  //
  // configured 与 ok 分开报：前者是"用户填过地址"，后者是"此刻 socket 是 OPEN"。
  // 新装扩展时两者都是否，界面上要说的是"去配置"而不是"连接失败"——这两件事的
  // 下一步完全不同，混成一个布尔就没法给出对应指引了。
  if (msg && msg.type === 'keeper:status') {
    loadConfig().then((cfg) => {
      sendResponse({
        ok: !!(ws && ws.readyState === WebSocket.OPEN),
        url: baseUrl || cfg.kernelUrl || "",
        configured: !!cfg.kernelUrl,
      });
    });
    return true;
  }

  // content script 注入时会发这个来"叫醒"我并触发连接。
  // 为什么必须由它来叫：内核是在浏览器起来之后才把 config 写进来的，而 SW 早在
  // 那之前就启动过一次、读不到地址就放弃了；没人再戳它一下就永远连不上。
  if (msg && msg.type === 'keeper:connect') {
    ensureConnected().then((sock) => {
      sendResponse({ ok: !!sock });
      // 没连上基本都是「内核还没下发 config」，退避重试，别就此躺平
      if (!sock) scheduleReconnect();
    });
    return true;
  }

  if (!msg || msg.type !== 'keeper:request') return false;

  /* 先就地取数再转发：内核拿到 page_data 就不再需要去浏览器里"找"这一页。
     注意 payload 是**拷贝**——直接改 msg.payload 会在 content script 重用同一个
     对象时留下上一次的数据。 */
  const payload = Object.assign({}, msg.payload || {});
  const tabId = sender && sender.tab && sender.tab.id;
  attachPageData(tabId, payload)
    .catch(() => {})// 取不到不是错误，交给内核老路
    .then(() => request(payload))
    .then((result) => sendResponse({ ok: true, result }))
    .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
  return true;   // 异步响应
});

/** SW 启动时试着连一次：内核那边可能已经在线等了。 */
chrome.runtime.onStartup.addListener(() => { ensureConnected().then(s => { if (!s) scheduleReconnect(); }); });
chrome.runtime.onInstalled.addListener(() => { ensureConnected().then(s => { if (!s) scheduleReconnect(); }); });
ensureConnected().then((sock) => { if (!sock) scheduleReconnect(); });

