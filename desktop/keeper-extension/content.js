/**
 * keeper 扩展的 content script：把浮球注入页面，数据交给 background → 内核。
 *
 * 为什么跑在隔离世界还能做事
 * ------------------------
 * content script 有独立的 JS 世界，但**共用同一个 DOM**。所以它照样能读页面元素、
 * 改页面样式 —— 只是拿不到页面的 JS 变量。这正好是我们想要的：页面脚本既看不到浮球
 * 内部（closed shadow），也伪造不了发给内核的数据（数据走 chrome.runtime
 * 消息，不经过页面世界）。CDP 注入时代只能把整个脚本塞进主世界，于是页面能用
 * postMessage 伪造「内核返回的结果」—— 换扩展顺手把这个洞堵上了。
 *
 * 与内核的通信只有一条路：
 *     chrome.runtime.sendMessage({type:'keeper:request', payload:{op, url, ...}})
 * 没有任何一个字节经过页面主世界。
 */
(() => {
  if (window.__keeperOverlayLoaded) return;
  window.__keeperOverlayLoaded = true;

  /* 构建标记。改了这个文件就顺手改一下它——
   *
   * 为什么要留这个：扩展点「重新加载」之后，**页面上跑的仍然是旧的 content
   * script**（MV3 会把旧 context 干掉，但浮球照旧画在那里）。于是"我改了怎么没
   * 生效"和"我还没刷新页面"这两件事，从外面看**一模一样**——按钮在、点下去没反应、
   * 不报错。这标记就是用来一眼分清的：hover 浮球看 title 尾部，或打开连接码输入框
   * 看提示行末尾。 */
  const BUILD = "1018e";

  // ---------- 页面身份（cid）----------
  //
  // 内核要按 URL 猜"这是哪个标签页"，可 URL 会重定向、会在 SPA 里变、而
  // Playwright 的 page.url 还有缓存（刚导航完时它身上带的仍是上一个页面的地址）。
  // 于是出现过「打开新页面后第一次抓取抓到的是上一个页面的内容」。
  //
  // 与其让内核猜，不如**让每个文档自己报身份**：进页面就生成一个唯一 id 写在自己
  // 的 DOM 上，请求时带上，内核遍历几个页面比对哪个 id 对得上就选它。比 URL 匹配
  // 可靠得多，也不受页面顺序、可见性、URL 形态的影响。
  //
  // 为什么不用「内核给每页注入 pid」：注入只对**后续**导航生效，用户自己 Ctrl+T
  // 或点链接新开的标签页赶不上，得再补一套"导航时补注入"的同步逻辑。自己生成就
  // 没这个时序问题 —— 谁在页面里，谁就有 id。
  const CID_ATTR = "data-keeper-cid";
  let cid = null;
  const ensureCid = () => {
    if (cid) return cid;
    const de = document.documentElement;
    if (!de) return null;   // 极早期（document_start 时 <html> 还没解析出来）
    let v = de.getAttribute(CID_ATTR);
    if (!v) {
      try {
        v = crypto.randomUUID();
      } catch (e) {
        v = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
      }
      de.setAttribute(CID_ATTR, v);
    }
    cid = v;
    return cid;
  };

  // ---------- 与内核通信 ----------
  //
  // 不用页面里的 window.keeper（那是 CDP 注入时代的入口，content script 在隔离世界
  // 里也够不到它，而且它能被页面伪造）。
  const send = async (payload) => {
    const r = await chrome.runtime.sendMessage({
      type: "keeper:request",
      payload: { cid: ensureCid() || "", url: location.href, ...payload },
    });
    if (!r) throw new Error("内核没响应（扩展可能已被禁用）");
    if (!r.ok) throw new Error(r.error || "内核返回失败");
    return r.result;
  };

  // 品牌色：与 keeper 前端一致
  const BRAND = "#4285f4";

  // ---------- 位置：跨导航 / 跨标签常驻 ----------
  //
  // 为什么不用 sessionStorage（原来那样）：它按 origin 分区，跨站导航就取不到了，
  // 于是球每换一个站点就跳回默认位置。storage.local 是扩展级的，一个浏览器一份、
  // 跨标签也一致 —— 球在浏览器里本来就只该摆在一个地方。
  //
  // 为什么不是「先读完位置再挂载」：chrome.storage 是异步的，而球必须在第一帧就
  // 在（晚一帧用户就能看见「啪」一下，document_idle 时代就是这个毛病）。所以先按
  // 「看不见」挂上，拿到位置再显示：
  //   * 位置没被拖过（默认右上角）→ 读回来还是同一个位置，等于零延迟
  //   * 拖过 → 直接出现在新位置，不会先在右上角闪一下再跳过去
  const POS_KEY = "__keeperBallPos";
  // 本标签已应用的位置：storage 回声要靠它过滤掉，否则拖动会被自己的写入弹回来
  let applied = null;
  const posListeners = [];

  chrome.storage.local.get(POS_KEY).then((d) => {
    const p = d && d[POS_KEY];
    if (p && typeof p.x === "number" && typeof p.y === "number") {
      applied = p;
      posListeners.forEach((f) => f(p));
    } else {
      posListeners.forEach((f) => f(null));   // null = 保持默认位置
    }
  }).catch(() => posListeners.forEach((f) => f(null)));

  // 别的标签拖了球，这边立刻跟上（否则用户会看到「同一个浏览器里几个球摆得不一样」）
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes[POS_KEY]) return;
    const p = changes[POS_KEY].newValue;
    if (!p || (applied && p.x === applied.x && p.y === applied.y)) return;
    applied = p;
    posListeners.forEach((f) => f(p));
  });

  buildUI();

  // 叫醒 service worker 并让它去连内核。
  //
  // 这一步不是可选的礼节：内核是在浏览器起来**之后**才把地址和 token 写进扩展存储的，
  // 而 SW 通常在那之前就启动过、读不到就放弃了。此后如果没人再戳它一下，它永远不会
  // 重连，于是浮球点下去全是「没连上内核」。
  //
  // 静默失败：内核还没启动时（用户刚开浏览器还没进内核）也该正常显示浮球。
  chrome.runtime.sendMessage({ type: "keeper:connect" }).catch(() => {});

  function buildUI() {
  // ---------- 容器 ----------
  const host = document.createElement("div");
  host.id = "__keeper-overlay-host";
  // 用 style 顶死 position/z-index，别让页面祖先的 transform 把它带走。
  // 放右上角：右下角是页面滚动条与「回到顶部」之类控件的地盘，挡路。
  //
  // opacity:0 —— 先「挂上但看不见」，位置从 storage 读回来后再亮（见下面 show）。
  // 为什么不能直接亮：拖过球的用户读到的是左下角那个位置，而球是先按右上角挂的，
  // 于是会先在右上角闪一下再跳过去。宁可晚 1ms 显示，也不要跳。
  host.style.cssText =
    "all:initial;position:fixed;right:18px;top:18px;z-index:2147483647;" +
    "opacity:0;transition:opacity .1s linear;";
  host.setAttribute("data-keeper-ui", "1");
  const root = host.attachShadow({ mode: "closed" });

  root.innerHTML = [
    "<style>",
    ":host{all:initial}",
    ".k-ball{",
    "  width:40px;height:40px;border-radius:50%;cursor:pointer;",
    "  background:" + BRAND + ";",
    "  box-shadow:0 4px 14px rgba(0,0,0,.28);",
    "  display:flex;align-items:center;justify-content:center;",
    "  color:#fff;font:600 13px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;",
    "  user-select:none;transition:transform .12s ease;",
    "}",
    ".k-ball:hover{transform:scale(1.08)}",
    ".k-ball.k-drag{cursor:grabbing;transform:scale(.96)}",
    ".k-panel{",
    // 球在右上角，面板就在它下方展开（原来是 bottom:50px 在球上方）
    "  position:absolute;right:0;top:50px;width:328px;",
    "  max-height:min(62vh,520px);display:none;flex-direction:column;",
    "  background:rgba(17,20,26,.97);color:#e8eaed;",
    "  border:1px solid rgba(255,255,255,.12);border-radius:12px;",
    "  box-shadow:0 12px 40px rgba(0,0,0,.4);overflow:hidden;",
    "  font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;",
    "}",
    ".k-panel.k-open{display:flex}",
    // 连接状态条：常驻在面板**最上面**。为什么必须在最上面而不是折进设置里——它回答的
    // 是"我现在到底能不能干活"，是用户点开面板后第一个想知道的事。放在下面就等于
    // 让人先点一次抓取、失败了再往上翻找原因。
    ".k-link{display:flex;align-items:center;gap:6px;padding:7px 10px;",
    "  border-bottom:1px solid rgba(255,255,255,.08);flex:0 0 auto}",
    ".k-dot{width:7px;height:7px;border-radius:50%;background:#80868b;",
    "  flex:0 0 auto;transition:background .15s ease}",
    ".k-dot.k-on{background:#34a853;box-shadow:0 0 0 2px rgba(52,168,83,.22)}",
    ".k-lurl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;",
    "  white-space:nowrap;color:#9aa0a6;",
    "  font:10.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}",
    ".k-lb{cursor:pointer;border:1px solid rgba(255,255,255,.16);",
    "  background:transparent;color:#9aa0a6;font-size:10.5px;",
    "  padding:2px 7px;border-radius:6px;flex:0 0 auto}",
    ".k-lb:hover{border-color:" + BRAND + ";color:#8ab4f8}",
    // 连接码输入区：默认隐藏，点「去配置」就地展开。为什么不在这个位置直接放一个
    // 永远可见的输入框——绝大多数时候它是连着的，一个空输入框占着面板最显眼的位置
    // 只会让人以为"还没配好"。按需展开，状态条那一行始终是答案。
    ".k-cfg{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.08);flex:0 0 auto}",
    ".k-cfg[hidden]{display:none}",
    ".k-cta{width:100%;box-sizing:border-box;resize:vertical;",
    "  border:1px solid rgba(255,255,255,.16);border-radius:6px;",
    "  background:rgba(0,0,0,.28);color:#e8eaed;padding:6px 7px;",
    "  font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}",
    ".k-cta:focus{outline:none;border-color:" + BRAND + "}",
    ".k-cfgrow{display:flex;gap:6px;margin-top:6px;align-items:center}",
    ".k-save{cursor:pointer;border:0;border-radius:6px;background:" + BRAND + ";",
    "  color:#fff;font-size:11px;padding:5px 10px;flex:0 0 auto}",
    ".k-cfgtip{margin-top:6px;font-size:10.5px;line-height:1.5;color:#9aa0a6}",
    ".k-cfgtip.k-bad{color:#f28b82}",
    ".k-x{cursor:pointer;border:0;background:transparent;color:#6b7280;",
    // 字号和内边距都比「去配置」小一圈：它是次要动作，不该在状态条里跟地址抢视线
    "  font-size:13px;line-height:1;padding:2px 5px;border-radius:5px;flex:0 0 auto}",
    ".k-x:hover{background:rgba(255,255,255,.1);color:#e8eaed}",
    ".k-bar{display:flex;flex-wrap:wrap;align-items:center;gap:5px;",
    "  padding:8px 10px;flex:0 0 auto}",
    // margin-left:auto 把它推到按钮栏右端；内容为空时不占位（flex 项无内容即无宽度），
    // 所以"没有动作就不显示"是天然成立的，不需要额外切 class。
    ".k-title{margin-left:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;",
    "  white-space:nowrap;font-size:11.5px;color:#9aa0a6}",
    ".k-b{",
    "  display:inline-flex;align-items:center;gap:4px;height:27px;padding:0 10px;",
    "  border-radius:7px;border:1px solid rgba(255,255,255,.16);",
    "  background:transparent;color:#e8eaed;font-size:11.5px;cursor:pointer;",
    "}",
    ".k-b:hover:not(:disabled){border-color:#4285f4;color:#8ab4f8}",
    ".k-b:disabled{opacity:.45;cursor:not-allowed}",
    ".k-b.k-primary{background:" + BRAND + ";border-color:transparent;color:#fff}",
    ".k-b.k-primary:hover:not(:disabled){filter:brightness(1.1);color:#fff}",
    ".k-out{flex:1;min-height:80px;overflow:auto;padding:0 10px 10px;",
    "  font-size:11.5px;color:#c8ccd0;white-space:pre-wrap;",
    "  word-break:break-word;user-select:text}",
    ".k-out img{max-width:100%;display:block;border-radius:7px;",
    "  border:1px solid rgba(255,255,255,.12)}",
    ".k-hint{color:#80868b;padding:0 10px 10px}",
    "</style>",
    '<div class="k-panel" id="p">',
    '  <div class="k-link">',
    '    <span class="k-dot" id="ld"></span>',
    '    <span class="k-lurl" id="lu">检测中…</span>',
    '    <button class="k-lb" id="lb" type="button">去配置</button>',
    '    <button class="k-x" id="x" title="收起">&times;</button>',
    "  </div>",
    '  <div class="k-cfg" id="cfg" hidden>',
    '    <textarea class="k-cta" id="cta" rows="2" spellcheck="false"',
    '      placeholder="粘贴 keeper「浏览器」页的连接码"></textarea>',
    '    <div class="k-cfgrow">',
    '      <button class="k-save" id="csave" type="button">保存并连接</button>',
    '    </div>',
    '    <div class="k-cfgtip" id="ctip">整段粘贴即可，不用拆开填。</div>',
    "  </div>",
    '  <div class="k-bar">',
    '    <button class="k-b k-primary" id="bText">抓文字</button>',
    '    <button class="k-b" id="bShot">截图</button>',
    '    <button class="k-b" id="bEval">取标题/正文</button>',
    '    <button class="k-b" id="bCopy" disabled>复制</button>',
    /* 状态文字跟着按钮走，空闲时为空。**不要**再单独开一行放它——
       面板的垂直空间每一像素都在跟按钮抢，而"就绪"这类没有任何信息量的占位
       会让人以为面板还没准备好。只有真的有动作在进行或刚结束时才显示。 */
    '    <span class="k-title" id="t"></span>',
    "  </div>",
    '  <div class="k-out" id="o"><span class="k-hint" id="h">',
    "    点上面的按钮抓这一页；抓到的文字可以直接选中复制。",
    "  </span></div>",
    "</div>",
    '    <div class="k-ball" id="b">K</div>',
  ].join("\n");

  // ---------- 挂载 ----------
  //
  // run_at 是 document_start：此刻 HTML 往往还没解析出 <html>，元素无处可挂。
  // 原来靠 50ms 轮询等，而 50ms 恰好是肉眼能看见的一帧 —— 「啪一下」的另一半原因。
  // 改成 MutationObserver 盯 document：<html> 一被解析出来就立刻挂，零延迟。
  //
  // 不需要等页面样式或内容：host 用 all:initial + position:fixed，不依赖页面任何
  // 样式，也不受页面 CSP 约束（DOM 注入不是脚本注入）。
  const mount = () => {
    const de = document.documentElement;
    if (!de || de.contains(host)) return true;
    try {
      de.appendChild(host);
      return true;
    } catch (e) {
      return false;
    }
  };

  if (!mount()) {
    const mo = new MutationObserver(() => {
      if (mount()) mo.disconnect();
    });
    mo.observe(document, { childList: true, subtree: true });
    // 兜底：极少数文档（xml/svg 根）会走到这里，25ms 一轮，最多 5 秒
    let n = 0;
    const timer = setInterval(() => {
      if (mount() || ++n > 200) {
        clearInterval(timer);
        mo.disconnect();
      }
    }, 25);
  }

  // 挂上了就把 cid 落进 DOM：这样用户点「抓文字」时内核一定能读到它
  // （而不是等到 mount 完才临时生成，那次请求就少了身份信息）。
  ensureCid();


  const $ = (id) => root.getElementById(id);

  /** 把 Chrome 的原始报错翻成能照着做的话。
   *
   * `Extension context invalidated` 是 MV3 的固有现象，不是 bug：扩展一旦被重新
   * 加载或更新，**所有已打开页面里旧的 content script 立刻失效**——它们的 JS 还
   * 在跑，但通往 service worker 的路已经断了，于是发任何消息都抛这个错。
   *
   * 这个坑几乎人人都踩（改完扩展点一下重新加载，然后忘了刷新页面）。用户看到原始
   * 英文完全无从下手，而解决办法只有一个动作，所以直接告诉他那一个动作。
   */
  const explain = (e) => {
    const msg = String((e && e.message) || e || "");
    if (/Extension context invalidated/i.test(msg)) {
      return "扩展刚更新过，这个页面里的浮球已经失效了。"
        + "请按 ⌘⇧R（Windows 是 Ctrl+F5）刷新本页。";
    }
    if (/Receiving end does not exist/i.test(msg)) {
      return "扩展后台没响应，刷新本页试试。";
    }
    return msg;
  };

  /* 把内部状态镜像到 host 的 dataset，让**闭 shadow** 的内容能被外部读到。
   *
   * 为什么要这么绕：shadow root 必须是 closed ——否则页面脚本能读能改浮球内部，
   * 也就伪造得了"内核返回的结果"。但 closed 的代价是从 DevTools 里同样看不见它，
   * 于是出问题时（按钮点了没反应、面板不展开）**无法观察内部状态**，只能猜：
   * 是旧代码没生效？事件没绑上？hidden 真的被移除了？每一种都无法验证。
   *
   * 镜像出去之后，一行控制台命令就能看出卡在哪一步：
   *     document.querySelector('[data-keeper-ui]')?.dataset
   */
  const mark = (k, v) => { try { host.dataset[k] = String(v); } catch (e) { /* 忽略 */ } };
  mark("build", BUILD);
  mark("cfg", "closed");
  const panel = $("p"), ball = $("b"), out = $("o"), title = $("t");
  // hover 浮球看 title 尾部就能确认跑的是不是最新代码
  ball.title = "keeper·" + BUILD;
  const bText = $("bText"), bShot = $("bShot"), bEval = $("bEval"), bCopy = $("bCopy");
  const ldot = $("ld"), lurl = $("lu"), lbtn = $("lb");

  /* ---------- 连接状态条 ----------
   *
   * 显示什么由两个独立维度决定（configured / ok），因为它们回答不同的问题：
   *   - 没填过连接码 → 灰点 + 「未配置内核」+ 按钮「去配置」
   *   - 填了但此刻没连上 → 灰点 + 地址 + 「 · 未连接」+ 按钮「重新连接」
   *   - 连上了 → 绿点 + 地址
   *
   * **两个按钮点下去都会展开输入框**，并且在配置还在时顺手先重试一次连接。
   * 这样做的理由：连不上的绝大多数原因是配置过期（内核换端口、token 轮换）或压根
   * 没填，而不是网络抖动。所以让用户点一次就能粘贴新的连接码，比"点了没反应、
   * 再去找「去配置」那个按钮"少一次试探。反过来也不吃亏——网络真抖了的话，
   * 那次重试已经顺带做完了。
   *
   * 地址刻意只显示 host:port（去掉 http:// 前缀）：面板宽 328px，完整 URL 既
   * 放不下也会被截断成看不出端口的样子，而端口正是用户唯一需要核对的东西。
   */
  const shortUrl = (u) => String(u || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
  const paintLink = (s) => {
    if (!s || !s.configured) {
      ldot.className = "k-dot";
      lurl.textContent = "未配置内核";
      lbtn.textContent = "去配置";
      lbtn.title = "粘贴 keeper「浏览器」页的连接码";
      mark("link", "unconfigured");
      return;
    }
    ldot.className = s.ok ? "k-dot k-on" : "k-dot";
    lurl.textContent = shortUrl(s.url) + (s.ok ? "" : " · 未连接");
    lbtn.textContent = "重新连接";
    lbtn.title = "重试一次连接，并可粘贴新的连接码";
    mark("link", s.ok ? "ok" : "configured-notok");
  };
  /* 本页的浮球是不是已经死了（扩展被更新/重新加载过，MV3 把旧 context 干掉了）。
   *
   * 这是 MV3 最容易让人抓狂的一点：扩展一重新加载，**所有已打开页面里旧的
   * content script 立刻失效**——JS 还在跑、浮球还在画、按钮还能点，但通往
   * service worker 的路已经断了，于是点下去什么都不会发生，而且**不报错**。
   * 用户看到的就是"我明明刚点了重新连接，怎么没反应"。
   *
   * 所以这里主动识别它：连续两次消息失败就基本可以断定是这个（偶尔的失败是 SW
   * 正在被启动/被杀，下一轮 4 秒后自然恢复）。识别出来就把状态条改写成一句
   * 能照着做的指令，并把按钮变成"刷新页面"——点了直接刷新，总比让人对着一排
   * 毫无反应的按钮猜要做什么强。 */
  let deadContext = false;
  let deadStrikes = 0;
  const refreshLink = async () => {
    try {
      paintLink(await chrome.runtime.sendMessage({ type: "keeper:status" }));
      deadStrikes = 0;
    } catch (e) {
      if (++deadStrikes < 2) return;   // 一次失败多半是 SW 正在启动，不打扰
      deadContext = true;
      ldot.className = "k-dot";
      lurl.textContent = "扩展已更新 · 请刷新本页";
      lbtn.textContent = "刷新页面";
      lbtn.title = "刷新本页后浮球才会重新工作（⌘⇧R / Ctrl+F5）";
      mark("link", "dead-context");
      cfgBox.hidden = true;
    }
  };

  /* ---------- 就地填连接码 ----------
   *
   * 解析与存盘都在 service worker 里（keeper:save-code），这里只负责收文本、
   * 传出去、把结果显示出来。刻意不在 content script 里再实现一份解析：格式以后
   * 若变，改一处就够，两处各写一份迟早会漏改一个。
   */
  const cfgBox = $("cfg"), cta = $("cta"), csave = $("csave"), ctip = $("ctip");
  const showCfg = (open) => {
    cfgBox.hidden = !open;
    mark("cfg", open ? "open" : "closed");
    mark("cfgHidden", cfgBox.hidden);
    if (open) {
      ctip.className = "k-cfgtip";
      ctip.textContent = "整段粘贴即可，不用拆开填。· " + BUILD;
      // 展开后就把光标放进输入框：用户接下来唯一要做的事就是粘贴
      try { cta.focus(); } catch (e) { /* 面板还没获得焦点，忽略 */ }
      // 下一帧再记一次"渲染后"的真值：hidden 属性移除了不代表元素可见
      //（父容器 overflow/高度都可能把它裁掉），所以要量实际盒子。
      requestAnimationFrame(() => {
        const r = cfgBox.getBoundingClientRect();
        mark("cfgBox", `${Math.round(r.width)}x${Math.round(r.height)}`);
      });
    }
  };
  const sayCfg = (text, bad) => {
    ctip.className = "k-cfgtip" + (bad ? " k-bad" : "");
    ctip.textContent = text;
  };

  // 面板级探针（捕获阶段）：click 到达面板时先记一笔。
  // 它和 lbtn 上那个处理器相互独立——两个计数一对比，就能区分
  // "点击根本没进来" 与 "处理器跑了但 hidden 没被移除"，不用再猜。
  panel.addEventListener("click", () => {
    mark("panelClicks", (Number(host.dataset.panelClicks) || 0) + 1);
  }, true);

  lbtn.addEventListener("click", async () => {
    mark("clicks", (Number(host.dataset.clicks) || 0) + 1);
    // 上下文已失效时，唯一有用的动作就是刷新本页——点它就直接刷新
    if (deadContext) { location.reload(); return; }

    // **先把输入框摊开，再去干别的。**
    //
    // 顺序很重要，而且是有教训的：这里原本把 showCfg() 放在最后、排在
    // `await keeper:connect` 之后。而那个请求会真正去建 WebSocket，连不上时
    // background 的 ensureConnected 有 4 秒 failTimer（background.js:87）才
    // resolve —— 于是点一下按钮，输入框要**等 4 秒**才出现，看起来就是"点了没反应"。
    // 而这 4 秒里界面没有任何反馈，用户只会以为按钮坏了。
    //
    // 原则：点击的反馈必须是**同步**的。网络操作一律搭车、不挡路。
    showCfg(true);

    // 然后顺带重试一次连接。**不 await** —— 它慢也好失败也好，与用户能不能立刻
    // 粘贴连接码无关。状态条会在这次重连有结果时自己刷新。
    let s = null;
    try { s = await chrome.runtime.sendMessage({ type: "keeper:status" }); } catch (e) {}
    if (s && s.configured) {
      try { chrome.runtime.sendMessage({ type: "keeper:connect" }); } catch (e) {}
      setTimeout(refreshLink, 800);
    }
  });

  csave.addEventListener("click", async () => {
    const code = cta.value.trim();
    if (!code) { sayCfg("先把连接码粘进来。", true); return; }
    csave.textContent = "保存中…";
    try {
      const r = await chrome.runtime.sendMessage({ type: "keeper:save-code", code });
      if (!r) { sayCfg("扩展后台没响应，稍后再试。", true); csave.textContent = "保存并连接"; return; }
      if (!r.ok) { sayCfg(r.error || "没能保存。", true); csave.textContent = "保存并连接"; return; }
      if (r.extMismatch) {
        /* 这条最坑：装成了另一个 id 的扩展，而内核按 Origin 校验会拒它。
           与其让内核静默拒绝（用户只看到"点了没反应"），不如当场说清。 */
        sayCfg("已保存，但扩展 id 对不上内核的期望（" + r.gotExt + " ≠ " + r.wantExt +
               "）。多半是从别的目录加载的扩展。", true);
        csave.textContent = "保存并连接";
        return;
      }
      cta.value = "";
      showCfg(false);
      csave.textContent = "保存并连接";
      // 存盘是瞬时的，连接是异步的 —— 这里立刻收起输入框，连上没连上交给顶部
      // 状态条播报，不让用户对着按钮等一个可能长达 4 秒的结果。
      lurl.textContent = "连接中…";
      refreshLink();
    } catch (e) {
      sayCfg(explain(e), true);
      csave.textContent = "保存并连接";
    }
  });
  // 回车即提交：连接码经常是从聊天工具里复制的，多一次鼠标点击纯属折磨
  cta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); csave.click(); }
  });
  /* 只在面板打开时轮询：SW 空闲 30 秒会被杀，连接随之断开，所以这个状态本来就会
     变；但没打开面板时去问是没有意义的——用户看不见，而每个页面 4 秒一次消息
     会把 SW 一直吊着不睡，白白多占一个进程。 */
  let linkTimer = null;
  const stopLinkPoll = () => { if (linkTimer) { clearInterval(linkTimer); linkTimer = null; } };

  // 抓到的文字放这里，供「复制」用（放闭包里，不挂 window，免得被页面脚本读到）
  let lastText = "";
  const idle = (s) => { title.textContent = s; };

  const setOut = (node, hint) => {
    out.textContent = "";
    out.appendChild(node || document.createTextNode(hint || ""));
  };

  const busy = async (btn, label, fn) => {
    const btns = [bText, bShot, bEval];
    btns.forEach((x) => (x.disabled = true));
    const old = btn.textContent;
    btn.textContent = "…";
    title.textContent = label;
    try {
      await fn();
    } catch (e) {
      idle("失败");
      setOut(null, "⚠ " + explain(e));
    } finally {
      btns.forEach((x) => (x.disabled = false));
      btn.textContent = old;
    }
  };

  // ---------- 抓文字 ----------
  bText.addEventListener("click", () =>
    busy(bText, "正在抓文字…", async () => {
      const r = await send({ op: "text", max_chars: 20000 });
      lastText = r.text || "";
      idle(r.title ? r.title + " · " + r.full_chars + " 字" : r.full_chars + " 字");
      const pre = document.createElement("div");
      pre.textContent = r.text || "（这一页没有可提取的文字）";
      setOut(pre);
      if (r.truncated) {
        const n = document.createElement("div");
        n.className = "k-hint";
        n.textContent = "已截断，共 " + r.full_chars + " 字";
        out.appendChild(n);
      }
      bCopy.disabled = !lastText;
    })
  );

  // ---------- 截图 ----------
  bShot.addEventListener("click", () =>
    busy(bShot, "正在截图…", async () => {
      const r = await send({ op: "shot", full_page: false, quality: 70 });
      const img = document.createElement("img");
      img.src = r.data_url;
      setOut(img);
      idle(r.title || "截图");
    })
  );

  // ---------- 标题 / 正文（evaluate 通道）----------
  bEval.addEventListener("click", () =>
    busy(bEval, "正在读…", async () => {
      const r = await send({ op: "eval" });
      lastText = JSON.stringify(r.value, null, 2);
      const pre = document.createElement("div");
      pre.textContent = lastText;
      setOut(pre);
      bCopy.disabled = false;
      idle("evaluate");
    })
  );

  // ---------- 复制 ----------
  bCopy.addEventListener("click", async () => {
    if (!lastText) return;
    try {
      await navigator.clipboard.writeText(lastText);
      idle("已复制");
    } catch (e) {
      // 非 https / 无权限时 clipboard API 会失败，退回 execCommand
      const ta = document.createElement("textarea");
      ta.value = lastText;
      ta.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      idle(ok ? "已复制" : "复制失败，请手动选中");
    }
  });

  $("x").addEventListener("click", () => {
    panel.classList.remove("k-open");
    // 收起即停：面板看不见了还每 4 秒问一次，等于白白把 service worker 吊着不睡
    stopLinkPoll();
  });

  // ---------- 拖动 ----------
  // 位置存扩展的 chrome.storage.local（理由见上面 POS_KEY 的注释）：跨导航、
  // 跨标签、跨站点都一致。
  const applyPos = (x, y) => {
    // right 必须置 auto：cssText 里给了 right:18px，若 left 与 right 同时生效，
    // fixed 元素会被拉成"视口宽 - left - right"那么宽（球直接变成一条横幅）。
    host.style.right = "auto";
    host.style.left = x + "px";
    host.style.top = y + "px";
  };

  // 位置一确定就亮。默认情况（没拖过）storage 里读回来就是「没记录」，直接亮，
  // 零额外延迟；拖过的话球直接出现在它该在的地方，不会先在右上角闪一下。
  let shown = false;
  const show = () => {
    if (shown) return;
    shown = true;
    host.style.opacity = "1";
  };
  posListeners.push((p) => {
    if (p) applyPos(p.x, p.y);
    show();
  });

  let drag = null;
  const onDown = (e) => {
    const r = ball.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    ball.classList.add("k-drag");
    ball.setPointerCapture && ball.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  const onMove = (e) => {
    if (!drag) return;
    const x = Math.min(Math.max(0, e.clientX - drag.dx), innerWidth - 40);
    const y = Math.min(Math.max(0, e.clientY - drag.dy), innerHeight - 40);
    applyPos(x, y);
  };
  const onUp = () => {
    if (!drag) return;
    drag = null;
    ball.classList.remove("k-drag");
    const r = host.getBoundingClientRect();
    const p = { x: Math.round(r.left), y: Math.round(r.top) };
    applied = p;
    chrome.storage.local.set({ [POS_KEY]: p }).catch(() => {});
  };
  ball.addEventListener("pointerdown", onDown);
  ball.addEventListener("pointermove", onMove);
  ball.addEventListener("pointerup", onUp);
  ball.addEventListener("pointercancel", onUp);

  // 点击球：拖动刚结束时的那一下 pointerup 不该顺带展开面板
  ball.addEventListener("click", () => {
    if (drag) return;
    panel.classList.toggle("k-open");
    // 展开即刷新：连接状态是每次打开都可能变的（SW 可能刚被杀、token 可能
    // 因为内核重启而过期），用面板打开那一刻的探测结果，别让用户对着上一次
    // 打开时的旧状态做判断。
    if (panel.classList.contains("k-open")) {
      refreshLink();
      if (!linkTimer) linkTimer = setInterval(refreshLink, 4000);
    } else {
      stopLinkPoll();
    }
  });

  // 这里**刻意没有** pagehide 时把 host 删掉：
  //   * 正常导航：旧文档整体销毁，球跟着一起没了，不需要手动删；
  //   * 后退键（bfcache）：文档连 DOM 一起从缓存里回来，而 content script 不会
  //     再跑第二次 —— 主动删掉等于球从此再也不出现。
  // 不删反而是「零重建」的那条路：后退回来时球原地还在，一个像素都不用动。
  }   // buildUI
})();
