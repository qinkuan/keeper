/**
 * 浏览器工作台（对话页右侧区域，按需打开）。
 *
 * **两套实现，同一个界面**
 * -----------------------------------
 * - **桌面壳（Electron）**：主进程挂一个原生子视图 WebContentsView 来显示网页，
 *   本组件只画地址栏 + 一个**占位盒子**（原生视图会精确盖在它上面）。
 *   这是主力路径：百度、Google、GitHub 都能正常打开。
 * - **Web 形态**：降级为 iframe。这条路会被服务端 X-Frame-Options / CSP
 *   frame-ancestors 拒绝——这三个站点全都禁止被嵌入，表现为一整块空白。所以
 *   Web 形态下不提供「抓取」：抓取要经 CDP 连桌面壳那个浏览器，Web 形态压根
 *   没有那个端口。
 *
 * 判断依据是 window.keeperBrowser（preload 注入，见 desktop/preload.js）：
 * 有就用原生视图，没有就走 iframe。对用户完全透明。
 *
 * 抓取（文字 / 截图）走内核接口：内核用 Playwright 经 CDP **连接桌面壳正在跑的
 * 这个浏览器**，所以拿到的是用户眼前这一页，连登录态都是同一份。见
 * keeper/browser/cdp.py。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Camera,
  Download,
  ExternalLink,
  Globe,
  Loader2,
  RotateCw,
  ScanText,
  X,
} from "lucide-react";
import { cn } from "../lib/utils";
import {
  apiBrowserScreenshot,
  apiBrowserStatus,
  apiBrowserText,
} from "../api/client";

/** 空态里的快捷入口：省得每次都手打一遍。 */
const QUICK = [
  { label: "百度", url: "https://www.baidu.com" },
  { label: "GitHub", url: "https://github.com" },
  { label: "MDN", url: "https://developer.mozilla.org" },
  { label: "本机开发", url: "http://localhost:5273" },
];

/**
 * 补全协议。用户只输入 example.com 时直接塞给浏览器会被当成相对路径，
 * 页面直接白掉——这类"点了没反应"最让人困惑，所以统一补 https://。
 */
function normalize(raw: string): string {
  const s = raw.trim();
  if (!s) return "";
  // 已有 scheme（http/https/about/data...）原样放行
  if (/^[a-z][a-z0-9+.-]*:/i.test(s)) return s;
  return `https://${s}`;
}

/** 桌面壳注入的桥；Web 形态为 null */
const bridge = typeof window === "undefined" ? null : window.keeperBrowser;

export type CaptureResult =
  | {
      kind: "text";
      url: string;
      title: string;
      text: string;
      truncated: boolean;
      full_chars: number;
    }
  | {
      kind: "image";
      url: string;
      title: string;
      mime: string;
      dataUrl: string;
      bytes: number;
    };

export default function BrowserPanel({
  className,
  /** 页面上有浮层盖上来时（会话抽屉）把原生视图藏起来：原生视图永远在最上层 */
  overlayOpen = false,
}: {
  className?: string;
  overlayOpen?: boolean;
}) {
  return bridge ? (
    <NativeBrowser bridge={bridge} className={className} overlayOpen={overlayOpen} />
  ) : (
    <IframeBrowser className={className} />
  );
}

// ============================================================================
// 原生视图轨（桌面壳）
// ============================================================================

function NativeBrowser({
  bridge: b,
  className,
  overlayOpen,
}: {
  bridge: NonNullable<typeof window.keeperBrowser>;
  className?: string;
  overlayOpen: boolean;
}) {
  const [input, setInput] = useState("");
  const [state, setState] = useState({
    url: "",
    title: "",
    loading: false,
    canGoBack: false,
    canGoForward: false,
  });
  const [err, setErr] = useState("");
  /** 抓取（文字 / 截图）状态。放在 NativeBrowser 里是因为触发按钮与结果面板分居
      面板上下两处（**都必须在原生视图之外**才点得动），状态得同一个组件持有 */
  const [capBusy, setCapBusy] = useState<"" | "text" | "shot">("");
  const [capResult, setCapResult] = useState<CaptureResult | null>(null);
  const [capErr, setCapErr] = useState("");
  const [capOk, setCapOk] = useState<boolean | null>(null);

  /** 占位盒子：原生视图就盖在它上面，位置必须精确同步给主进程 */
  const boxRef = useRef<HTMLDivElement>(null);

  /** 把占位盒子的矩形发给主进程（原生视图的坐标原点是窗口内容区） */
  const syncBounds = useCallback(() => {
    const el = boxRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // 太窄太矮时先别发：折叠/隐藏过程中的中间态会让原生视图闪成一条缝
    if (r.width < 16 || r.height < 16) return;
    b.setBounds({ x: r.left, y: r.top, width: r.width, height: r.height });
  }, [b]);

  useEffect(() => {
    // 每次都发 open（主进程侧是幂等的：视图已在就不重建，只同步位置与可见性）。
    // 不用"只 open 一次"的标记，是因为 StrictMode 下 effect 会 mount→cleanup→mount
    // 走两遍：标记会让第二次 mount 以为"已经开过了"，而第一次的 cleanup 已经把
    // 视图销毁——结果面板一片空白。
    // 不带 url：只把视图挂出来，等用户在地址栏输入。
    b.open("", { x: 0, y: 0, width: 0, height: 0 });

    // 布局变化（面板拖宽、窗口 resize、滚动）都要跟上，否则视图会停在旧位置
    const ro = new ResizeObserver(syncBounds);
    if (boxRef.current) ro.observe(boxRef.current);
    window.addEventListener("resize", syncBounds);
    window.addEventListener("scroll", syncBounds, true);
    syncBounds();
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", syncBounds);
      window.removeEventListener("scroll", syncBounds, true);
    };
  }, [b, syncBounds]);

  // 面板被卸载（工作台切到编码 / 关闭）必须销毁原生视图：留着它会继续跑
  // 自己的定时器和视频，白耗 CPU，而且它还浮在页面上挡住别的东西。
  // 与上面的 open 配对：StrictMode 会 cleanup 一遍再重跑一遍 open，所以最终
  // 仍是打开状态；真卸载时则只剩 close。
  useEffect(() => () => b.close(), [b]);

  // 浮层盖上来时藏视图。原生视图不在 DOM 里，CSS 的 z-index 对它无效——
  // 不显式藏起来，会话抽屉会被这个网页整个压在下面。
  useEffect(() => {
    b.setVisible(!overlayOpen);
  }, [b, overlayOpen]);

  // 状态订阅之外再主动拉一次当前地址：视图是在挂载那刻创建的，头几条状态事件
  // 很可能早于这次订阅（StrictMode 下还会先建后拆一次），只靠事件的话
  // state.url 会一直空着 —— 页面明明开着，抓取按钮却一直是灰的。
  useEffect(() => {
    let alive = true;
    b.currentUrl()
      .then((u) => {
        if (alive && u) setState((s) => (s.url ? s : { ...s, url: u }));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [b]);

  useEffect(() => {
    const offState = b.onState((s) => {
      setState(s);
      setErr("");
    });
    const offErr = b.onError((e) => {
      setErr(`${e.description || "加载失败"}${e.url ? `（${e.url}）` : ""}`);
    });
    return () => {
      offState();
      offErr();
    };
  }, [b]);

  const go = (raw: string) => {
    const u = normalize(raw);
    if (!u) return;
    setInput(u);
    b.command("load", u);
  };

  /** 内核是否支持抓取。不可用（Web 形态 / 没装 playwright / 端口没监听）就整套隐藏，
      别让用户点一下吃一个错误 */
  useEffect(() => {
    let alive = true;
    apiBrowserStatus()
      .then((s) => alive && setCapOk(s.available))
      .catch(() => alive && setCapOk(false));
    return () => {
      alive = false;
    };
  }, []);

  /** 抓当前这一页。
   *
   * 点击时若 state.url 还是空的，**先向主进程现问一次**：状态事件有可能全部错过
   * （视图在页面订阅之前就创建好了），此时页面上明明开着网页，按钮却像坏了一样。
   */
  const runCapture = async (kind: "text" | "shot") => {
    setCapBusy(kind);
    setCapErr("");
    try {
      let url = state.url;
      if (!url) {
        url = await b.currentUrl().catch(() => "");
        if (url) setState((s) => (s.url ? s : { ...s, url }));
      }
      if (!url) {
        setCapErr("还没打开网页：先在上方地址栏输入网址");
        return;
      }
      if (kind === "text") {
        const r = await apiBrowserText();
        setCapResult({ kind: "text", ...r });
      } else {
        const r = await apiBrowserScreenshot();
        setCapResult({
          kind: "image",
          url: r.url,
          title: r.title,
          mime: r.mime,
          dataUrl: `data:${r.mime};base64,${r.image_base64}`,
          bytes: r.bytes,
        });
      }
    } catch (e) {
      setCapErr(e instanceof Error ? e.message : String(e));
    } finally {
      setCapBusy("");
    }
  };

  const btn =
    "flex h-7 w-7 shrink-0 items-center justify-center rounded-[7px] text-ink-muted transition-colors duration-150 hover:bg-surface hover:text-ink disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-transparent";

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-panel", className)}>
      {/* 地址栏 */}
      <div className="flex shrink-0 items-center gap-1 border-b border-line-soft px-1.5 py-1.5">
        <button
          type="button"
          className={btn}
          onClick={() => b.command("back")}
          disabled={!state.canGoBack}
          title="后退"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          className={btn}
          onClick={() => b.command("forward")}
          disabled={!state.canGoForward}
          title="前进"
        >
          <ArrowRight className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          className={btn}
          onClick={() => b.command(state.loading ? "stop" : "reload")}
          title={state.loading ? "停止" : "重新加载"}
        >
          {state.loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RotateCw className="h-3.5 w-3.5" />
          )}
        </button>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            go(input);
          }}
          className="flex h-7 min-w-0 flex-1 items-center gap-1.5 rounded-[8px] border border-line-soft bg-surface/60 px-2 focus-within:border-brand/40"
        >
          <Globe className="h-3 w-3 shrink-0 text-ink-faint" />
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onFocus={(e) => e.currentTarget.select()}
            placeholder="输入网址，如 baidu.com"
            spellCheck={false}
            autoComplete="off"
            className="min-w-0 flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-faint"
          />
        </form>

        <button
          type="button"
          className={btn}
          disabled={!state.url}
          onClick={() => b.command("external", state.url)}
          title="在新标签页 / 系统浏览器打开"
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </button>

        {/*
          抓取按钮放在**地址栏这一行**，不能放进下面的网页区：原生视图是浮在所有
          DOM 之上的原生层，盒子范围内的一切（按钮、结果面板）都会被网页整个盖住
          —— 看得见、点不动。第一版就是这么废掉的。
        */}
        {capOk === true && (
          <div className="flex shrink-0 items-center gap-1 pl-1">
            <button
              type="button"
              onClick={() => runCapture("text")}
              disabled={!!capBusy}
              title="把这一页的文字抓下来（可直接喂给模型）"
              className="flex h-7 items-center gap-1 rounded-[7px] bg-brand-gradient px-2 text-[11.5px] font-medium text-white shadow-brand transition-all duration-150 hover:brightness-110 active:scale-95 disabled:opacity-50"
            >
              {capBusy === "text" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <ScanText className="h-3 w-3" />
              )}
              抓文字
            </button>
            <button
              type="button"
              onClick={() => runCapture("shot")}
              disabled={!!capBusy}
              title="抓这一页的截图"
              className="flex h-7 items-center gap-1 rounded-[7px] border border-line-soft px-2 text-[11.5px] font-medium text-ink-muted transition-colors hover:border-brand/40 hover:text-brand disabled:opacity-50"
            >
              {capBusy === "shot" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Camera className="h-3 w-3" />
              )}
              截图
            </button>
          </div>
        )}
      </div>

      {/* 网页区：占位盒子 + 原生视图精确盖在它上面 */}
      <div className="relative min-h-0 flex-1">
        <div ref={boxRef} className="absolute inset-0" />
        {!state.url && !err && <QuickStart onPick={go} />}
        {err && (
          <div className="pointer-events-none absolute inset-x-0 top-3 z-10 mx-auto w-fit max-w-[90%] rounded-[8px] bg-amber-500/15 px-2.5 py-1 text-[11.5px] text-amber-700 dark:text-amber-300">
            {err}
          </div>
        )}
      </div>

      {/* 抓取结果：必须放在网页区**外面**（同理会被原生视图盖住）。
          出现时占掉下方一块高度，盒子随之变矮 —— ResizeObserver 会把新的 bounds
          自动同步给主进程，网页区域跟着缩，不会被压住。 */}
      {(capResult || capErr) && (
        <div className="flex max-h-[42%] min-h-0 shrink-0 flex-col border-t border-line-soft bg-panel">
          <div className="flex shrink-0 items-center gap-2 border-b border-line-soft px-2.5 py-1.5">
            <span className="min-w-0 flex-1 truncate text-[11px] text-ink-faint">
              {capResult ? capResult.title || capResult.url : "抓取失败"}
            </span>
            {capResult?.kind === "image" && (
              <a
                href={capResult.dataUrl}
                download={`${(capResult.title || "page").replace(/\W+/g, "_")}.jpg`}
                className="flex h-6 items-center gap-1 rounded-[7px] border border-line-soft px-2 text-[11px] text-ink-muted transition-colors hover:border-brand/40 hover:text-brand"
              >
                <Download className="h-3 w-3" />
                存为
              </a>
            )}
            <button
              type="button"
              onClick={() => {
                setCapResult(null);
                setCapErr("");
              }}
              title="关闭"
              className="flex h-6 w-6 items-center justify-center rounded-[7px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="min-h-0 flex-1 overflow-auto">
            {capErr ? (
              <p className="px-2.5 py-2 text-[11.5px] leading-relaxed text-amber-700 dark:text-amber-300">
                {capErr}
              </p>
            ) : capResult?.kind === "text" ? (
              <>
                <pre className="whitespace-pre-wrap break-words px-2.5 py-2 text-[11.5px] leading-relaxed text-ink-muted">
                  {capResult.text ||
                    "（这一页没有可提取的文字，可能是纯图片或还在加载）"}
                </pre>
                {capResult.truncated && (
                  <p className="px-2.5 pb-2 text-[11px] text-ink-faint">
                    已截断：这一页共 {capResult.full_chars} 字，只取前若干字。
                  </p>
                )}
              </>
            ) : capResult?.kind === "image" ? (
              <img
                src={capResult.dataUrl}
                alt={capResult.title || "页面截图"}
                className="block w-full"
              />
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}

/** 空态：告诉用户"这里能输网址"，而不是留一块白板让人猜 */
function QuickStart({ onPick }: { onPick: (u: string) => void }) {
  return (
    <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
      <Globe className="h-7 w-7 text-ink-faint" />
      <p className="text-[12.5px] text-ink-muted">在上方输入网址即可打开网页</p>
      <div className="pointer-events-auto flex flex-wrap items-center justify-center gap-1.5">
        {QUICK.map((q) => (
          <button
            key={q.url}
            type="button"
            onClick={() => onPick(q.url)}
            className="rounded-full border border-line-soft px-2.5 py-1 text-[11.5px] text-ink-muted transition-colors hover:border-brand/40 hover:text-brand"
          >
            {q.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ============================================================================
// iframe 轨（Web 形态降级）
// ============================================================================

function IframeBrowser({ className }: { className?: string }) {
  const [input, setInput] = useState("");
  const [url, setUrl] = useState("");
  const [hist, setHist] = useState<string[]>([]);
  const [idx, setIdx] = useState(-1);
  // 自增即整块 iframe 重挂载 → 重新发起请求（reload）
  const [nonce, setNonce] = useState(0);
  const [loading, setLoading] = useState(false);

  /** 跳转：压掉"前进"分支后把新地址入栈 */
  const go = (raw: string) => {
    const u = normalize(raw);
    if (!u) return;
    setInput(u);
    setHist((h) => [...h.slice(0, idx + 1), u]);
    setIdx((i) => i + 1);
    setUrl(u);
    setLoading(true);
  };

  /** 回退 / 前进：只移动游标，不改栈 */
  const step = (d: -1 | 1) => {
    const n = idx + d;
    if (n < 0 || n >= hist.length) return;
    setIdx(n);
    setUrl(hist[n]);
    setInput(hist[n]);
    setLoading(true);
  };

  const btn =
    "flex h-7 w-7 shrink-0 items-center justify-center rounded-[7px] text-ink-muted transition-colors duration-150 hover:bg-surface hover:text-ink disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-transparent";

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-panel", className)}>
      <div className="flex shrink-0 items-center gap-1 border-b border-line-soft px-1.5 py-1.5">
        <button
          type="button"
          className={btn}
          onClick={() => step(-1)}
          disabled={idx <= 0}
          title="后退"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          className={btn}
          onClick={() => step(1)}
          disabled={idx < 0 || idx >= hist.length - 1}
          title="前进"
        >
          <ArrowRight className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          className={btn}
          onClick={() => {
            if (!url) return;
            setNonce((n) => n + 1);
            setLoading(true);
          }}
          disabled={!url}
          title="重新加载"
        >
          <RotateCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            go(input);
          }}
          className="flex h-7 min-w-0 flex-1 items-center gap-1.5 rounded-[8px] border border-line-soft bg-surface/60 px-2 focus-within:border-brand/40"
        >
          <Globe className="h-3 w-3 shrink-0 text-ink-faint" />
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onFocus={(e) => e.currentTarget.select()}
            placeholder="输入网址，如 example.com"
            spellCheck={false}
            autoComplete="off"
            className="min-w-0 flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-faint"
          />
        </form>

        <button
          type="button"
          className={btn}
          disabled={!url}
          onClick={() => url && window.open(url, "_blank", "noopener")}
          title="在新标签页打开（站点禁止嵌入时用这个）"
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="relative min-h-0 flex-1">
        {url ? (
          <>
            {loading && (
              <div className="absolute inset-0 z-10 flex items-start justify-center bg-panel/60 pt-16 text-[12px] text-ink-muted">
                加载中…
              </div>
            )}
            <iframe
              /* 换地址或刷新都靠 key 重挂载：不这么做，改 src 相同值时浏览器会
                 直接拿缓存，连"刷新"都点不动 */
              key={`${url}#${nonce}`}
              src={url}
              title="浏览器"
              onLoad={() => setLoading(false)}
              referrerPolicy="no-referrer"
              className="h-full w-full border-0 bg-white"
            />
          </>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
            <Globe className="h-7 w-7 text-ink-faint" />
            <p className="text-[12.5px] text-ink-muted">
              在上方输入网址即可打开网页
            </p>
            <div className="flex flex-wrap items-center justify-center gap-1.5">
              {QUICK.map((q) => (
                <button
                  key={q.url}
                  type="button"
                  onClick={() => go(q.url)}
                  className="rounded-full border border-line-soft px-2.5 py-1 text-[11.5px] text-ink-muted transition-colors hover:border-brand/40 hover:text-brand"
                >
                  {q.label}
                </button>
              ))}
            </div>
            <p className="max-w-[30rem] text-[11px] leading-relaxed text-ink-faint">
              当前是 Web 形态，只能内嵌打开：百度、Google、GitHub 等站点禁止被嵌入
              （frame-ancestors），在这里会显示空白。桌面版走原生浏览器，没有这个限制。
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
