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

/**
 * 「对这一页提问」的超时。
 *
 * 给到 30 分钟，因为这一轮可以在 keeper 的浏览器页里**看着它跑、必要时手动停掉**
 * ——那意味着一轮可能真的跑很久（长任务、逐步等外部输入）。30 秒是抓取那种秒级操作
 * 的量级，180 秒也仍然偏短：超时一小，用户在浏览器页看着 agent 正常工作，
 * 浮球这边却先报「没回」，两边的说法直接打架。
 *
 * 这条超时是**兜底**，不是设计目标：真正的反馈来自内核立刻回的那帧 accepted
 * （1 秒内），用户不必靠这个计时器判断有没有生效。
 */
const ASK_TIMEOUT_MS = 30 * 60 * 1000;

/** 秒数 → 「30 秒 / 1800 秒（30 分钟）」，只给大值换算分钟。 */
const fmtTimeout = (ms) => {
  const s = Math.round(ms / 1000);
  return s >= 120 ? `${s} 秒（${Math.round(s / 60)} 分钟）` : `${s} 秒`;
};

/* ---- SW 保活 ----
 *
 * 为什么需要：MV3 的 service worker 空闲约 30 秒就被 Chrome 杀掉。「对这一页提问」
 * 这一轮可能要跑很久，SW 一死，pending 里的 Promise 随端口一起消失，浮球立刻显示
 * 「连接断了」——而内核侧那一轮**还在正常跑**，于是两边说法直接打架。
 *
 * 为什么用 alarms 而不是 setInterval：SW 里的定时器不保证被唤醒（进程随时可能被
 * 冻结），只有 alarms 会被 Chrome 事件循环可靠地触发。而 alarms 每次响都是一次
 * 「事件」，恰好也重置了 SW 的空闲计时器——这是它顺带起到的作用。
 *
 * 0.5 分钟是生产环境允许的**最小周期**（Chrome 120+ 从 1 分钟放宽到 30 秒）。
 * 它正好卡在 30 秒空闲线上，属于「刚好够」而不是「稳过」：真遇到 Chrome 提前回收，
 * 兜底是内核那条 agent:latest（浮球重开面板时补取最新结果），不是这个闹钟。
 * 别把它当成保证，只当成把概率压低。
 */
const KEEPALIVE_ALARM = "keeper-keepalive";
// 保活闹钟的触发间隔。必须显著小于 MV3 SW 的空闲回收阈值（约 30 秒），
// 否则 SW 会在闹钟唤醒前就被杀、长任务（如 Agent 多步执行）必断。
// 用一次性闹钟（when）而非 periodInMinutes：Chrome 对重复闹钟的最小周期会取整到
// 约 1 分钟，远超过阈值——原代码的 periodInMinutes:0.5 实际≈60s，等于没有保活。
// 一次性闹钟每次响完由 refreshKeepalive 再续，把空闲窗口稳稳压在 30 秒阈值以内。
const KEEPALIVE_MS = 20000;

function ensureKeepalive() {
  try {
    chrome.alarms.create(KEEPALIVE_ALARM, { when: Date.now() + KEEPALIVE_MS });
  } catch (e) {
    /* 保活失败不该影响请求本身 */
  }
}

function clearKeepalive() {
  try { chrome.alarms.clear(KEEPALIVE_ALARM); } catch (e) { }
}

function refreshKeepalive() {
  if (pending.size) ensureKeepalive();
  else clearKeepalive();
}

chrome.alarms.onAlarm.addListener((a) => {
  if (!a || a.name !== KEEPALIVE_ALARM) return;
  // 没有在等的请求就收摊：空闲时 Keeping alive 纯属白耗唤醒
  if (!pending.size) {
    clearKeepalive();
    return;
  }
  // 顺便校正一次连接：SW 可能在这个闹钟响起之前就已经重启过、socket 早就断了
  ensureConnected().then(() => refreshKeepalive());
});

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
        lastDenial = "";   // 真连上了，之前那次拒绝已经过去，别让它继续影响新提示
        console.log('[keeper] 已连内核', baseUrl);
        resolve(sock);
      };
      sock.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        /* 内核拒绝连接。它**没有 id**（拒绝发生在握手阶段，还没有具体请求），
         * 所以不能走下面按 id 认领 pending 的路——那一帧会被静默丢掉，用户最后只
         * 看到「与内核的连接断了」。这里把原因记下来，onclose 时用它给出可执行的话。 */
        if (msg.event === 'denied') {
          lastDenial = String(msg.reason || "");
          console.warn('[keeper] 内核拒绝连接:', lastDenial);
          return;
        }
        /* 中间态：内核收到了、正在跑。只 accepted 一种——它**不** resolve promise
           （结果还没出来），只作为通知转给发起的那一页的 content script。
           注意不要把"查询类"的应答也放进来：那类消息同样是终点，必须走下面的
           result 分支兑现 promise，否则请求会一直挂到超时。 */
        if (msg.event === 'accepted') {
          const p = pending.get(msg.id);
          if (p && typeof p.onNote === 'function') {
            try { p.onNote(msg); } catch (e) { /* 通知失败不该影响请求本身 */ }
          }
          return;
        }
        if (msg.event === 'result' || msg.event === 'denied') {
          const p = pending.get(msg.id);
          if (p) {
            pending.delete(msg.id);
            clearTimeout(p.timer);
            refreshKeepalive();
            if (msg.error) p.reject(new Error(msg.error));
            else p.resolve(msg.result);
          }
        }
      };
      sock.onclose = () => {
        clearTimeout(failTimer);
        if (ws === sock) ws = null;
        connecting = null;
        // 有拒绝原因就用它：说清"是 token 不对"还是"内核没跑"。
        // 「连接断了」对这两件事说了同一句话，用户只能靠猜——而猜错方向很费时间。
        const why = lastDenial ? denialText(lastDenial) : '与内核的连接断了';
        // 所有在等的请求直接失败，别让它们挂到超时
        pending.forEach((p) => {
          clearTimeout(p.timer);
          p.reject(new Error(why));
        });
        pending.clear();
        clearKeepalive();
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

/* 内核拒绝连接的原因（token / origin）。只在"连上又被拒"的那一瞬间有意义——
 * 连上就清掉，否则一次陈旧的拒绝会污染后面所有的重连提示。
 *
 * 为什么需要它：内核发来的是 ``{event:"denied", reason:"token"}``，**没有 id**。
 * 而下面按 id 认领 pending 的那段对它是空转——这一帧会被原样丢掉，用户最终只看到
 * 「与内核的连接断了」。那句话对两种完全不同的情况说了同一件事：
 *   · 网线断了 / 内核没跑        → 该去启动内核
 *   · 内核在跑、但不认这个扩展   → 该去重新复制连接码
 * 用户按前者去排查会白花半小时。 */
let lastDenial = "";

/** 把内核给的原因翻成一句能照着做的话。 */
function denialText(reason) {
  if (reason === "token") {
    return "连接码不对——内核每次启动端口和 token 都会重新生成。去 keeper「浏览器」页复制新的连接码，在浮球里重填一次";
  }
  if (reason === "origin") {
    return "内核不认这个扩展的 id（多半是扩展目录换了位置加载的）";
  }
  return "内核拒绝了这条连接";
}

/** content script 入口：发一条请求，等结果。 */
async function request(req, opts) {
  const sock = await ensureConnected();
  if (!sock) throw new Error('没连上内核');
  const id = req.id || `r${++seq}`;
  // 默认 30 秒（抓取）。跑 agent 的那条要放宽，由调用方通过 opts.timeoutMs 给。
  const timeoutMs = (opts && opts.timeoutMs) || 30000;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      refreshKeepalive();
      reject(new Error(`内核 ${fmtTimeout(timeoutMs)} 没回`));
    }, timeoutMs);
    // onNote：收中间态（accepted / sessions）用。不 resolve，只是把消息透出去。
    pending.set(id, {
      resolve,
      reject,
      timer,
      onNote: opts && opts.onNote,
    });
    // 有请求在飞就把保活闹钟挂上：这一轮可能跑很久，SW 不能在这期间被回收
    ensureKeepalive();
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
    // 总结场景：让页面侧按块过滤掉小于阈值的碎片（导航/按钮等噪声）
    minNodeChars: op === "text" ? (Number(payload.min_node_chars) || 0) : 0,
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
        let value = (node ? node.innerText : "") || "";
        // 总结场景：按块切分，丢弃字符数小于阈值的碎片，再拼回。0 表示不过滤。
        if (a.minNodeChars > 0) {
          value = value
            .split(/\n+/)
            .map((s) => s.trim())
            .filter((s) => s.length > a.minNodeChars)
            .join("\n\n");
        }
        return Object.assign(head, { value, ok: true });
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
    /* captureVisibleTab 不需要注入也不需要调试权限，代价是只能截**当前可见**的那个
       标签页。网页 Agent 若点了会开新标签的链接，焦点会跑到新标签上，这一拍截到的
       就是错误那一页；而 content script 里 collectMarks 扫的是发起请求的那个 tab
       （锚定页），于是出现「新标签的图 + 原页的编号框」错位。所以截图前先把锚定
       tab 设为活动页，并截它所在窗口，保证截图与 collectMarks 同源。 */
    try {
      // 锚定：截图前把发起请求的那个 tab（即 Agent 起始页）设为活动页，
      // 这样 captureVisibleTab 截到的就是它，避免新标签抢焦点后截错页、与
      // collectMarks 扫的 DOM 不同源。普通情况该 tab 本就是活动页，update 为空操作。
      if (typeof tabId === "number" && tabId >= 0) {
        try { await chrome.tabs.update(tabId, { active: true }); } catch (e) {}
      }
      const fmt = payload.image_format === "png" ? "png" : "jpeg";
      const q = Math.max(10, Math.min(100, Number(payload.quality) || 70));
      // 不传 windowId：captureVisibleTab 截「当前活动页」，上面已把锚定 tab 设为活动。
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

// 快捷键截图：Ctrl+K → 通知当前活动标签页的内容脚本进入框选。
// 与「截图」按钮（整块可见区）并存：这条是新增的「框选」通道。
chrome.commands &&
  chrome.commands.onCommand.addListener((command) => {
    if (command !== "region-screenshot") return;
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs && tabs[0];
      if (tab && tab.id != null) {
        chrome.tabs
          .sendMessage(tab.id, { type: "keeper:region-shot" })
          .catch(() => {});
      }
    });
  });

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
      const isOpen = !!(ws && ws.readyState === WebSocket.OPEN);
      sendResponse({
        ok: isOpen,
        /* 「正在连接」必须来自**真实的握手状态**（connecting 非空 = 手上有个没
           兑现的连接 promise），不能让 content script 自己乐观地假装。
           假装的代价是：内核根本没跑时状态条会一直转，用户对着一个永远在旋转
           的东西等下去，而真相是早就失败了——那比显示「未连接」更糟。
           真实状态下，握手一旦失败 connecting 就被清掉，这里自然回到「未连接」。 */
        connecting: connecting !== null && !isOpen,
        url: baseUrl || cfg.kernelUrl || "",
        configured: !!cfg.kernelUrl,
        /* 最近一次被拒的原因。状态条要能把「地址填了但内核不认」和「地址根本没填」
           区分开——前者的下一步是重新复制连接码，后者是去填。合成一个 ok 就没了，
           而这两种情况都会表现为"连不上"。 */
        denial: lastDenial || "",
        denialText: lastDenial ? denialText(lastDenial) : "",
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

  // 网页 Agent 的「关闭当前页」：关掉当前活动、但**不是 Agent 锚定页**的那个 tab
  // （通常是误点开的新标签页），焦点自动回到锚定页。content script 关不了 tab
  // （window.close 对非脚本打开的页被浏览器拦截），只能由 SW 用 chrome.tabs.remove 做。
  // 锚定页就是发这条消息来的那个 tab（sender.tab.id），绝不关它自己。
  if (msg && msg.type === 'keeper:close-tab') {
    const anchor = sender && sender.tab && sender.tab.id;
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const t = tabs && tabs[0];
      if (t && typeof anchor === 'number' && t.id !== anchor) {
        chrome.tabs.remove(t.id).catch(() => {});
        // 关掉后把锚定页重新设为活动，确保下一拍截图对准它
        try { chrome.tabs.update(anchor, { active: true }); } catch (e) {}
      }
    });
    sendResponse({ ok: true });
    return true;
  }

  if (!msg || msg.type !== 'keeper:request') return false;

  const payload = Object.assign({}, msg.payload || {});
  const tabId = sender && sender.tab && sender.tab.id;

  /* ---- 浮球对话：不取页面数据，原样转发 ----
     抓取那条路会先 attachPageData 把整页 innerText 塞进 payload，而提问这条路
     恰恰**不能**这么做：内核够不到用户 Chrome 那一页，能送过去的只有用户在
     content.js 里亲自读到的那点上下文（URL / 标题 / 选区）。替他把整页塞进去
     既是没必要的几万字 token，也会让「没选中内容 → agent 看不到这页」这件事
     变得看不出来——用户会以为 agent 读过了。 */
  if (payload.cmd === 'agent:ask' || payload.cmd === 'agent:sessions'
      || payload.cmd === 'agent:latest') {
    const ask = payload.cmd === 'agent:ask';
    request(payload, {
      timeoutMs: ask ? ASK_TIMEOUT_MS : 30000,
      /* 内核的中间态（accepted / sessions）转回发起的那一页。它不在 DOM 里的
         地方也不涉及权限，`chrome.tabs.sendMessage` 是发给自己扩展的标签页。 */
      onNote: (note) => {
        if (typeof tabId !== 'number' || tabId < 0) return;
        chrome.tabs.sendMessage(tabId, { type: 'keeper:note', note }).catch(() => {
          /* 页面已经关了/导航走了：请求本身还在内核侧跑，结果回来时同样收不到 */
        });
      },
    })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;
  }

  /* 先就地取数再转发：内核拿到 page_data 就不再需要去浏览器里"找"这一页。
     注意 payload 是**拷贝**——直接改 msg.payload 会在 content script 重用同一个
     对象时留下上一次的数据。 */
  const afterAttach = () => {
    /* 截图（op:"shot"）是纯客户端行为：data_url 已由 attachPageData 用
       captureVisibleTab 抓好、塞进 payload.page_data。内核没有截图能力，再把请求
       转给它只会拿到空/错误响应——而 content script 那边读的是 r.data_url，于是
       整条截图链路静默失败。所以这里直接把 page_data 回传，不走内核。
       text / eval 仍需要内核处理，照旧往下转发。 */
    if (payload.op === "shot" && payload.self_served && payload.page_data && payload.page_data.data_url) {
      sendResponse({ ok: true, result: payload.page_data });
      return;
    }
    request(payload)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
  };
  attachPageData(tabId, payload)
    .catch(() => {})  // 取不到不是错误，交给内核老路
    .then(afterAttach);
  return true;   // 异步响应
});

/** SW 启动时试着连一次：内核那边可能已经在线等了。 */
chrome.runtime.onStartup.addListener(() => { ensureConnected().then(s => { if (!s) scheduleReconnect(); }); });
chrome.runtime.onInstalled.addListener(() => { ensureConnected().then(s => { if (!s) scheduleReconnect(); }); });
ensureConnected().then((sock) => { if (!sock) scheduleReconnect(); });

/* 选中文字的入口原先做成右键菜单（contextMenus），后来改成**选中即浮现小按钮**。
 *
 * 换掉的原因是交互路径：右键要先移动手到菜单、再从一组项里挑一个，而"把这段
 * 加进提问"这个动作本身是唯一的（不需要在几个动作里选）。直接出现在选区边上，
 * 一次点击就完成，比"右键 → 找项 → 点"短得多。
 *
 * 顺带省掉一个权限：contextMenus 从此不再需要。 */

