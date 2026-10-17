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
  const BUILD = "1028b";

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
    // ---- 对这一页提问 ----
    /* 分区标题：两块能力（提问 / 抓取）用途完全不同，混在一排按钮里会让人以为
       「发送」和「抓文字」是同一件事的两步。 */
    ".k-sec{padding:9px 10px 4px;font-size:10.5px;color:#80868b;flex:0 0 auto}",
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
    /* 占位文案就是这一节的用途本身，所以上面不再挂一个「对这一页提问」的小节
       标题——同一句话在一屏里出现两次，读起来像出了什么重复渲染。 */
    /* 会话横条放在**输入框上面**：它决定"这轮记到哪条会话"，是发消息前就该看见
       的事实，压在输入框下面（原来那个 <select> 的位置）会让人发完才知道记哪了。 */
    '  <div class="k-tabs" id="pick" title="这条对话记到哪条会话"></div>',
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
    '  <div class="k-askmeta">',
    '    <span class="k-sel" id="sel">未选中文字</span>',
    "  </div>",
    '  <div class="k-askout" id="ao" hidden></div>',
    '  <div class="k-sec">抓取</div>',
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
  const bText = $("bText"), bShot = $("bShot"), bEval = $("bEval"), bCopy = $("bCopy");
  const ldot = $("ld"), lurl = $("lu"), lbtn = $("lb");
  const qbox = $("q"), goBtn = $("go"), pickBar = $("pick"),
        selTip = $("sel"), askOut = $("ao"), qhl = $("qhl");
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

  const paintSelection = () => {
    const s = currentSelection();
    const n = s.length;
    selTip.textContent = n
      ? "已选中 " + n + " 字" + (n >= SEL_LIMIT ? "（已截断）" : "")
      : "未选中文字 · agent 看不到本页";
    selTip.className = "k-sel" + (n ? " k-has" : "");
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

  const askAgent = async (q, selection = "") => {
    /* 新对话态：session_id 传空 + new_session=true，让内核**新建**一条。
       只传空是不够的——空在 _resolve_ball_target 里的意思是「用浮球默认会话」，
       那样点了新对话却记进上一条。（NEW_SESS 是内部哨兵，绝不能原样发出去。） */
    const fresh = isNewSess();
    const r = await chrome.runtime.sendMessage({
      type: "keeper:request",
      payload: {
        cid: ensureCid() || "",
        cmd: "agent:ask",
        q,
        session_id: fresh ? "" : chosenSess,
        new_session: fresh,
        page: {
          url: location.href,
          title: document.title,
          /* selection 完全由调用方给出（见 doAsk）。
             只有**没点过「添加」**时才带当前选区：引用一旦进了输入框，它就已经是
             问题的一部分了，再在这里带一份，agent 会在同一条消息里看到同一段文字
             出现两次——一次在「页面选中内容」，一次在「问题」。 */
          selection: selection || "",
        },
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
    if (!q) { setAskOut("先写点什么。", "k-wait"); return; }
    asking = true;
    goBtn.disabled = true;
    qbox.disabled = true;
    // 立刻给反馈，不等内核：这一轮可能跑很久，没有这句用户会以为按钮坏了
    setAskOut("正在思考…", "k-wait");
    mark("ask", "waiting");
    try {
      /* 引用已经展开进 q 了，这里就**不再**带选区：否则同一段文字会出现两次。
         没点过「添加」时才把当前选区带上（浮球一直有的行为）。 */
      const res = await askAgent(q, hasQuote ? "" : currentSelection());
      setAskOut(res && res.text ? res.text : "（没有返回内容）");
      // 置空而不是记下 id：这条已经被本地消费过，下次开面板补取时不必重画
      shownMsgId = "";
      mark("ask", "done");
      qbox.value = "";         // 只留最新一条，问完清空方便连续问
      /* 引用跟着清：它属于这一轮提问，留在那里下一轮会被误带上。
         序号一并归零，下一条又从「引用1」开始——否则用户会看到「引用7」这种
         莫名其妙的编号。（失败时不清，留着让用户直接重试。） */
      quotedList = [];
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
    if (!msg || msg.type !== "keeper:note" || !msg.note) return;
    const n = msg.note;
    if (n.event === "accepted") {
      mark("askAccepted", n.session_id || "");
      if (n.session_id && n.session_id !== chosenSess) loadSessions();
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
      return;
    }
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
      /* 会话列表也在这里拉：它在浏览器页那一侧随时会变（新建、删除、改名），
         面板关着的时候拉纯属浪费一个内核往返。顺带把上次选的那条恢复出来。 */
      loadSessions().then(fetchLatest);
    } else {
      stopLinkPoll();
      // 面板看不见了，"还在跑"的轮询也就没有意义——继续问只是在打扰内核
      if (latestTimer) { clearTimeout(latestTimer); latestTimer = null; }
    }
  });

  // 这里**刻意没有** pagehide 时把 host 删掉：
  //   * 正常导航：旧文档整体销毁，球跟着一起没了，不需要手动删；
  //   * 后退键（bfcache）：文档连 DOM 一起从缓存里回来，而 content script 不会
  //     再跑第二次 —— 主动删掉等于球从此再也不出现。
  // 不删反而是「零重建」的那条路：后退回来时球原地还在，一个像素都不用动。
  }   // buildUI
})();
