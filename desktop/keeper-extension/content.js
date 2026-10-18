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
  /* 改动这个值 → hover 浮球时 title 尾部就变了，用来确认"跑的到底是不是最新
     代码"。改 content.js 后**不重载扩展 + 不刷新网页**的话，这里永远显示旧值——
     MV3 的 content script 只在页面加载时注入，这是最常见的"我明明改了怎么没变"。 */
  const BUILD = "1101y";

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
    /* 高度上限从 520px 放宽到 660px：多了一个「对这一页提问」区（输入框 + 会话
       选择 + 结果，约 210px）。不放宽的话结果区会被压到只剩几十像素，而它恰恰
       是这一块唯一要看的东西——面板能显示、不等于显示得下。 */
    "  max-height:min(76vh,660px);display:none;flex-direction:column;",
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
    /* 握手中：琥珀色 + 轻微呼吸。为什么不能用 k-on 那种纯色常亮——「正在连」是
       一个**过程**，需要一个仍在发生的信号；静态的绿点会被当成"已经好了"。 */
    ".k-dot.k-wait{background:#fbbc04;box-shadow:0 0 0 2px rgba(251,188,4,.22);",
    "  animation:k-blink 1s ease-in-out infinite}",
    "@keyframes k-blink{0%,100%{opacity:1}50%{opacity:.35}}",
    ".k-lurl{white-space:nowrap;color:#9aa0a6;",
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
    ".k-cancel{cursor:pointer;border:1px solid rgba(255,255,255,.16);",
    "  background:transparent;color:#9aa0a6;font-size:11px;padding:5px 10px;",
    "  border-radius:6px;flex:0 0 auto}",
    ".k-cancel:hover{border-color:" + BRAND + ";color:#8ab4f8}",
    ".k-cfgtip{margin-top:6px;font-size:10.5px;line-height:1.5;color:#9aa0a6}",
    ".k-cfgtip.k-bad{color:#f28b82}",
    ".k-x{cursor:pointer;border:0;background:transparent;color:#6b7280;",
    // 字号和内边距都比「去配置」小一圈：它是次要动作，不该在状态条里跟地址抢视线
    "  font-size:13px;line-height:1;padding:2px 5px;border-radius:5px;flex:0 0 auto}",
    ".k-x:hover{background:rgba(255,255,255,.1);color:#e8eaed}",
    ".k-bar{display:flex;flex-wrap:nowrap;align-items:center;gap:5px;",
    "  padding:8px 10px;flex:0 0 auto;overflow-x:auto;white-space:nowrap;",
    "  scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.25) transparent}",
    ".k-bar::-webkit-scrollbar{height:5px}",
    ".k-bar::-webkit-scrollbar-thumb{background:rgba(255,255,255,.22);border-radius:3px}",
    // margin-left:auto 把它推到按钮栏右端；内容为空时不占位（flex 项无内容即无宽度），
    // 所以"没有动作就不显示"是天然成立的，不需要额外切 class。
    ".k-title{display:block;text-align:right;overflow-x:auto;white-space:nowrap;",
    "  padding:0 10px 6px;font-size:11.5px;color:#9aa0a6;",
    "  scrollbar-width:none;-ms-overflow-style:none}",
    ".k-title:empty{display:none}",
    ".k-title::-webkit-scrollbar{display:none}",
    ".k-b{",
    "  display:inline-flex;align-items:center;gap:4px;height:27px;padding:0 10px;",
    "  flex:0 0 auto;white-space:nowrap;",
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
    // ---- 对这一页提问 ----
    /* 分区标题：两块能力（提问 / 抓取）用途完全不同，混在一排按钮里会让人以为
       「发送」和「抓文字」是同一件事的两步。 */
    ".k-sec{padding:9px 10px 4px;font-size:10.5px;color:#80868b;flex:0 0 auto}",
    ".k-sechead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex:0 0 auto}",
    ".k-sechead>span:first-child{flex:0 0 auto}",
    ".k-sechead .k-title{display:block;flex:1;min-width:0;text-align:right;padding:0 10px;overflow-x:auto}",
    ".k-askrow{display:flex;gap:6px;padding:0 10px;align-items:flex-end;flex:0 0 auto}",
    /* 输入框 + 高亮层的共同外框。定位锚点放在这里，两层都相对它铺满。 */
    ".k-qwrap{position:relative;flex:1;min-width:0}",
    /* 两个必须逐项相同的量：padding / font / 换行 / 最小高度。
       字体和内边距差 1px，高亮块就会整体偏移，看起来像坏了。 */
    ".k-q,.k-qhl{padding:6px 8px;font:12px/1.5 ui-sans-serif,system-ui,sans-serif;",
    "  white-space:pre-wrap;word-break:break-word;min-height:36px;",
    "  box-sizing:border-box;letter-spacing:normal}",
    ".k-q{flex:1;min-width:0;resize:none;background:transparent;",
    "  border:1px solid rgba(255,255,255,.14);border-radius:7px;color:#e8eaed;",
    "  outline:none;position:relative;width:100%;display:block}",
    /* 高亮层：铺在输入框**下面**（z-index 更低），Pointer events 全部放行，
       否则它挡住 textarea，用户就点不进输入框了。
       文字透明——它的作用只是给引用那几段提供底色，真正可见的文字在上面那层。
       末尾补一个换行：textarea 的值以 \n 结尾时，最后一行渲染不出来，
       高亮层会少一行而错位。 */
    ".k-qhl{position:absolute;inset:1px;z-index:0;pointer-events:none;",
    "  color:transparent;overflow:hidden;background:transparent;border:0}",
    /* 浅浅的高亮：只给底色和一点描边，不抢文字的视觉重量。 */
    ".k-qmark{background:rgba(138,180,248,.22);color:transparent;",
    "  border-radius:3px;box-shadow:0 0 0 1px rgba(138,180,248,.28)}",
    ".k-qwrap>.k-q{background:rgba(255,255,255,.07);z-index:1}",
    ".k-q:focus{border-color:" + BRAND + "}",
    ".k-q::placeholder{color:#7b8087}",
    ".k-go{cursor:pointer;border:0;border-radius:7px;background:" + BRAND + ";",
    "  color:#fff;font-size:11.5px;padding:0 13px;height:36px;flex:0 0 auto}",
    ".k-go:disabled{opacity:.45;cursor:not-allowed}",
    ".k-askmeta{display:flex;align-items:center;gap:8px;padding:6px 10px 0;flex:0 0 auto}",
    /* 会话横条：最左一个「新对话」，右边各会话，横向可滚动。
       原来是原生 <select>：它只能纵向弹出列表，看不到"现在选的是哪条"，
       而这条选择决定这轮对话记到哪去——是发消息前就该看见的事实。
       tab 不限宽也不截断：横条本来就能左右滑，把标题截成「浮球…」反而分不清
       两条会话；能滑就不需要省略号。 */
    ".k-tabs{display:flex;gap:5px;margin-top:5px;padding:0 10px 6px;",
    "  overflow-x:auto;flex:0 0 auto;scrollbar-width:none;-ms-overflow-style:none}",
    ".k-tabs::-webkit-scrollbar{display:none}",
    ".k-tab{flex:0 0 auto;overflow:visible;white-space:nowrap;cursor:pointer;",
    "  border-radius:5px;font-size:10.5px;line-height:1.5;padding:2px 7px;",
    "  background:rgba(255,255,255,.05);color:#9aa0a6;",
    "  border:1px solid rgba(255,255,255,.12);outline:none}",
    ".k-tab:hover{border-color:rgba(138,180,248,.5);color:#c7d7f5}",
    ".k-tab.k-on{background:" + BRAND + ";border-color:" + BRAND + ";color:#fff;font-weight:600}",
    /* 加载失败时横条上那个**不可点**的提示。它必须看得见：横条整个由内核数据
       渲染，内核一断就只剩「新对话」，而那看起来跟"加载成功但你没别的会话"
       一模一样——用户没法判断到底是哪一回事。 */
    ".k-tab.k-bad{color:#f28b82;border-color:rgba(242,139,130,.4);",
    "  background:rgba(242,139,130,.08)}",
    ".k-tab.k-bad:hover{border-color:rgba(242,139,130,.75);color:#f28b82}",
    /* 可复制：光标形状就是"这里能点"的唯一提示（浮球里没有按钮标签的位置），
       别省。 */
    ".k-copy{cursor:copy}",
    /* 选区浮标。z-index 取到 int32 上限：页面自己的浮层（吸顶导航、弹窗、视频
       控件）常常是 9999 / 2147483647 量级，比它低就会被盖住——而它一旦被盖住，
       整个功能看起来就是坏的。 */
    ".k-bubble{position:fixed;z-index:2147483647;padding:3px 9px;border:none;",
    "  border-radius:6px;font-size:11px;line-height:1.5;color:#fff;cursor:pointer;",
    "  background:" + BRAND + ";box-shadow:0 2px 8px rgba(0,0,0,.35)}",
    ".k-bubble:hover{filter:brightness(1.08)}",
    ".k-bubble[hidden]{display:none}",
    /* 引用条：整条可溢出截断，但「×」不能被截掉——它是唯一的移除入口，
       一起被截掉的话用户就没法取消了。所以截断只作用在文字那一段。 */
    ".k-quote{display:flex;align-items:center;gap:4px;min-width:0}",
    ".k-quote-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".k-quote-x{flex:0 0 auto;border:none;background:rgba(255,255,255,.1);color:#9aa0a6;",
    "  border-radius:3px;width:14px;height:14px;line-height:1;font-size:11px;",
    "  cursor:pointer;padding:0}",
    ".k-quote-x:hover{background:rgba(242,139,130,.25);color:#f28b82}",
    ".k-sel{margin-left:auto;font-size:10.5px;color:#80868b;",
    "  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}",
    /* 有选中文字时点亮：它是"agent 能不能看见这页"唯一的信号，必须一眼可辨。 */
    ".k-sel.k-has{color:#8ab4f8}",
    ".k-askout{margin:7px 10px 0;padding:8px 9px;border-radius:8px;",
    "  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);",
    "  font-size:11.5px;line-height:1.65;color:#e8eaed;white-space:pre-wrap;",
    "  word-break:break-word;user-select:text;max-height:240px;overflow:auto;",
    "  flex:0 0 auto}",
    ".k-askout.k-wait{color:#9aa0a6}",
    ".k-aoh{display:flex;align-items:center;gap:6px;margin:6px 10px 0;padding:4px 8px;",
    "  font-size:11.5px;color:#9aa0a6;cursor:pointer;user-select:none;",
    "  border-radius:8px 8px 0 0;background:rgba(255,255,255,.03)}",
    ".k-aoh:hover{color:#c8ccd0}",
    ".k-askout.k-fold{display:none}",
    ".k-aostat{margin-left:auto;color:#f28b82;font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:62%}",
    ".k-aostat:empty{display:none}",
    // ---- 设置 ----
    ".k-gear{cursor:pointer;border:0;background:transparent;color:#9aa0a6;",
    "  font-size:13px;line-height:1;padding:2px 5px;border-radius:5px;flex:0 0 auto;margin-left:auto}",
    ".k-gear:hover{background:rgba(255,255,255,.1);color:#e8eaed}",
    /* 设置视图：覆盖整个面板，盖住主界面（含顶部状态条），只留自己的头部。 */
    ".k-settings{position:absolute;inset:0;z-index:6;display:flex;flex-direction:column;",
    "  background:#1e1f20;}",
    ".k-settings[hidden]{display:none}",
    ".k-sethead{display:flex;align-items:center;gap:6px;padding:7px 9px;",
    "  border-bottom:1px solid rgba(255,255,255,.1)}",
    ".k-back{cursor:pointer;border:0;background:transparent;color:#9aa0a6;",
    "  font-size:15px;line-height:1;padding:2px 6px;border-radius:5px;flex:0 0 auto}",
    ".k-back:hover{background:rgba(255,255,255,.1);color:#e8eaed}",
    ".k-setbody{flex:1;overflow:auto;padding:4px 0 10px}",
    ".k-row{display:flex;align-items:center;gap:10px;padding:7px 12px;font-size:11.5px}",
    ".k-row label{color:#c8ccd0;flex:0 0 auto}",
    ".k-sel2{flex:1;min-width:0;background:rgba(255,255,255,.07);",
    "  border:1px solid rgba(255,255,255,.16);border-radius:6px;color:#e8eaed;",
    "  font-size:11.5px;padding:5px 7px;outline:none}",
    ".k-inp{flex:0 0 auto;width:90px;background:rgba(255,255,255,.07);",
    "  border:1px solid rgba(255,255,255,.16);border-radius:6px;color:#e8eaed;",
    "  font-size:11.5px;padding:5px 7px;outline:none}",
    ".k-sel2:focus,.k-inp:focus{border-color:" + BRAND + "}",
    // ---- 附件（图片）chip ----
    ".k-atts{display:flex;flex-wrap:wrap;gap:5px;padding:0 10px 6px}",
    ".k-atts:empty{display:none}",
    ".k-atch{display:inline-flex;align-items:center;gap:5px;max-width:160px;",
    "  padding:3px 4px 3px 3px;border:1px solid rgba(138,180,248,.35);",
    "  background:rgba(138,180,248,.14);border-radius:7px;",
    "  font-size:10.5px;color:#e8eaed;flex:0 0 auto}",
    ".k-atch img{width:22px;height:22px;border-radius:4px;object-fit:cover;flex:0 0 auto}",
    ".k-atch .k-n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".k-atch .k-x{border:0;background:rgba(255,255,255,.1);color:#9aa0a6;",
    "  border-radius:3px;width:14px;height:14px;line-height:1;font-size:11px;",
    "  cursor:pointer;padding:0;flex:0 0 auto}",
    ".k-atch .k-x:hover{background:rgba(242,139,130,.3);color:#f28b82}",
    /* 拖文件悬停在面板上：整块描边高亮，和对话输入框一个味。 */
    ".k-panel.k-drag{box-shadow:0 0 0 2px rgba(138,180,248,.7),0 12px 40px rgba(0,0,0,.4)}",
    // ---- 区域截图（Ctrl+K）：蒙版 / 选区 / 结果操作条 ----
    ".k-rshot{position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.35);",
    "  cursor:crosshair;user-select:none}",
    ".k-rsel{position:absolute;border:1px solid #8ab4f8;",
    "  background:rgba(138,180,248,.12);box-shadow:0 0 0 1px rgba(0,0,0,.45)}",
    ".k-rhint{position:fixed;top:14px;left:50%;transform:translateX(-50%);",
    "  color:#fff;font-size:13px;background:rgba(0,0,0,.6);padding:6px 12px;",
    "  border-radius:6px;pointer-events:none}",
    ".k-shotimg{display:block;max-width:100%;max-height:240px;border-radius:8px;",
    "  border:1px solid rgba(255,255,255,.12);margin:2px 0}",
    ".k-actbar{display:flex;flex-wrap:wrap;gap:6px;padding:8px 0 2px}",
    ".k-b.k-sm{font-size:11px;padding:4px 8px}",
    "</style>",
    '<div class="k-panel" id="p">',
    '  <div class="k-link">',
    '    <span class="k-dot" id="ld"></span>',
    '    <span class="k-lurl" id="lu">检测中…</span>',
    '    <button class="k-lb" id="lb" type="button">去配置</button>',
    '    <button class="k-gear" id="gear" type="button" title="设置">⚙</button>',
    '    <button class="k-x" id="x" title="收起">&times;</button>',
    "  </div>",
    '  <div class="k-cfg" id="cfg" hidden>',
    '    <textarea class="k-cta" id="cta" rows="2" spellcheck="false"',
    '      placeholder="粘贴 keeper「浏览器」页的连接码"></textarea>',
    '    <div class="k-cfgrow">',
    '      <button class="k-save" id="csave" type="button">保存并连接</button>',
    '      <button class="k-cancel" id="ccancel" type="button">取消</button>',
    '    </div>',
    '    <div class="k-cfgtip" id="ctip">整段粘贴即可，不用拆开填。</div>',
    "  </div>",
    /* 占位文案就是这一节的用途本身，所以上面不再挂一个「对这一页提问」的小节
       标题——同一句话在一屏里出现两次，读起来像出了什么重复渲染。 */
    /* 会话横条放在**输入框上面**：它决定"这轮记到哪条会话"，是发消息前就该看见
       的事实，压在输入框下面（原来那个 <select> 的位置）会让人发完才知道记哪了。 */
    '  <div class="k-tabs" id="pick" title="这条对话记到哪条会话"></div>',
    '  <div class="k-atts" id="atts"></div>',
    '  <div class="k-askrow">',
    /* 引用要在输入框里**高亮**，而 textarea 是纯文本、做不到局部着色。
       所以叠一层：底层 qhl 渲染同样内容、文字透明、只给引用那几段上底色；
       上层 textarea 透明背景、负责真正接收输入。两层字体 / 内边距 / 换行
       规则必须逐项对齐，差一点高亮就会飘。 */
    '    <div class="k-qwrap">',
    '      <div class="k-qhl" id="qhl" aria-hidden="true"></div>',
    '      <textarea class="k-q" id="q" rows="2" spellcheck="false"',
    '        placeholder="对这一页提问"></textarea>',
    '    </div>',
    '    <button class="k-go k-primary" id="go" type="button">发送</button>',
    '  </div>',
    '  <div class="k-aoh" id="aoh" hidden>响应消息 <span class="k-aot">▾</span><span class="k-aostat" id="aostat"></span></div>',
    '  <div class="k-askout" id="ao" hidden></div>',
    '  <div class="k-sec k-sechead"><span>功能</span><span class="k-title" id="t"></span></div>',
    '  <div class="k-bar">',
    '    <button class="k-b" id="bAgent">网页Agent</button>',
    '    <button class="k-b" id="bTrans">翻译</button>',
    '    <button class="k-b" id="bSum">总结页面</button>',
    '    <button class="k-b" id="bShot">截图</button>',
    '    <button class="k-b" id="bRegion">框选</button>',
    '    <button class="k-b" id="bStop">停止</button>',
    '    <button class="k-b" id="bResume" hidden>继续</button>',
    '    <button class="k-b" id="bExit">退出</button>',
    "  </div>",
    /* 状态提示（如 Agent 步骤 N/M：决策中…）移到「功能」标题右边，右对齐、超长可横向滑动：
       不放进按钮栏是因为按钮已排成一行滚动，挤进去会换行；放标题右侧既不占地又始终可见。 */
    '  <div class="k-out" id="o"></div>',
    '  <div class="k-settings" id="set" hidden>',
    '    <div class="k-sethead">',
    '      <button class="k-back" id="bk" type="button" title="返回">←</button>',
    '      <b class="k-name">设置</b>',
    '    </div>',
    '    <div class="k-setbody">',
    '      <div class="k-sec">翻译</div>',
    '      <div class="k-row"><label>目标语言</label><select class="k-sel2" id="lang"></select></div>',
    '      <div class="k-sec">网页总结</div>',
    '      <div class="k-row"><label>节点字符数 &gt;</label><input class="k-inp" id="minc" type="number" min="0" step="10"></div>',
    '      <div class="k-sec">网页Agent</div>',
    '      <div class="k-row"><label>手机号</label><input class="k-inp" id="phone" type="text" placeholder="登录时填写用" maxlength="20"></div>',
    '      <div class="k-row"><label>最大步数</label><input class="k-inp" id="maxSteps" type="number" min="1" step="1" value="20"></div>',
    '      <div class="k-row"><label>重试次数</label><input class="k-inp" id="maxRetry" type="number" min="0" step="1" value="3"></div>',
    '      <div class="k-row"><label>重试间隔(秒)</label><input class="k-inp" id="retryDelay" type="number" min="0" step="1" value="2"></div>',
    '    </div>',
    '  </div>',
    "</div>",
    '    <div class="k-ball" id="b">K</div>',
  ].join("\n");

  // 区域截图蒙版是挂到主文档（documentElement）的，不在 shadow 内，而上面那套样式
  // 写在 shadow 的 <style> 里、够不到主文档元素。这里再往主文档注入一份蒙版样式，
  // 否则 ov 是个没样式的 0 尺寸 div —— data_url 有值、浮球也藏了，蒙版（十字光标）
  // 却死活不出现。结果图（#o 里）用的 .k-shotimg/.k-actbar 等仍在 shadow 内，不受影响。
  const ovStyle = document.createElement("style");
  ovStyle.textContent =
    ".k-rshot{position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.35);" +
    "cursor:crosshair;user-select:none}" +
    ".k-rsel{position:absolute;border:1px solid #8ab4f8;" +
    "background:rgba(138,180,248,.12);box-shadow:0 0 0 1px rgba(0,0,0,.45)}" +
    ".k-rhint{position:fixed;top:14px;left:50%;transform:translateX(-50%);" +
    "color:#fff;font-size:13px;background:rgba(0,0,0,.6);padding:6px 12px;" +
    "border-radius:6px;pointer-events:none}";

  // ---------- 挂载 ----------
  //
  // run_at 是 document_start：此刻 HTML 往往还没解析出 <html>，元素无处可挂。
  // 原来靠 50ms 轮询等，而 50ms 恰好是肉眼能看见的一帧 —— 「啪一下」的另一半原因。
  // 改成 MutationObserver 盯 document：<html> 一被解析出来就立刻挂，零延迟。
  //
  // 不需要等页面样式或内容：host 用 all:initial + position:fixed，不依赖页面任何
  // 样式，也不受页面 CSP 约束（DOM 注入不是脚本注入）。
  // 退出后留在角落的「重新打开」药丸：点一下把悬浮球唤回（整页刷新也会重新出现）。
  const launcher = document.createElement("div");
  launcher.id = "k-launcher";
  launcher.textContent = "打开网页助手";
  launcher.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483646;background:#1b74e8;color:#fff;padding:8px 14px;border-radius:20px;cursor:pointer;font:13px/1.2 system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3);display:none;";
  launcher.addEventListener("click", () => {
    try { launcher.style.display = "none"; host.style.display = ""; } catch (e) {}
    killAgent = false;
  });

  const mount = () => {
    const de = document.documentElement;
    if (!de) return false;
    try {
      if (!de.contains(host)) de.appendChild(host);
      if (!de.contains(ovStyle)) de.appendChild(ovStyle);
      if (!de.contains(launcher)) de.appendChild(launcher);
    } catch (e) {
      return false;
    }
    return true;
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
    /* 内核拒绝连接时 SW 给出的是**可执行**的句子（"去重新复制连接码"），
       已经是人话了，别再包一层前缀把它说得更含糊。 */
    if (/连接码不对|内核不认这个扩展|内核拒绝了这条连接/.test(msg)) {
      return msg;
    }
    if (/与内核的连接断了/.test(msg)) {
      return "和内核的连接断了。先看浮球状态条那里写的是什么——"
        + "如果是「连接码已过期」，去 keeper「浏览器」页复制新的连接码重填一次。";
    }
    return msg;
  };

  /** 复制一段文本，返回成功与否。
   *
   *  为什么不能直接调 ``navigator.clipboard``：content script 跑在**页面**的上下文
   *  里，而页面可能是 http（非安全上下文），那种页面上 ``navigator.clipboard``
   *  是 undefined。``execCommand`` 虽然早被废弃，但它在这些页面上仍然可用，是
   *  唯一出路——所以留着它当后备，而不是为了"用新 API"把一批页面甩在后面。
   *
   *  临时输入框放进 shadow root 而不碰页面 DOM：页面可能带严格 CSP 或全局
   *  CSS（``textarea{...}``），插进去既可能报错也可能被染上样式。
   */
  const copyText = (text) => {
    const s = String(text || "");
    if (!s) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(s);
        return true;
      }
    } catch (e) { /* 落到下面的后备 */ }
    try {
      const ta = document.createElement("textarea");
      ta.value = s;
      ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
      root.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      root.removeChild(ta);
      return !!ok;
    } catch (e) {
      return false;
    }
  };

  /** 把某个元素变成"点一下复制这段文本"，并在复制后给一次即时反馈。
   *
   *  反馈必须**可见**：剪贴板是看不见的地方，点了没反应时用户会以为按钮坏了，
   *  于是再点七八次。所以文案临时变成「✓ 已复制」，过一会儿自己还原。 */
  const makeCopyable = (el, getText, doneLabel = "✓ 已复制") => {
    el.classList.add("k-copy");
    el.title = "点击复制";
    let timer = null;
    el.addEventListener("click", () => {
      const ok = copyText(getText());
      if (timer) clearTimeout(timer);
      const origin = el.textContent;
      el.textContent = ok ? doneLabel : "复制失败";
      timer = setTimeout(() => { el.textContent = origin; }, 1400);
    });
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
  const bSum = $("bSum"), bShot = $("bShot"), bRegion = $("bRegion"), bTrans = $("bTrans"), bAgent = $("bAgent"), bExit = $("bExit"), bStop = $("bStop"), bResume = $("bResume");
  // 「框选」按钮 hover 时展示快捷键：Mac 上 Chrome 会把 manifest 里的 Ctrl+K 自动
  // 当成 ⌘K，所以按系统区分显示，免得 Mac 用户看到 Ctrl+K 却得按 ⌘K 的困惑。
  const IS_MAC = /Mac|iPhone|iPad|iPod/i.test(navigator.userAgent || navigator.platform || "");
  bRegion.title = "区域截图（拖拽框选） · " + (IS_MAC ? "⌘K" : "Ctrl+K");
  const ldot = $("ld"), lurl = $("lu"), lbtn = $("lb");
  const qbox = $("q"), goBtn = $("go"), pickBar = $("pick"),
        askOut = $("ao"), qhl = $("qhl");
  // 响应消息区可收缩：面板关闭时自动收起，收到/拉取到新消息时自动展开。
  const aoh = $("aoh");
  const aostat = $("aostat");
  let aoFold = false;
  const applyAo = () => {
    aoh.hidden = askOut.hidden;
    askOut.classList.toggle("k-fold", aoFold);
    const ar = aoh.querySelector(".k-aot");
    if (ar) ar.textContent = aoFold ? "▸" : "▾";
  };
  aoh.addEventListener("click", () => { aoFold = !aoFold; applyAo(); });
  mark("ask", "idle");

  /* ---------- 对这一页提问 ----------
   *
   * 这条路的上下文完全来自**用户给的东西**（URL / 标题 / 选中的文字），不是整页。
   *
   * 为什么不给整页：内核的 Playwright 只连得着桌面壳那个内置浏览器，够不到用户
   * 自己的 Chrome（见 keeper/browser/agent_browser.py 开头）。所以从浮球到 agent
   * 这一段里，页面内容只能由扩展自己送；而送多少是**用户的决定**——全选整页动辄
   * 几万字，塞进上下文既贵又挤掉问题本身。
   *
   * 于是"没选中文字"这件事必须**明说**：那种情况下 agent 只看得到一句问题和
   * 一个 URL，它会开始猜。把这件事藏起来，用户看到的是"模型不行"。
   */
  const SEL_LIMIT = 4000;   // 与内核 BALL_SELECTION_LIMIT 对齐

  const currentSelection = () => {
    try {
      const t = String((window.getSelection && window.getSelection()) || "");
      return t.replace(/\s+/g, " ").trim().slice(0, SEL_LIMIT);
    } catch (e) {
      return "";
    }
  };

  /* 已经添加的引用，按**添加顺序**存完整原文。
   *
   * 为什么必须存一份、而不是发送时现读 currentSelection()：用户点完「添加」
   * 必然要去浮球里打字，那时页面选区早就被清掉了（一点别处就没）。
   *
   * 为什么是数组而不是单个字符串：一次提问可能要引好几段（对比两处说法、
   * 挑两段一起问）。输入框里只放缩略占位（见 QUOTE_RE），完整原文按序号存在
   * 这里，发送那一刻才换上去。 */
  let quotedList = [];
  /* 待发送的图片附件（粘贴 / 拖放而来）。与对话输入框同款设计：不进文本框，
     独立成 chip；点 ✕ 整块删。每项 {name, dataUrl}。 */
  let pendingAttachments = [];

  /* 输入框里的引用占位，形如「【引用1：这是前五个…】」。
   *
   * 带序号是为了支持多段：占位被删掉一条时，剩下几条的序号仍然能对上各自
   * 的完整原文（靠下标取会整串错位）。
   * 括号选中文书名号：正文里极少出现，撞车概率远低于 [] 或 {}。 */
  const QUOTE_RE = /【引用(\d+)：[^】]*】/g;

  /** 把输入框的值渲染到高亮层：引用那几段包一层 mark，其余文字透明。 */
  const syncHighlight = () => {
    const raw = qbox.value;
    if (!raw) { qhl.innerHTML = ""; return; }
    /* 先转义再替换：页面文字里带 < 或 & 时，不转义就会被当成标签解析，
       轻则高亮错位，重则把选中内容变成真元素。 */
    const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    /* 末尾补一个换行：textarea 的值以 \n 结尾时最后一行不渲染，
       高亮层少一行就会整体错位。 */
    qhl.innerHTML = esc(raw).replace(
      /【引用(\d+)：[^】]*】/g,
      (m) => '<span class="k-qmark">' + m + "</span>"
    ) + "\n";
  };

  /** 追加一条引用（不覆盖已有的），并同步高亮。 */
  const insertQuote = (text) => {
    if (!text) return;
    quotedList.push(text);
    const idx = quotedList.length;
    /* 只留前 5 个字：够确认"加的是不是这段"，又不会把输入框占满。 */
    const brief = text.slice(0, 5) + (text.length > 5 ? "…" : "");
    const token = "【引用" + idx + "：" + brief + "】";
    /* 展示时**不换行**，用空格接着写：占位本来就只有几个字，多段挤在一行才
       看得出它们同属一次提问；每段各占一行的话，输入框会被几条短短的占位
       撑得很高，真正要写的问题反而被推到看不见的地方。
       分段留到发送那一刻再做（见 doAsk）—— 那时换的是完整原文，才需要边界。 */
    qbox.value = (qbox.value ? qbox.value.replace(/\s+$/, "") + " " : "") + token;
    syncHighlight();
    qbox.focus();
    // 光标挪到末尾：占位是刚追加的，用户接着要写"这两段有什么不一样"
    try { qbox.setSelectionRange(qbox.value.length, qbox.value.length); } catch (e) { }
  };

  /** 把输入框里所有引用占位按 quotedList 的顺序重新编号（1,2,3…）。
   *  删除某条后调用：quotedList 已 splice，重排 token 序号让显示与数据对齐。 */
  const renderQuotes = () => {
    const raw = qbox.value;
    let i = 1;
    const next = raw.replace(/【引用(\d+)：[^】]*】/g, () => {
      const t = quotedList[i - 1];
      if (t === undefined) return "";
      const brief = t.slice(0, 5) + (t.length > 5 ? "…" : "");
      return "【引用" + (i++) + "：" + brief + "】";
    });
    if (next !== raw) { qbox.value = next; syncHighlight(); }
  };

  /** 退格/Delete 紧贴某个引用占位时，一次性删掉整块（含 quotedList 里那条），
   *  而不是逐字符退。光标在 token 末尾按退格、在开头按 Delete、或直接选中
   *  整个 token 再删，都会触发整块删除。 */
  const onQuoteKey = (e) => {
    if (e.key !== "Backspace" && e.key !== "Delete") return;
    const val = qbox.value;
    const s = qbox.selectionStart, en = qbox.selectionEnd;
    let mStart = -1, mEnd = -1, mText = "";
    if (s !== en) {
      const mm = val.slice(s, en).match(/^【引用(\d+)：[^】]*】$/);
      if (mm) { mStart = s; mEnd = en; mText = mm[1]; if (val[mEnd] === " ") mEnd++; }
    } else if (e.key === "Backspace") {
      // token 后常带一个空格，一并吃掉
      const mm = val.slice(0, s).match(/【引用(\d+)：[^】]*】\s*$/);
      if (mm) { mStart = s - mm[0].length; mEnd = s; mText = mm[1]; }
    } else {
      const mm = val.slice(s).match(/^【引用(\d+)：[^】]*】/);
      if (mm) { mStart = s; mEnd = s + mm[0].length; mText = mm[1]; if (val[mEnd] === " ") mEnd++; }
    }
    if (mStart < 0) return;
    e.preventDefault();
    const n = parseInt(mText, 10);
    qbox.value = val.slice(0, mStart) + val.slice(mEnd);
    if (n >= 1 && n <= quotedList.length) quotedList.splice(n - 1, 1);
    renderQuotes();
    try { qbox.setSelectionRange(mStart, mStart); } catch (e2) {}
    syncHighlight();
  };

  const paintSelection = () => {
    const s = currentSelection();
    return s;
  };

  /* 选区变化很频繁（拖动选择的过程每个字符都触发），但提示文案按字数走，
     只在"有无/字数"变化时才重画，省掉无意义的 DOM 写入。 */
  let selPainted = -1;
  const onSelChange = () => {
    const n = currentSelection().length;
    if (n === selPainted) return;
    selPainted = n;
    paintSelection();
  };
  document.addEventListener("selectionchange", onSelChange);

  /* ---------- 选区浮标 ----------
   *
   * 选中文字后直接在选区边上冒出一个小按钮，一次点击就把这段加进提问。
   *
   * 为什么不用右键菜单：那个路径要"移动手到菜单 → 在一组项里挑一个"，而这
   * 里的动作**只有一个**（加进提问），不需要挑。出现在选区边上省掉一整个来回。
   * 动作多于一个时右键菜单才划算——那时"挑哪个"本身就是用户要做出的决定。
   */
  const bubble = document.createElement("button");
  bubble.type = "button";
  bubble.className = "k-bubble";
  bubble.textContent = "添加到提问";
  bubble.hidden = true;
  root.appendChild(bubble);

  /** 这次是否值得冒出浮标：太短的选区多半是误拖（比如想点链接却拖了两个字）。 */
  const worthQuoting = (t) => t.length >= 2;

  /* 浮标**当前代表的那段文字**，在浮标出现时就抓下来存着。
   *
   * 为什么不能等点击时再读选区：浮标在 shadow DOM 里，点它会把焦点带进 shadow
   * tree，浏览器随即把**页面**的 selection 清掉——mousedown 上的 preventDefault
   * 拦不住这一条（它拦的是"按下即开始新选择"，不是焦点迁移带来的清空）。
   * 于是点击时 currentSelection() 已经是空的，代码在 `if (!text) return` 处
   * 静默返回：浮标消失、面板维持原样，用户看到的就是"点了但什么都没发生"。
   * 在浮标出现那一刻就取值，才能让"按钮所指"和"看到它时选的那段"始终一致。 */
  let bubbleText = "";

  const placeBubble = (rect) => {
    bubble.hidden = false;
    /* 先放、再量：浮标的宽度只有它自己在文档里才知道（文案、字号、padding
       都会变），先设 left/top 再去读 offsetWidth 是最省事的办法。 */
    const w = bubble.offsetWidth || 96;
    const h = bubble.offsetHeight || 22;
    let left = rect.right - w;
    if (left < 8) left = 8;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
    // 选区下方放得下就放下方（更符合"顺着往下看"的习惯），放不下就翻到上方
    let top = rect.bottom + 6;
    if (top + h > window.innerHeight - 8) top = rect.top - h - 6;
    if (top < 8) top = 8;
    bubble.style.left = left + "px";
    bubble.style.top = top + "px";
  };

  const hideBubble = () => { bubble.hidden = true; };

  const showBubble = () => {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount < 1) return hideBubble();
    /* 输入框 / 文本域里的选区不算：用户在浮球里打字时正常拖选文字会误触发，
       而且那里的文字本来就已在输入框里，加进引用毫无意义。 */
    let node = sel.anchorNode;
    while (node && node.nodeType !== 1) node = node.parentNode;
    if (node && /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName || "")) return hideBubble();
    const text = currentSelection();
    if (!worthQuoting(text)) { bubbleText = ""; return hideBubble(); }
    let rect = null;
    try {
      rect = sel.getRangeAt(0).getBoundingClientRect();
    } catch (e) { /* 跨影子树等特殊情况下取不到 */ }
    if (!rect || (!rect.width && !rect.height)) { bubbleText = ""; return hideBubble(); }
    bubbleText = text;      // 就这一刻抓下来，点击时不再重读（见 bubbleText 的说明）
    placeBubble(rect);
  };

  /* 「刚刚按下的是不是浮标本身」。
   *
   * 为什么只能用标志、而不能在 document 的监听器里判断事件来源：root 是
   * **closed** shadow root，事件冒泡到外面时 ——
   *   · ev.target 被**重定向**成 host，所以 `ev.target === bubble` 恒为 false；
   *   · composedPath() 也会**省略** shadow tree 内部的节点，一样拿不到。
   * 两条路都堵死。但在浮标**自己**的 mousedown 里是拿得到的，所以就地记一笔，
   * 让外面那层来查。
   *
   * 这道门不能省：浮标若在自己按下时就被收走（display:none 发生在 mousedown 与
   * mouseup 之间），浏览器判定"两次落在同一元素"失败，click **根本不会派发**——
   * 按钮点了毫无反应，而代码里看不出任何异常。 */
  let onBubble = false;

  /* 用 mouseup 而不是 selectionchange：拖动选择的过程中每多选一个字符都会触发
     selectionchange，浮标会跟着抖成一片。松手才显示，是"我选完了"的信号。
     点浮标自己时也会走这里，但 showBubble 会按当前选区重新评估，无害。 */
  document.addEventListener("mouseup", () => {
    setTimeout(showBubble, 0);   // 让浏览器先把选区定下来
  });
  document.addEventListener("mousedown", () => {
    if (onBubble) { onBubble = false; return; }   // 放过浮标自己
    hideBubble();
  });
  window.addEventListener("scroll", hideBubble, true);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") hideBubble();
  });

  /* mousedown 上 preventDefault：挡掉"按下即开始新选择"，免得浮标刚出现就被
     下一次按下的默认行为收走。（它拦不住焦点迁移带来的选区清空，所以文本仍
     要在 showBubble 时就抓下来——见 bubbleText。） */
  bubble.addEventListener("mousedown", (ev) => {
    ev.preventDefault();   // 挡掉"按下即开始新选择"
    onBubble = true;       // 告诉外面那层：这次是我自己
  });
  bubble.addEventListener("click", () => {
    /* 用浮标出现时抓的那份，而不是此刻重读：此刻焦点已经进了 shadow tree，
       页面选区多半已被清掉。 */
    const text = bubbleText || currentSelection();
    bubbleText = "";
    hideBubble();
    /* 取消页面选区。不做这一步的话，mouseup 时排的那个 setTimeout(showBubble)
       会在本 handler **之后**执行（click 是同步派发的，宏任务在其后），而选区
       因为上面的 preventDefault 还在 —— 于是刚收起的浮标立刻又冒出来，看起来
       就是"点了没反应"。内容已经进了输入框，选区的使命到此为止。 */
    try {
      const sel = window.getSelection && window.getSelection();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
    } catch (e) { }
    if (!text) {
      /* 走到这里说明连缓存都没有（理论上不会）。给一句可见的话，
         别默默什么都不做——那正是这次这个 bug 最难看出的地方。 */
      if (!panel.classList.contains("k-open")) ball.click();
      setAskOut("⚠ 没拿到选中的文字，重新选一次试试", "k-wait");
      return;
    }
    /* 展开面板。必须判是否已展开：ball 的 click 是 toggle，开着时再点是收起。 */
    if (!panel.classList.contains("k-open")) ball.click();
    insertQuote(text);
  });

  /* 会话选择：存扩展自己的 storage，内核不需要知道用户上次选了哪条。
     不存内核是因为这条选择是**浮球这个界面**的偏好，而内核没有"某个浮球"的概念
     ——存过去就得再引入一张映射表，换来的只是一个能省两次点击的功能。

     记在单独一个变量里而不是读某个控件的 value：控件上的选中态是**渲染出来的
     结果**（横条的 .k-on 类），拿它当真相来源就得在每次改值后再去 DOM 里反查一遍，
     而选项列表又是异步到的——那段时间控件上根本没有可反查的东西。变量没有这个
     时序问题，控件只是它的投影。 */
  const SESS_KEY = "ballSessionId";
  /* 「新对话」在内部的表示。
   *
   * 为什么不能直接用空串：内核把"空的 session_id"解释成「用浮球默认会话」
   * （api/agent_bridge.py::_resolve_ball_target 的回退分支）。所以点「新对话」
   * 发出去，会记进上一条——用户选的是新会话，拿到的却是旧会话，界面上还没有
   * 任何迹象能让他发现。原先那个 <select> 里的「（新对话）」项也是这么假着。
   *
   * 所以新建必须**显式**告诉内核（new_session: true）。这个哨兵值不可能撞上真实
   * 会话 id——后者是 26 位 ULID。 */
  const NEW_SESS = "__new__";
  let chosenSess = "";
  try {
    chrome.storage.local.get([SESS_KEY]).then((g) => {
      chosenSess = (g && g[SESS_KEY]) || "";
      mark("session", chosenSess === NEW_SESS ? "new" : (chosenSess || "default"));
    }).catch(() => {});
  } catch (e) { /* 存储不可用就用默认值 */ }

  const isNewSess = () => chosenSess === NEW_SESS;

  const setChosen = (sid) => {
    chosenSess = sid || "";
    mark("session", isNewSess() ? "new" : (chosenSess || "default"));
    try { chrome.storage.local.set({ [SESS_KEY]: chosenSess }); } catch (e) { }
  };

  /* 画一个 tab。
   *
   * 抽到模块级（而不是留在 applySessions 里）是因为**横条不该依赖内核**：「新对话」
   * 是一个本地选项，不拉列表也该在。整个横条原先都由 applySessions 渲染，于是内核
   * 一断、`agent:sessions` 一失败，横条就是**彻底空的**：连「新对话」都没了，用户
   * 连"现在还能不能发消息"都看不出来。
   *
   * 代价是这里引用了 applySessions 之后才声明的 shownMsgId / setAskOut / askOut /
   * fetchLatest —— 但它们只在**点击时**才用到，那必然发生在脚本执行完毕之后，
   * 没有 TDZ 风险。 */
  const addTab = (sid, label, title) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "k-tab" + (sid === chosenSess ? " k-on" : "");
    b.textContent = label;
    b.dataset.sid = sid || "";
    if (title) b.title = title;
    b.addEventListener("click", () => {
      /* 点当前已选中的那条 = 什么都不该发生。原来挂在 <select> 的 change 上，
         浏览器只在值**变化**时才触发 change，所以重复点是安全的；换成 button
         的 click 就得自己守住这道门——否则手滑点两下，正在看的那条回答就被
         清空了还要重新拉一遍。 */
      if (sid === chosenSess) return;
      setChosen(sid);
      /* 就地更新高亮，不重拉列表：列表内容没变，重拉只会让滚动位置跳一下，
         用户点一下却看到界面在动。 */
      Array.prototype.forEach.call(pickBar.children, (el) => {
        el.classList.toggle("k-on", el.dataset.sid === (sid || ""));
      });
      const on = pickBar.querySelector(".k-on");
      if (on && on.scrollIntoView) {
        on.scrollIntoView({ block: "nearest", inline: "center" });
      }
      /* 换了会话，之前那条结果就不该继续占着显示位（这段原来挂在
         <select> 的 change 上，横向条没有 change，统一放到点击里）。 */
      shownMsgId = "";
      setAskOut("");
      askOut.hidden = true;
      applyAo();
      fetchLatest();
    });
    pickBar.appendChild(b);
  };

  /** 按会话列表重画整条横条（列表变了、或初次挂载时都走这里）。
   *
   *  ``persist=false`` 只更新内存、不落 storage：挂载那一刻 chosenSess 还没从
   *  storage 读回来（get 是异步的），这时 setChosen("") 会把用户上次选的那条
   *  **抹掉**——而它只是"还没读到"，不是"没有"。 */
  const paintTabs = (list, def, persist = true) => {
    pickBar.textContent = "";
    /* 「新对话」固定在最左：它不是列表里的一条，而是"这一轮另起一条会话"这个动作。 */
    addTab(NEW_SESS, "＋ 新对话", "新对话（这一轮另起一条会话）");
    list.forEach((s) => addTab(s.id, s.title || "未命名", s.title || "未命名"));

    // 优先级：用户上次选的（仍在列表里）→ 内核的默认那条 → 列表第一条。
    // 后两级不能省：内核重启、或用户在浏览器页新建/删除了会话，上次那条可能没了。
    //
    // def 也必须在**列表里**才认：内核的 default_id 指向的那条可能刚被删、或这次
    // 列表还没包含它。认了它却找不到对应 tab，结果是横条上一个都不高亮——比
    // 落到「新对话」更糟，因为用户看不出自己处在一个不存在的会话上。
    //
    // 新对话态要**放行**：它本来就不在列表里（还没建），被当成"已失效"清掉的话，
    // 用户刚点的「新对话」会被立刻弹回别的会话。
    if (chosenSess && chosenSess !== NEW_SESS && !list.some((s) => s.id === chosenSess)) {
      chosenSess = "";
    }
    const want = chosenSess
      || (list.some((s) => s.id === def) ? def : "")
      /* 列表只有一条时直接选它：横条上就它一个，不选中会显得"哪条都没选" */
      || (list.length === 1 ? list[0].id : "");
    if (persist) setChosen(want);
    else chosenSess = want;
    Array.prototype.forEach.call(pickBar.children, (el) => {
      el.classList.toggle("k-on", el.dataset.sid === (want || ""));
    });
  };

  /* 面板一挂上就先画一次空的：新对话这一项不依赖内核，先让它在，
     内核连上后 applySessions 会带着列表重画一遍（persist=false：此时
     storage 里的上次选择还没读回来，不能拿它去覆盖）。 */
  paintTabs([], "", false);

  const applySessions = (res) => {
    const list = (res && res.sessions) || [];
    const def = (res && res.default_id) || "";
    /* 选中态只以 chosenSess 为真相来源，DOM 上的 .k-on 类是它的投影。 */
    paintTabs(list, def);
    mark("sessions", String(list.length));
  };

  /** 会话列表没拿到：在横条末尾挂一个不可点的红色提示。
   *
   *  为什么非做不可：横条的内容全部来自内核，失败时若什么都不显示，用户看到的
   *  就只是"只有新对话"——与"加载成功、但我确实只有一条浮球会话"完全无法区分。
   *  而这两种情况的处置完全不同（前者去查连接 / token，后者什么都不用做）。 */
  const showTabsError = (why) => {
    const text = why || "会话加载失败";
    const b = document.createElement("button");
    b.type = "button";
    b.className = "k-tab k-bad";
    b.textContent = "⚠ " + text;
    /* 可一键复制：这类报错通常是"说给用户听的一句话 + 要他做的一个动作"，
       而真正的排查信息（Chrome 的原始英文、内核地址、token 状态）不在这句话里。
       用户要么得手动选中这行小字（浮球里它本来就窄），要么得去翻控制台——
       而绝大多数反馈里，对方要的第一句就是"把报错原文发我"。 */
    makeCopyable(b, () => text);
    pickBar.appendChild(b);
  };

  /* 这里**不**做并发去重。
   *
   * 试过，并因此出过事：用「正在拉就跳过」的布尔挡住重复请求，只要那一次
   * sendMessage 挂起（SW 被回收、WS 半开等），标志就**永远停在 true**，
   * 之后每次 loadSessions() 都被静默跳过 —— 表现正是"重新打开面板也拉不出
   * 会话"，而连接灯好端端是绿的，用户和代码都看不出卡在哪。
   *
   * 省一个内核往返换不来这个风险：会话列表是低频动作，偶发重复拉一次最多多一
   * 个往返，而卡死是永久的、且没有任何迹象。宁可重拉。 */
  const loadSessions = async () => {
    try {
      const r = await chrome.runtime.sendMessage({
        type: "keeper:request",
        payload: { cmd: "agent:sessions" },
      });
      if (r && r.ok) applySessions(r.result);
      else showTabsError((r && r.error) || "内核没回应");
    } catch (e) {
      showTabsError(explain(e));
    }
  };

  const askAgent = async (q, selection = "", attachments, opts = {}) => {
    /* 新对话态：session_id 传空 + new_session=true，让内核**新建**一条。
       只传空是不够的——空在 _resolve_ball_target 里的意思是「用浮球默认会话」，
       那样点了新对话却记进上一条。（NEW_SESS 是内部哨兵，绝不能原样发出去。）
       opts.sessionId / opts.newSession 供网页 Agent 复用同一条独立会话。 */
    const wantNew = opts.newSession != null ? opts.newSession : isNewSess();
    const sessId = opts.sessionId != null ? opts.sessionId : (wantNew ? "" : chosenSess);
    const r = await chrome.runtime.sendMessage({
      type: "keeper:request",
      payload: {
        cid: ensureCid() || "",
        cmd: "agent:ask",
        q,
        session_id: sessId,
        new_session: wantNew,
        /* 网页 Agent 发起的请求标记 browser_automation，后端据此把会话归到
           browser-automation 类别、不进浮球会话列表；浮球「对这一页提问」不带此标记。 */
        browser_automation: !!opts.browserAutomation,
        page: {
          url: location.href,
          title: document.title,
          /* selection 完全由调用方给出（见 doAsk）。
             只有**没点过「添加」**时才带当前选区：引用一旦进了输入框，它就已经是
             问题的一部分了，再在这里带一份，agent 会在同一条消息里看到同一段文字
             出现两次——一次在「页面选中内容」，一次在「问题」。 */
          selection: selection || "",
        },
        /* 图片附件：WS 内联的 base64 data URL 列表，内核落成附件后交给 agent。 */
        attachments: attachments && attachments.length ? attachments : undefined,
      },
    });
    if (!r) throw new Error("内核没响应（扩展可能已被禁用）");
    if (!r.ok) throw new Error(r.error || "内核返回失败");
    return r.result;
  };

  const setAskOut = (text, cls) => {
    askOut.textContent = text;
    askOut.className = "k-askout" + (cls ? " " + cls : "");
    askOut.hidden = false;
    aoFold = false; applyAo();   // 收到/拉取到消息即自动展开响应区
    /* 报错那几行都以「⚠ 」开头，让它们一键可复制。正常回答**不加**：
       那里用户常要的是自己拖选其中一段，整段复制反而抢了他的动作。
       （出错时他要的是"把这个报错发给对方"，所以整段正是他想要的。）
       用 onclick 赋值而不是 addEventListener：本函数会被反复调用，累加监听器
       会让一次点击连着复制好几遍。 */
    const isErr = String(text || "").startsWith("⚠ ");
    askOut.classList.toggle("k-copy", isErr);
    askOut.title = isErr ? "点击复制报错原文" : "";
    if (!isErr) { askOut.onclick = null; return; }
    const full = String(text);
    askOut.onclick = () => {
      const ok = copyText(full);
      askOut.textContent = full + (ok ? "\n\n✓ 已复制" : "\n\n复制失败");
      setTimeout(() => { askOut.textContent = full; }, 1400);
    };
  };

  /* 已经显示过的那条结果。判重只用这一个值——浮球只显示最新一条，所以「同一条
     消息被重新拉到」是常态（每次开面板都会补取一次），只有「多了一条新的」才该
     更新。不记它的话每次打开面板都会重写结果区，滚动位置被顶回顶部。 */
  let shownMsgId = "";

  /* 「这一轮还在跑」的轮询。2.5 秒一次，够跟手又不至于变成对内核的打扰；
     面板一关就停——没必要在看不见的时候继续问。 */
  let latestTimer = null;
  const scheduleLatest = () => {
    if (latestTimer || !panel.classList.contains("k-open")) return;
    latestTimer = setTimeout(() => {
      latestTimer = null;
      fetchLatest();
    }, 2500);
  };

  /** 补取某条会话的最新一条回答。与 SW 存活无关，所以它是 SW 被回收后的兜底。 */
  const fetchLatest = async () => {
    /* 新对话态直接跳过：这一轮还没有会话，没有"最新回答"可补。
       不跳过的话内核会拿浮球默认会话（那条旧会话）来答，把**上一轮的内容**
       显示在"新对话"的提问区里——用户会以为 agent 在复读。 */
    if (isNewSess()) return;
    try {
      const r = await chrome.runtime.sendMessage({
        type: "keeper:request",
        payload: { cmd: "agent:latest", session_id: chosenSess },
      });
      if (!r || !r.ok) return;
      const last = r.result || {};
      if (last.in_progress) {
        /* 这一轮还在跑（多半是浮球关着、或中途 SW 被回收过一次）。
           如实说"还在跑"并继续轮询——否则用户看到的是一片空白的提问区，
           分不清是还没开始还是已经失败了。 */
        setAskOut("这一轮还在跑…", "k-wait");
        mark("ask", "running");
        scheduleLatest();
        return;
      }
      if (!last.message_id || last.message_id === shownMsgId) return;
      shownMsgId = last.message_id;
      setAskOut(last.text || "（这一轮没有内容）");
      mark("ask", "done");
    } catch (e) {
      mark("ask", "err");
    }
  };

  /* ---------- 附件（图片）：粘贴 / 拖放 ----------
   *
   * 与对话输入框同款：文件不进文本框（否则退格逐字删文件名），独立成 chip 展示，
   * 点 ✕ 整块删。只收图片（与内核附件上传一致），体积上限 5MB。
   *
   * 为什么走 WS 内联 base64 而不是让内核再去拉：浮球本来就连着内核那条 WS，
   * 鉴权复用 token，没有跨域/CORS 那一摊事——和「截图」那条 data_url 同源。 */
  const ATT_EXT = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  const ATT_MAX = 5 * 1024 * 1024;
  const fileToDataUrl = (file) => new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = () => rej(r.error || new Error("read failed"));
    r.readAsDataURL(file);
  });
  const renderAttachments = () => {
    const box = $("atts");
    if (!box) return;
    box.textContent = "";
    pendingAttachments.forEach((a, i) => {
      const chip = document.createElement("div");
      chip.className = "k-atch";
      const img = document.createElement("img");
      img.src = a.dataUrl; img.alt = a.name;
      const name = document.createElement("span");
      name.className = "k-n"; name.textContent = a.name; name.title = a.name;
      const x = document.createElement("button");
      x.type = "button"; x.className = "k-x"; x.textContent = "×"; x.title = "移除";
      x.addEventListener("click", () => { pendingAttachments.splice(i, 1); renderAttachments(); });
      chip.appendChild(img); chip.appendChild(name); chip.appendChild(x);
      box.appendChild(chip);
    });
  };
  const addAttachments = async (files, label) => {
    // label 用于命名：粘贴来的截图没有文件名，统一叫「截图」；拖进来的真文件
    // 用原文件名。注意要带扩展名——内核按 id 目录列文件预览时靠扩展名猜 mime，
    // 没有扩展名会被当成 octet-stream，缩略图就裂了。
    for (const f of Array.from(files || [])) {
      if (!ATT_EXT.includes(f.type)) {
        setAskOut("⚠ 仅支持图片（png/jpg/gif/webp）：" + (f.name || ""), "k-wait");
        continue;
      }
      if (f.size > ATT_MAX) {
        setAskOut("⚠ 图片超过 5MB，换张小的：" + (f.name || ""), "k-wait");
        continue;
      }
      const ext = (f.type.split("/")[1] || "png").replace(/^x-/, "");
      const name = label ? (label + "." + ext) : (f.name || ("image." + ext));
      try {
        const dataUrl = await fileToDataUrl(f);
        pendingAttachments.push({ name, dataUrl });
        renderAttachments();
      } catch (e) {
        setAskOut("⚠ 读取失败：" + (f.name || ""), "k-wait");
      }
    }
  };

  // 粘贴：截图 / 复制的图片进剪贴板时，拦下默认行为转成附件。文字粘贴照常。
  qbox.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.files;
    if (items && items.length) { e.preventDefault(); addAttachments(items, "截图"); }
  });

  // 拖放：把图片拖到浮球面板上即添加。用计数应付子元素引发的 dragenter/leave 抖动。
  let dragDepth = 0;
  panel.addEventListener("dragenter", (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
      e.preventDefault(); dragDepth += 1; panel.classList.add("k-drag");
    }
  });
  panel.addEventListener("dragover", (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
      e.preventDefault();
    }
  });
  panel.addEventListener("dragleave", (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
      if (dragDepth > 0) dragDepth -= 1;
      if (dragDepth === 0) panel.classList.remove("k-drag");
    }
  });
  panel.addEventListener("drop", (e) => {
    dragDepth = 0; panel.classList.remove("k-drag");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      e.preventDefault();
      addAttachments(e.dataTransfer.files);
    }
  });

  let asking = false;
  const doAsk = async () => {
    if (asking) return;
    /* 引用占位在这一步**展开成完整原文**：输入框里只放了缩略（前 5 个字），
       那是为了不把用户想说的话挤走；真正发出去的必须是整段。
       按占位里的序号取对应那条，删掉占位就等于不要那条引用。 */
    const raw = qbox.value;
    const hasQuote = QUOTE_RE.test(raw);
    /* 展示时各段是挤在一行的，但**发出去时每段各占一行**：占位只有几个字所以
       挤得下，完整原文动辄几百字，黏在一起 agent 分不出边界在哪。
       所以从第二条起在前面补一个换行（第一条不补，免得开头多一个空行）。 */
    const q = raw
      .replace(QUOTE_RE, (m, i) => {
        const n = parseInt(i, 10);
        if (n < 1 || n > quotedList.length) return m;
        /* 每段自占一行，并标成**无序列表项**。
         *
         * 光换行还不够：引用和用户的问题在同一段纯文本里时，agent 只能靠位置猜
         * 哪句是提问——最后一段引用紧挨着问题那次，它就真把两者读成了一句。
         * 加一个列表标记，材料与提问的边界就是**显式**的，不用猜。 */
        return "\n- " + quotedList[n - 1] + "\n";
      })
      /* 换行前后残留的空格会让 Markdown 把列表项渲染成代码块或续行，
         先清掉；再把连续空行压成一个——引用之间留空行会把列表断开成两个。 */
      .replace(/[ \t]*\n[ \t]*/g, "\n")
      .replace(/\n{2,}/g, "\n")
      .trim();
    if (!q && pendingAttachments.length === 0) { setAskOut("先写点什么，或加张图。", "k-wait"); return; }
    asking = true;
    goBtn.disabled = true;
    qbox.disabled = true;
    // 立刻给反馈，不等内核：这一轮可能跑很久，没有这句用户会以为按钮坏了
    setAskOut("正在思考…", "k-wait");
    mark("ask", "waiting");
    try {
      /* 引用已经展开进 q 了，这里就**不再**带选区：否则同一段文字会出现两次。
         没点过「添加」时才把当前选区带上（浮球一直有的行为）。 */
      const atts = pendingAttachments.map((a) => ({ name: a.name, data_url: a.dataUrl }));
      const res = await askAgent(q, hasQuote ? "" : currentSelection(), atts);
      setAskOut(res && res.text ? res.text : "（没有返回内容）");
      // 置空而不是记下 id：这条已经被本地消费过，下次开面板补取时不必重画
      shownMsgId = "";
      mark("ask", "done");
      qbox.value = "";         // 只留最新一条，问完清空方便连续问
      /* 引用跟着清：它属于这一轮提问，留在那里下一轮会被误带上。
         序号一并归零，下一条又从「引用1」开始——否则用户会看到「引用7」这种
         莫名其妙的编号。（失败时不清，留着让用户直接重试。） */
      quotedList = [];
      pendingAttachments = [];
      renderAttachments();
      syncHighlight();
      paintSelection();
      if (res && res.session_id && res.session_id !== chosenSess) {
        /* 新建那一轮：把选中项从「新对话」切到刚落地的这条。不切的话用户接着
           追问第二条时仍是新对话态 —— 那就又建一条新会话，一次追问散成好几条，
           与他点「新对话」时"换个话题重新开始"的意图正好相反。 */
        if (isNewSess()) setChosen(res.session_id);
        loadSessions();       // 新建了一条，或内核回退了会话（选中的那条被删）
      }
    } catch (e) {
      setAskOut("⚠ " + explain(e), "k-wait");
      mark("ask", "error");
    } finally {
      asking = false;
      goBtn.disabled = false;
      qbox.disabled = false;
    }
  };

  goBtn.addEventListener("click", doAsk);
  // Enter 发送、Shift+Enter 换行：单行 textarea 上这是最符合预期的映射
  qbox.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      doAsk();
    }
  });
  /* 高亮层必须跟着输入框走，否则一打字就和高亮错位。
     input 覆盖打字 / 粘贴 / 删除；scroll 覆盖内容超出时的滚动；
     窗口尺寸变化会改变换行位置，也要跟着重排。
     （高亮层是"输入框内容的影子"，凡是能改变它渲染结果的事都得同步一次。） */
  qbox.addEventListener("input", syncHighlight);
  // 退格/Delete 紧贴引用占位时整块删（见 onQuoteKey）
  qbox.addEventListener("keydown", onQuoteKey);
  qbox.addEventListener("scroll", () => { qhl.scrollTop = qbox.scrollTop; });
  window.addEventListener("resize", syncHighlight);
  syncHighlight();

  /* 横向条没有 <select> 的 change：选中变化由各 tab 自己的 click 处理
     （见 applySessions 里的 addTab），那里顺带做清结果 + 拉该会话最新回答。 */
  paintSelection();

  /* 竖向滚轮 → 横向滚动。
     会话一多就横向溢出，而面板是个窄条，鼠标滚轮默认只推纵向——用户会发现
     "条子滑不动"。所以把 deltaY 转成 scrollLeft。
     必须挂 { passive: false } 才能 preventDefault：不拦的话滚轮会先把整个面板
     （甚至页面）滚走，横条却纹丝不动。宽裕时不管，免得用户想滚别的却被按住。 */
  pickBar.addEventListener("wheel", (e) => {
    if (pickBar.scrollWidth <= pickBar.clientWidth) return;
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;  // 已是横向手势
    const before = pickBar.scrollLeft;
    pickBar.scrollLeft = before + e.deltaY;
    if (pickBar.scrollLeft !== before) e.preventDefault();
  }, { passive: false });

  /* 内核的中间通知。它**不**带结果，只用于把选择器与内核的真实落点对齐：
     用户选的那条会话可能刚在浏览器页被删了，内核会静默回退到默认会话，
     而选择器还停在那条已不存在的 id 上——不更新的话用户会以为消息进了
     他指定的地方。 */
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || !msg.type) return;
    if (msg.type === "keeper:resume-agent") {
      // 别的标签移交过来的 Agent 任务：在本标签（新页面）继续，保留历史与会话。
      // 把「来路」标签（点链接前的那一页）记下来，供 close_tab 日后清理掉。
      if (typeof msg.prevTabId === "number") prevTabId = msg.prevTabId;
      runAgent(msg.goal, msg.history, msg.sessionId);
      return;
    }
    if (msg.type === "keeper:note") {
      if (!msg.note) return;
      const n = msg.note;
      if (n.event === "accepted") {
        mark("askAccepted", n.session_id || "");
        if (n.session_id && n.session_id !== chosenSess) loadSessions();
      }
      return;
    }
    if (msg.type === "keeper:region-shot") {
      // 快捷键 Ctrl+K 触发的框选截图
      void startRegionShot();
    }
  });

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
      if (aostat) aostat.textContent = "未配置内核";
      return;
    }
    /* 正在握手：单独给一个状态，别让它落在「未连接」里。
       「未连接」在这几毫秒里其实不假，但它同时覆盖了「根本没打算连」和「连不上」，
       而用户那会儿真正想要的只是一个字面的确认：**我在连了**。

       这个 connecting 来自 SW 里**真实的握手状态**，不是这里乐观猜的 —— 见
       background.js 里 keeper:status 那段注释：状态查询刻意不触发重连，否则 MJ3
       的 SW 每次被唤醒都顺手一连，状态条就会在**断线时一直闪「连接中…」**，反而
       看不出是断了还是填错了。真握手失败时 connecting 会被清掉，这里自然回到
       「未连接」并带上被拒的原因。 */
    if (s.connecting && !s.ok) {
      ldot.className = "k-dot k-wait";
      lurl.textContent = "连接中…";
      lbtn.textContent = "重新连接";
      lbtn.title = "正在与内核建立连接";
      mark("link", "connecting");
      if (aostat) aostat.textContent = "连接中…";
      return;
    }
    ldot.className = s.ok ? "k-dot k-on" : "k-dot";
    /* 被内核拒绝过时直接说原因，别只显示「未连接」——拒绝和没连上是两回事，
       下一步也完全不同：前者要重新复制连接码，后者先重试。而「未连接」这三个字
       在这两种情况下都成立，等于把有用的信息抹掉了。 */
    if (!s.ok && s.denialText) {
      lurl.textContent = s.denial === "token" ? "连接码已过期" : "内核拒绝了这个扩展";
      lbtn.textContent = "重新连接";
      lbtn.title = s.denialText;
      mark("link", "denied-" + s.denial);
      if (aostat) aostat.textContent = (s.denial === "token" ? "连接码已过期" : "内核拒绝了这个扩展");
      return;
    }
    lurl.textContent = shortUrl(s.url) + (s.ok ? "" : " · 未连接");
    lbtn.textContent = "重新连接";
    lbtn.title = "重试一次连接，并可粘贴新的连接码";
    mark("link", s.ok ? "ok" : "configured-notok");
    if (aostat) aostat.textContent = s.ok ? "" : "与内核未连接";
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
  /* 上一次探到的「是否真的连上了」。用来识别**跃迁**：只有从未连上变成已连上
     那一次才去拉会话列表，而不是每 4 秒的轮询都拉一遍。 */
  let wasLinked = false;
  const refreshLink = async () => {
    let s = null;
    try {
      s = await chrome.runtime.sendMessage({ type: "keeper:status" });
      paintLink(s);
      deadStrikes = 0;
    } catch (e) {
      if (++deadStrikes < 2) return;   // 一次失败多半是 SW 正在启动，不打扰
      deadContext = true;
      ldot.className = "k-dot";
      lurl.textContent = "扩展已更新 · 请刷新本页";
      lbtn.textContent = "刷新页面";
      lbtn.title = "刷新本页后浮球才会重新工作（⌘⇧R / Ctrl+F5）";
      if (aostat) aostat.textContent = "扩展已更新 · 请刷新本页";
      mark("link", "dead-context");
      cfgBox.hidden = true;
      return;
    }
    /* 刚连上就去拉会话列表。
     *
     * 为什么必须补这一步：会话列表原先只在**面板展开那一刻**拉一次。用户第一次
     * 打开面板时常正好赶上内核刚开始握手，那一拉必然失败（"没连上内核"），
     * 之后即使连接成功，列表也再不会刷新 —— 表现就是"得重新打开一次浮球才出
     * 会话"。而连接成功本身就是唯一值得重拉的时刻：列表里的内容在另一边随时
     * 会变（浏览器页新建/删除/改名）。 */
    const linked = !!(s && s.ok);
    if (linked && !wasLinked) {
      // 面板关着时不必拉：看不见的东西拉回来只是白跑一个内核往返
      if (panel.classList.contains("k-open")) loadSessions().then(fetchLatest);
    }
    wasLinked = linked;
  };

  /* ---------- 就地填连接码 ----------
   *
   * 解析与存盘都在 service worker 里（keeper:save-code），这里只负责收文本、
   * 传出去、把结果显示出来。刻意不在 content script 里再实现一份解析：格式以后
   * 若变，改一处就够，两处各写一份迟早会漏改一个。
   */
  const cfgBox = $("cfg"), cta = $("cta"), csave = $("csave"), ctip = $("ctip"), ccancel = $("ccancel");
  // 取消：收起连接码输入框并清空，等效于"不保存"。
  ccancel.addEventListener("click", () => { showCfg(false); cta.value = ""; });
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
    /* 出错时让这行可一键复制：连接码这类问题，对方要的第一句就是报错原文，
       而这行字在浮球里又窄又小，手动拖选很别扭。
       用 onclick 赋值：本函数会被反复调用，累加监听器会让一次点击复制好几遍。 */
    ctip.classList.toggle("k-copy", !!bad);
    ctip.title = bad ? "点击复制报错原文" : "";
    if (!bad) { ctip.onclick = null; return; }
    const full = String(text || "");
    ctip.onclick = () => {
      const ok = copyText(full);
      ctip.textContent = ok ? "✓ 已复制" : "复制失败";
      setTimeout(() => { ctip.textContent = full; }, 1400);
    };
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
      //
      // 这两行只是**等到第一次真实状态回来之前**的占位：紧接着的 refreshLink
      // 会按 SW 报上来的 connecting / ok 覆盖掉它。所以不用等它自己消失。
      lurl.textContent = "连接中…";
      ldot.className = "k-dot k-wait";
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

  const idle = (s) => { title.textContent = s; };

  const setOut = (node, hint) => {
    out.textContent = "";
    out.appendChild(node || document.createTextNode(hint || ""));
  };

  const busy = async (btn, label, fn) => {
    const btns = [bSum, bShot, bRegion, bTrans];
    btns.forEach((x) => (x.disabled = true));
    const old = btn.textContent;
    const oldTitle = title.textContent;
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
      // 处理器若自行改写了状态条（如截图后的「已添加截图」）就保留；
      // 否则把 busy 占用的「正在…」还原，避免提示符卡住不消失。
      if (title.textContent === label) title.textContent = oldTitle;
    }
  };

  // ---------- 总结页面 ----------
  // 抓整页正文，按设置里的「节点字符数 >」阈值过滤掉小碎片（导航/按钮等噪声），
  // 把过滤后的正文塞进输入框，再追加总结提示并直接发送。
  bSum.addEventListener("click", () =>
    busy(bSum, "正在总结中", async () => {
      const r = await send({ op: "text", max_chars: 200000, min_node_chars: cfg.minChars });
      const pageText = (r && r.text) || "";
      if (!pageText.trim()) {
        idle("这一页没有可提取的正文");
        return;
      }
      const sentence = "请简要总结上述网页内容";
      let v = qbox.value;
      // 去掉已存在的同类提示，避免重复累加
      v = v.replace(/请简要总结上述网页内容[^\n]*\n?/g, "").replace(/\s+$/, "");
      v += (v.length ? "\n\n" : "") + pageText + "\n\n" + sentence;
      qbox.value = v;
      syncHighlight();
      qbox.focus();
      await doAsk();   // 追加提示后直接发送；结果回来后 busy 自动收起「正在总结中」
    })
  );

  // ---------- 网页 Agent（模型操作浏览器） ----------
  // 扩展侧自驱动循环：每步截一张带 SoM 标注的图 + 可交互元素清单，交给 askAgent
  // （复用现有多模态 LLM 通道，开一条独立会话）选出下一步动作，再在**本页面**就地执行
  // （滚动/点击/输入/后退）。无需内核反向通道，动作全在内容脚本里做。
  let agentRunning = false;
  let killAgent = false;
  let prevTabId = null;   // 上一次手递交给本标签的那个「来路」标签，close_tab 时关它
  let stepMarks = [];
  let pendingToasts = []; // 动作后短暂出现的 toast 文本，累积到下一步观测里喂给模型
  let agentPaused = false;   // wait 动作触发：暂停循环，等用户在页面完成操作（如验证码）
  let resumeInfo = null;     // 暂停时保存 {goal, history, sessionId}，供「继续」恢复
  let waitTimer = null;      // wait 的自动继续定时器
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const collectMarks = () => {
    const sel = 'a,button,input,select,textarea,[role="button"],[tabindex],[onclick]';
    const vw = window.innerWidth, vh = window.innerHeight;
    const seen = new Set();
    const list = [];
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none" || cs.opacity === "0") continue;
      const key = Math.round(r.left) + "," + Math.round(r.top) + "," + el.tagName;
      if (seen.has(key)) continue;
      seen.add(key);
      const text = (el.value != null && el.value !== "") ? el.value
        : (el.innerText || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      list.push({ el, rect: r, tag: el.tagName.toLowerCase(), text: text.slice(0, 50) });
    }
    list.sort((a, b) => (a.rect.top - b.rect.top) || (a.rect.left - b.rect.left));
    return list.slice(0, 60);
  };

  // 抓取当前可见的「轻提示 / 弹层」文本（toast、aria-live 播报、各类通知组件）。
  // 这类提示常是短暂的，截图时机容易错过，所以单独把文字抽出来喂给模型。
  const collectToasts = () => {
    const out = [];
    try {
      const sel = [
        '[role="alert"]', '[role="status"]', '[aria-live]',
        '.toast', '.notification', '.alert',
        '.ant-message', '.ant-notification', '.el-message', '.el-notification',
        '.Toastify', '[class*="toast" i]', '[class*="notify" i]',
      ].join(",");
      document.querySelectorAll(sel).forEach((el) => {
        const r = el.getClientRects();
        if (!r || !r.length) return; // 不可见
        const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (txt && txt.length <= 200) out.push(txt);
      });
    } catch (e) {}
    return out;
  };

  // 截视口图 → 画 SoM 编号框 → 返回标注图(data_url) + 百分比清单 + 元素引用
  const doSom = () => new Promise((resolve, reject) => {
    // 截图前把浮球 UI（球 + 面板）临时藏起来：captureVisibleTab 抓的是整块可见视口，
    // 面板若开着会被一起截进图里，328px 的面板会盖住右侧大片页面，干扰 Agent 对页面
    // 内容的判断。截完（拿到图）立刻恢复——这样你仍能在面板右侧实时看到步骤状态，
    // 又不会把面板拍进截图。（等价于"点击 Agent 后关面板"的诉求，但不丢失状态可见性。）
    const prevVis = host.style.visibility;
    host.style.visibility = "hidden";
    send({ op: "shot", full_page: false, quality: 80 })
      .then((shot) => {
        host.style.visibility = prevVis;
        const img = new Image();
        img.onload = () => {
          const W = img.naturalWidth, H = img.naturalHeight;
          const vw = window.innerWidth, vh = window.innerHeight;
          // 截图像素尺寸与视口 CSS 尺寸未必同比（HiDPI / 浏览器缩放 /
          // captureVisibleTab 取整 / 滚动条），x、y 轴分别换算，避免框被拉伸、编号错位。
          const sx = W / vw, sy = H / vh;
          const els = collectMarks();
          const canvas = document.createElement("canvas");
          canvas.width = W; canvas.height = H;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, W, H);
          const pctMarks = els.map((m, i) => {
            const id = i + 1;
            const x = m.rect.left, y = m.rect.top, w = m.rect.width, h = m.rect.height;
            ctx.strokeStyle = "#ff3b30";
            ctx.lineWidth = Math.max(2, 2 * Math.max(sx, sy));
            ctx.strokeRect(x * sx, y * sy, w * sx, h * sy);
            ctx.fillStyle = "#ff3b30";
            ctx.font = Math.max(11, 12 * Math.max(sx, sy)) + "px sans-serif";
            const ty = y * sy - 3 < 0 ? y * sy + 12 : y * sy - 3;
            ctx.fillText(String(id), x * sx + 2, ty);
            return {
              id, tag: m.tag, text: m.text,
              x: +(x / vw).toFixed(3), y: +(y / vh).toFixed(3),
              w: +(w / vw).toFixed(3), h: +(h / vh).toFixed(3),
            };
          });
          console.log("[keeper] doSom 观测页面:", location.href, "| 标题:", document.title, "| 可交互元素:", els.length);
          const live = collectToasts();
          const all = [...new Set([...pendingToasts, ...live].filter(Boolean))];
          pendingToasts = [];
          resolve({
            data_url: canvas.toDataURL("image/jpeg", 0.85),
            width: vw, height: vh, marks: pctMarks, els,
            url: location.href, title: document.title,
            senderTabId: shot.senderTabId != null ? shot.senderTabId : null,
            capturedTabId: shot.capturedTabId != null ? shot.capturedTabId : null,
            toasts: all.join(" | "),
          });
        };
        img.onerror = () => reject(new Error("截图加载失败"));
        img.src = shot.data_url;
      })
      .catch((e) => { host.style.visibility = prevVis; reject(e); });
  });

  // 尽力解析模型返回的动作 JSON（去 ``` 围栏，截取首个 {...}）
  const parseAction = (text) => {
    if (!text) return null;
    let s = String(text).trim().replace(/^```(?:json)?/i, "").replace(/```$/i, "").trim();
    const a = s.indexOf("{"), b = s.lastIndexOf("}");
    if (a < 0 || b <= a) return null;
    try { return JSON.parse(s.slice(a, b + 1)); } catch (e) { return null; }
  };

  const normCoord = (v) => (typeof v === "number" ? (v > 1 ? v / 100 : v) : 0);

  const execAction = async (a) => {
    const act = String(a.action || "").toLowerCase();
    console.log("[keeper] 执行动作:", act, "| 参数:", JSON.stringify(a));
    if (act === "scroll_down") window.scrollBy(0, Number(a.px) || 300);
    else if (act === "scroll_up") window.scrollBy(0, -(Number(a.px) || 300));
    else if (act === "back") { console.log("[keeper] >>> history.back() 触发，当前页:", location.href); history.back(); }
    else if (act === "click") {
      const m = typeof a.elem === "number" ? stepMarks[a.elem - 1] : null;
      // 元素可能因页面跳转而脱离文档（detached）：此时强制走坐标点击，避免对失效节点
      // 静默空点，导致模型以为"点了没反应"而反复重试、最终误判为需回退。
      if (m && m.el && m.el.isConnected) m.el.click();
      else {
        const px = normCoord(a.x) * window.innerWidth, py = normCoord(a.y) * window.innerHeight;
        const t = document.elementFromPoint(px, py);
        if (t) t.click();
      }
    } else if (act === "input") {
      let el = null;
      const m = typeof a.elem === "number" ? stepMarks[a.elem - 1] : null;
      if (m && m.el && m.el.isConnected) el = m.el;
      else if (typeof a.x === "number") {
        const px = normCoord(a.x) * window.innerWidth, py = normCoord(a.y) * window.innerHeight;
        el = document.elementFromPoint(px, py);
      }
      if (el && el.focus) {
        el.focus();
        try {
          const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
            : el.tagName === "SELECT" ? window.HTMLSelectElement.prototype
            : window.HTMLInputElement.prototype;
          Object.getOwnPropertyDescriptor(proto, "value").set.call(el, a.text != null ? String(a.text) : "");
        } catch (e) { el.value = a.text != null ? String(a.text) : ""; }
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    } else if (act === "close_tab") {
      // 关掉「来路」标签（通常是点链接进官网时留下的上一个搜索/列表页），焦点自然回到当前页。
      // content script 关不了普通页，交给 SW 用 chrome.tabs.remove 处理。
      await new Promise((res) => {
        try { chrome.runtime.sendMessage({ type: "keeper:close-tab", prevTabId }, () => res()); }
        catch (e) { res(); }
      });
    } else {
      throw new Error("未知动作：" + act);
    }
  };

  const buildAgentPrompt = (goal, obs, phone) => {
    const marksTxt = obs.marks.map((m) =>
      `#${m.id} [${m.tag}] ${m.text} @(${(m.x * 100).toFixed(0)}%,${(m.y * 100).toFixed(0)}%)`).join("\n");
    const toastsTxt = (obs.toasts && obs.toasts.trim()) ? obs.toasts.trim() : "";
    return [
      `# 网页操作智能体`,
      `**用户目标**：${goal}`,
      ``,
      `## 当前页面`,
      `- **标题**：${obs.title}`,
      `- **地址**：${obs.url}`,
      `- **视口**：${obs.width}x${obs.height}px`,
      (phone ? `> **登录信息**：如需填写手机号，请使用 **${phone}**` : ""),
      ``,
      `## 可交互元素（已在截图中标注编号）`,
      marksTxt,
      ``,
      (toastsTxt ? `> **⚠ 页面提示 / 弹层（截图可能未捕捉到，务必参考）**：${toastsTxt}` : ""),
      ``,
      `## 输出格式`,
      `**每步只输出一个 JSON 对象**，不要解释、不要 markdown 围栏：`,
      `\`\`\`json`,
      `{"action":"click","elem":N}                // 按编号点击，最准确`,
      `{"action":"click","x":0.45,"y":0.30}        // 或按坐标(0~1 百分比)点击`,
      `{"action":"scroll_down","px":400} / {"action":"scroll_up","px":400}`,
      `{"action":"input","elem":N,"text":"..."}    // 按编号输入`,
      `{"action":"input","x":0.45,"y":0.30,"text":"..."}`,
      `{"action":"back"}                           // 返回上一页（谨慎使用）`,
      `{"action":"close_tab"}   // 跳到无关新标签时关掉它，焦点回到任务页`,
      `{"action":"done","text":"完成情况摘要"}      // 目标达成时输出`,
      `{"action":"wait","seconds":30}  // 需要用户输入时等待,并向用户提问`,
      `\`\`\``,
      ``,
      `## 规则`,
      `- **坐标**一律用 \`0~1\` 百分比（相对视口）；**优先用 elem 编号**点击，更准。`,
      `- 点击导致正常跳转（进详情页 / 新开标签）是**预期行为**，应在新页面继续；**除非明确撤销上一步，否则不要用 back**。`,
      `- 遇到**登录页 / 验证码 / 需人工输入**才能继续的情况，使用 **wait** 暂停，等人工处理完再继续，不要自行猜测或提前 done。`,
      ``,
      `**现在请根据用户目标，只输出下一步的 JSON：**`,
    ].join("\n");
  };

  const runAgent = async (goal, resumeHistory, resumeSession) => {
    if (agentRunning || agentPaused) return;
    agentRunning = true;
    killAgent = false;
    [bAgent, bSum, bShot, bRegion, bTrans].forEach((x) => (x.disabled = true));
    bResume.hidden = true; // 新一轮开始时先藏起「继续」，等 wait 再显示
    const history = (resumeHistory && Array.isArray(resumeHistory)) ? resumeHistory.slice() : [];
    let sessionId = resumeSession || null;
    try {
      for (let n = 1; n <= cfg.maxSteps; n++) {
        if (killAgent) { idle("已退出"); break; }
        const obs = await doSom();
        stepMarks = obs.els;
        // 跳新标签检测：若这一拍截图实际来自的 tab 与 Agent 自身 tab 不同，说明点击打开了
        // 新标签（如从搜索结果进官网）。把 Agent 整体移交给新标签继续，保留历史与会话；
        // 不再把焦点拽回旧页。tab id 全部由 SW 在截图回包里给出，content script 不碰 chrome.tabs。
        if (typeof obs.capturedTabId === "number" && typeof obs.senderTabId === "number"
            && obs.capturedTabId !== obs.senderTabId) {
          prevTabId = obs.senderTabId; // 旧标签记为「来路」，供 close_tab 关掉
          console.log("[keeper] 检测到跳到新标签 tabId=", obs.capturedTabId, "→ 移交 Agent 给新标签继续（保留历史）");
          try {
            await sleep(600); // 等新标签 content script 注入就绪，确保能收到移交消息
            chrome.runtime.sendMessage({ type: "keeper:resume-agent", tabId: obs.capturedTabId, goal, history, sessionId, prevTabId: obs.senderTabId });
          } catch (e) {}
          return; // 结束本标签的循环（finally 会复位状态）
        }
        const prompt = buildAgentPrompt(goal, obs, cfg.phone);
        // 截图附件名：任务名称 + 步数（如「查天气_3.jpg」），便于在记录里辨识。
        // 任务名可能含路径/通配等非法字符，先洗成安全片段再拼步数。
        const safeGoal = goal.replace(/[\\/:*?"<>|\r\n\t]/g, "_").slice(0, 40);
        // 模型若没返回有效的 action / done，就重发上一条消息重试，最多 cfg.maxRetry 次；
        // 重试复用同一张截图与提示，不额外消耗步数，只在同一观察下反复催模型出合规动作。
        let action = null, tried = 0;
        while (tried <= cfg.maxRetry) {
          tried++;
          if (tried > 1 && cfg.retryDelay > 0) await sleep(cfg.retryDelay * 1000);
          idle(`Agent 步骤 ${n}/${cfg.maxSteps}：决策中…` + (tried > 1 ? `（重试 ${tried - 1}/${cfg.maxRetry}）` : ""));
          const res = await askAgent(prompt, "", [{ data_url: obs.data_url, name: `${safeGoal}_${n}.jpg` }],
            sessionId ? { sessionId, browserAutomation: true } : { newSession: true, browserAutomation: true });
          if (!sessionId && res && res.session_id) sessionId = res.session_id;
          action = parseAction(res && res.text);
          console.log("[keeper] 步骤", n, "模型返回:", JSON.stringify(action), "| 观测URL:", obs.url);
          if (action && action.action) break; // 拿到有效动作或 done，跳出重试
        }
        if (!action || !action.action) {
          idle(`Agent 步骤 ${n}：模型重试 ${cfg.maxRetry} 次仍未返回有效动作，已停止`);
          break;
        }
        if (action.action === "done") {
          setAskOut(action.text || "目标已完成。", "");
          idle("Agent 完成");
          break;
        }
        if (action.action === "wait") {
          // 页面需要人工操作（登录 / 输入验证码等）：暂停循环，等用户在页面完成后再继续。
          const secs = Number(action.seconds) || 0;
          history.push({ action });
          agentPaused = true;
          resumeInfo = { goal, history: history.slice(), sessionId };
          bResume.hidden = false;
          idle("等待中：请在页面完成人工操作（如登录 / 验证码），然后点【继续】" + (secs ? `（${secs} 秒后自动继续）` : ""));
          if (secs > 0) {
            clearTimeout(waitTimer);
            waitTimer = setTimeout(() => { if (agentPaused) bResume.click(); }, secs * 1000);
          }
          return; // 结束本标签循环；finally 复位状态，但保留 agentPaused / resumeInfo 供「继续」
        }
        idle(`Agent 步骤 ${n}/${cfg.maxSteps}：${action.action}` + (action.elem ? ` #${action.elem}` : ""));
        await execAction(action);
        history.push({ action });
        await sleep(500); // 等页面响应/跳转稳定
        // 抓动作后短暂出现的 toast/提示：截图时机容易错过，单独存起来下一步再带给模型。
        const t = collectToasts();
        if (t.length) { pendingToasts.push(...t); idle("页面提示：" + t.join(" | ")); }
        if (killAgent) { idle("已退出"); break; }
      }
      if (agentRunning) idle("Agent 已达最大步数或结束");
    } catch (e) {
      idle("Agent 出错：" + explain(e));
    } finally {
      agentRunning = false;
      killAgent = false;
      [bAgent, bSum, bShot, bRegion, bTrans].forEach((x) => (x.disabled = false));
    }
  };

  bAgent.addEventListener("click", () => {
    const goal = qbox.value.trim();
    if (!goal) { idle("请输入操作目标"); return; }
    runAgent(goal);
  });

  bExit.addEventListener("click", () => {
    // 退出悬浮球：停掉可能正在跑的 Agent，隐藏主面板、露出「重新打开」药丸。
    // 不 remove，便于一键唤回；整页刷新也会因 content script 重载而重新出现。
    killAgent = true;
    agentRunning = false;
    agentPaused = false; resumeInfo = null; clearTimeout(waitTimer);
    try { host.style.display = "none"; } catch (e) {}
    try { if (ovStyle && ovStyle.parentNode) ovStyle.remove(); } catch (e) {}
    try { launcher.style.display = ""; } catch (e) {}
  });

  bStop.addEventListener("click", () => {
    // 停止任务：中止 Agent 循环（保留浮球），并清除可能的等待 / 暂停状态。
    killAgent = true;
    agentPaused = false; resumeInfo = null; clearTimeout(waitTimer);
    bResume.hidden = true;
    idle("已停止任务");
  });

  bResume.addEventListener("click", () => {
    // 继续：从 wait 暂停处恢复 Agent（沿用同一目标 / 历史 / 会话）。
    if (!resumeInfo) return;
    const g = resumeInfo.goal, h = resumeInfo.history, s = resumeInfo.sessionId;
    agentPaused = false; resumeInfo = null; clearTimeout(waitTimer);
    bResume.hidden = true;
    runAgent(g, h, s);
  });

  // ---------- 翻译 ----------
  // 先确认有可翻译的东西（输入框内容或附件里的截图），都没有就提示；
  // 否则在内容尾部追加一句「把上述内容翻译成 xx」，xx 用系统语言显示设置里的目标语言。
  bTrans.addEventListener("click", () =>
    busy(bTrans, "正在翻译中", async () => {
      const hasText = qbox.value.trim().length > 0;
      const hasFile = pendingAttachments.length > 0;
      if (!hasText && !hasFile) {
        idle("请输入内容或截图");
        return;
      }
      const langName = sysLangName(cfg.lang.code);
      const sentence = "把上述内容翻译成" + langName;
      let v = qbox.value;
      // 去掉已存在的同类提示（含旧语言），避免重复累加或语言不一致
      v = v.replace(/把上述内容翻译成[^\n]*\n?/g, "").replace(/\s+$/, "");
      v += (v.length ? "\n" : "") + sentence;
      qbox.value = v;
      syncHighlight();
      qbox.focus();
      await doAsk();   // 追加翻译提示后直接发送；结果回来后 busy 自动收起「正在翻译中」
    })
  );

  // ---------- 截图 ----------
  bShot.addEventListener("click", () =>
    busy(bShot, "正在截图…", async () => {
      const r = await send({ op: "shot", full_page: false, quality: 70 });
      // 不再在底部展示截图：直接作为附件加进对话框，避免重复显示。
      pendingAttachments.push({ name: "截图.png", dataUrl: r.data_url });
      renderAttachments();
      qbox.focus();
      idle(r.title || "已添加截图");
    })
  );

  // ---------- 框选截图（与 Ctrl+K 同一条通道）----------
  // 不想记快捷键的人点这个也行：直接走 startRegionShot，体验一致。
  bRegion.addEventListener("click", () => {
    void startRegionShot();
  });


  // ---------- 区域截图（Ctrl+K / 面板「框选」按钮）----------
  // 与「截图」按钮（整块可见区）并存：这条是**框选**通道，体验和市场截图工具一致。
  // 流程：captureVisibleTab 抓整块可见区 → 页面蒙版上框选 → 松手裁剪 →
  // 在面板里显示裁剪结果（默认已加入对话框，无需按钮）。

  // 框选期间临时隐藏浮球，记住原 display 值，确认/取消时恢复。
  let regionPrev = "";

  const showShotResult = (cropUrl) => {
    host.style.display = regionPrev;   // 框选结束，恢复浮球/面板
    // 选区松手后**默认直接把图加进对话框**（复用 pendingAttachments，与拖放/粘贴
    // 图片同一链路），无需任何按钮；底部不再展示截图，避免重复显示。
    pendingAttachments.push({ name: "截图.png", dataUrl: cropUrl });
    renderAttachments();
    qbox.focus();
    idle("图片已加入对话框");
    // 截完把面板亮出来，否则用户看不到结果
    panel.classList.add("k-open");
  };

  const showRegionSelector = (fullImg) => {
    // captureVisibleTab 抓的是「可见视口」，蒙版也只盖视口；坐标系直接用
    // clientX/Y，无需考虑页面滚动。缩放比把 CSS 像素换算成截图的设备像素。
    const scaleX = fullImg.naturalWidth / window.innerWidth;
    const scaleY = fullImg.naturalHeight / window.innerHeight;

    const ov = document.createElement("div");
    ov.className = "k-rshot";
    const hint = document.createElement("div");
    hint.className = "k-rhint";
    hint.textContent = "拖拽框选区域 · Esc 取消";
    const sel = document.createElement("div");
    sel.className = "k-rsel";
    ov.appendChild(sel);
    ov.appendChild(hint);
    // 蒙版挂到 documentElement，跟浮球 host 分开，免得受 host.display 牵连。
    document.documentElement.appendChild(ov);

    let x0 = 0, y0 = 0, dragging = false;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const cleanup = () => {
      ov.removeEventListener("mousedown", onDown);
      ov.removeEventListener("mousemove", onMove);
      ov.removeEventListener("mouseup", finish);
      document.removeEventListener("keydown", onKey);
    };
    const onDown = (e) => {
      e.preventDefault();
      dragging = true;
      x0 = e.clientX;
      y0 = e.clientY;
      sel.style.left = x0 + "px";
      sel.style.top = y0 + "px";
      sel.style.width = "0px";
      sel.style.height = "0px";
    };
    const onMove = (e) => {
      if (!dragging) return;
      const x1 = clamp(e.clientX, 0, window.innerWidth);
      const y1 = clamp(e.clientY, 0, window.innerHeight);
      const left = Math.min(x0, x1);
      const top = Math.min(y0, y1);
      sel.style.left = left + "px";
      sel.style.top = top + "px";
      sel.style.width = Math.abs(x1 - x0) + "px";
      sel.style.height = Math.abs(y1 - y0) + "px";
    };
    const finish = () => {
      if (!dragging) return;
      dragging = false;
      const r = sel.getBoundingClientRect();
      cleanup();
      const w = Math.round(r.width * scaleX);
      const h = Math.round(r.height * scaleY);
      if (w < 5 || h < 5) { ov.remove(); host.style.display = regionPrev; return; } // 没框选 = 取消
      const sx = Math.round(r.left * scaleX);
      const sy = Math.round(r.top * scaleY);
      const c = document.createElement("canvas");
      c.width = w;
      c.height = h;
      c.getContext("2d").drawImage(fullImg, sx, sy, w, h, 0, 0, w, h);
      ov.remove();
      showShotResult(c.toDataURL("image/png"));
    };
    const onKey = (e) => {
      if (e.key === "Escape") {
        dragging = false;
        cleanup();
        ov.remove();
        host.style.display = regionPrev;
      }
    };
    ov.addEventListener("mousedown", onDown);
    ov.addEventListener("mousemove", onMove);
    ov.addEventListener("mouseup", finish);
    document.addEventListener("keydown", onKey);
  };

  const startRegionShot = async () => {
    // 先抓整块可见区（此时浮球还可见）。关键点：不能先藏浮球再抓——
    // 实测 captureVisibleTab 在浮球被 display:none 之后抓不到图，full 会变成
    // undefined，于是蒙版永远不出现（控制台会打印「shot 返回 =」后面为空）。
    // 所以等拿到截图再藏浮球；而框选蒙版 z-index 顶到最高，本就盖在浮球之上，
    // 框选期间浮球看不见也点不到，藏不藏都不影响体验。
    let full;
    try {
      full = await send({ op: "shot", full_page: false, quality: 80 });
    } catch (e) {
      console.warn("[keeper] 区域截图：send 抛错", e);
      const d = document.createElement("div");
      d.textContent = "截图失败：" + explain(e);
      setOut(d);
      return;
    }
    // 诊断：把原始返回打出来，正常应为带 data_url 的对象；若后面为空说明 captureVisibleTab 没返回图。
    console.warn("[keeper] 区域截图：shot 返回 =", full);
    if (!full || !full.data_url) {
      const d = document.createElement("div");
      d.textContent =
        "截图返回为空（多半是扩展没重载到新版 background，或当前页受限）。原始返回：" +
        JSON.stringify(full);
      setOut(d);
      return;
    }
    // 抓到图了，再藏浮球，避免它出现在裁剪结果里、也避免它浮在蒙版之上。
    regionPrev = host.style.display;
    host.style.display = "none";
    const img = new Image();
    img.onload = () => {
      try {
        showRegionSelector(img);
      } catch (e) {
        host.style.display = regionPrev;
        console.warn("[keeper] 区域截图：showRegionSelector 抛错", e);
        const d = document.createElement("div");
        d.textContent = "框选界面出错：" + explain(e);
        setOut(d);
      }
    };
    img.onerror = () => {
      host.style.display = regionPrev;
      const d = document.createElement("div");
      d.textContent = "截图加载失败";
      setOut(d);
    };
    img.src = full.data_url;
  };

  $("x").addEventListener("click", () => {
    panel.classList.remove("k-open");
    aoFold = true; applyAo();   // 面板关闭时同步收起响应消息区
    // 收起即停：面板看不见了还每 4 秒问一次，等于白白把 service worker 吊着不睡
    stopLinkPoll();
  });

  // ---------- 设置 ----------
  // 配置持久化在扩展自身的 storage（不依赖内核）；后续「翻译」「网页总结」功能直接读这里。
  const cfgKey = "keeper_cfg";
  const DEFAULT_CFG = { lang: { code: "en", name: "English" }, minChars: 50, phone: "", maxSteps: 20, maxRetry: 3, retryDelay: 2 };
  const cfg = { lang: { ...DEFAULT_CFG.lang }, minChars: DEFAULT_CFG.minChars, phone: DEFAULT_CFG.phone, maxSteps: DEFAULT_CFG.maxSteps, maxRetry: DEFAULT_CFG.maxRetry, retryDelay: DEFAULT_CFG.retryDelay };
  const gear = $("gear"), setView = $("set"), back = $("bk");
  const langSel = $("lang"), mincInp = $("minc"), phoneInp = $("phone"), maxStepsInp = $("maxSteps"), maxRetryInp = $("maxRetry"), retryDelayInp = $("retryDelay");
  // 20 种常用语言：翻译功能会把它作为「翻译成 XX 语言」的目标语言。
  const LANGS = [
    ["zh-CN", "简体中文", "简体中文"], ["zh-TW", "繁體中文", "繁體中文"], ["en", "English", "英语"],
    ["ja", "日本語", "日语"], ["ko", "한국어", "韩语"], ["fr", "Français", "法语"],
    ["de", "Deutsch", "德语"], ["es", "Español", "西班牙语"], ["ru", "Русский", "俄语"],
    ["pt", "Português", "葡萄牙语"], ["it", "Italiano", "意大利语"], ["ar", "العربية", "阿拉伯语"],
    ["hi", "हिन्दी", "印地语"], ["th", "ไทย", "泰语"], ["vi", "Tiếng Việt", "越南语"],
    ["id", "Bahasa Indonesia", "印尼语"], ["tr", "Türkçe", "土耳其语"],
    ["nl", "Nederlands", "荷兰语"], ["pl", "Polski", "波兰语"], ["uk", "Українська", "乌克兰语"]
  ];
  // 翻译提示里写的目标语言名，用「系统语言」表达：系统中文→中文名（英语），系统英文→原名（English）。
  const sysLangName = (code) => {
    const hit = LANGS.find((l) => l[0] === code);
    if (!hit) return code;
    const sysZh = /^(zh|zh-CN|zh-TW|zh-Hans|zh-Hant)/i.test(navigator.language || "zh");
    return sysZh ? hit[2] : hit[1];
  };
  const saveCfg = () => { try { chrome.storage.local.set({ [cfgKey]: cfg }); } catch (e) {} };
  const loadCfg = (cb) => {
    try {
      chrome.storage.local.get(cfgKey, (o) => {
        const s = o && o[cfgKey];
        if (s) {
          if (s.lang && s.lang.code) cfg.lang = { code: s.lang.code, name: s.lang.name || s.lang.code };
          if (typeof s.minChars === "number") cfg.minChars = s.minChars;
          if (typeof s.phone === "string") cfg.phone = s.phone;
          if (typeof s.maxSteps === "number" && s.maxSteps > 0) cfg.maxSteps = s.maxSteps;
          if (typeof s.maxRetry === "number" && s.maxRetry >= 0) cfg.maxRetry = s.maxRetry;
          if (typeof s.retryDelay === "number" && s.retryDelay >= 0) cfg.retryDelay = s.retryDelay;
        }
        cb && cb();
      });
    } catch (e) { cb && cb(); }
  };
  LANGS.forEach(([code, name]) => {
    const op = document.createElement("option");
    op.value = code; op.textContent = name;
    langSel.appendChild(op);
  });
  const applyCfgUi = () => { langSel.value = cfg.lang.code; mincInp.value = cfg.minChars; phoneInp.value = cfg.phone; maxStepsInp.value = cfg.maxSteps; maxRetryInp.value = cfg.maxRetry; retryDelayInp.value = cfg.retryDelay; };
  loadCfg(applyCfgUi);
  gear.addEventListener("click", () => { setView.hidden = false; });
  back.addEventListener("click", () => { setView.hidden = true; });
  langSel.addEventListener("change", () => {
    const hit = LANGS.find((l) => l[0] === langSel.value);
    if (hit) { cfg.lang = { code: hit[0], name: hit[1] }; saveCfg(); }
  });
  mincInp.addEventListener("change", () => {
    const v = parseInt(mincInp.value, 10);
    cfg.minChars = (isNaN(v) || v < 0) ? 0 : v;
    saveCfg();
  });
  phoneInp.addEventListener("change", () => {
    cfg.phone = (phoneInp.value || "").trim();
    saveCfg();
  });
  maxStepsInp.addEventListener("change", () => {
    const v = parseInt(maxStepsInp.value, 10);
    cfg.maxSteps = (isNaN(v) || v < 1) ? 1 : v;
    maxStepsInp.value = cfg.maxSteps;
    saveCfg();
  });
  maxRetryInp.addEventListener("change", () => {
    const v = parseInt(maxRetryInp.value, 10);
    cfg.maxRetry = (isNaN(v) || v < 0) ? 0 : v;
    maxRetryInp.value = cfg.maxRetry;
    saveCfg();
  });
  retryDelayInp.addEventListener("change", () => {
    const v = parseInt(retryDelayInp.value, 10);
    cfg.retryDelay = (isNaN(v) || v < 0) ? 0 : v;
    retryDelayInp.value = cfg.retryDelay;
    saveCfg();
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
      setView.hidden = true;   // 每次打开面板重置设置视图
      refreshLink();
      if (!linkTimer) linkTimer = setInterval(refreshLink, 4000);
      /* 会话列表也在这里拉：它在浏览器页那一侧随时会变（新建、删除、改名），
         面板关着的时候拉纯属浪费一个内核往返。顺带把上次选的那条恢复出来。 */
      loadSessions().then(fetchLatest);
    } else {
      stopLinkPoll();
      aoFold = true; applyAo();   // 收起时同步收起响应消息区
      // 面板看不见了，"还在跑"的轮询也就没有意义——继续问只是在打扰内核
      if (latestTimer) { clearTimeout(latestTimer); latestTimer = null; }
    }
  });

  // 点击面板（浮球）之外的页面区域，关闭面板。浮球与面板都挂在 shadow host 内，
  // 普通 host.contains 对 shadow 内部点击会失效，所以用 composedPath 判断点击
  // 是否落在 host 里：落在里面（不管面板还是浮球）都不关，落在外面才关。
  document.addEventListener("click", (e) => {
    if (!panel.classList.contains("k-open")) return;
    if (e.composedPath().includes(host)) return;
    // 正在/刚做完文本选区时不关面板：拖选结束会冒出一个 click，若直接关会让
    // 「在页面上选一段文字」的体验变成面板凭空消失。有非空选区就放过这次点击。
    const sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed && sel.toString().trim()) return;
    panel.classList.remove("k-open");
    aoFold = true; applyAo();   // 同步收起响应消息区
    stopLinkPoll();
  });

  // 这里**刻意没有** pagehide 时把 host 删掉：
  //   * 正常导航：旧文档整体销毁，球跟着一起没了，不需要手动删；
  //   * 后退键（bfcache）：文档连 DOM 一起从缓存里回来，而 content script 不会
  //     再跑第二次 —— 主动删掉等于球从此再也不出现。
  // 不删反而是「零重建」的那条路：后退回来时球原地还在，一个像素都不用动。
  }   // buildUI
})();
