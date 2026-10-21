import { memo, useEffect, useState } from "react";
import { toast } from "./ui/Toast";
import { Check, Copy } from "lucide-react";

/** 代码块右上角的小按钮：跟着代码块的浅色底走（原来写的 hover:bg-white/10 是
    上一版深色代码块的残留，放在浅底上等于没有 hover 反馈） */
const BTN =
  "inline-flex h-6 items-center gap-1 rounded-[6px] px-1.5 text-[11.5px] font-medium text-ink-muted transition-all duration-150 hover:bg-black/[0.06] hover:text-ink-strong";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remend from "remend";
import type { Components } from "react-markdown";
import { MermaidView, ChartView } from "./BlockView";

/** 只预加载常用语言，控制 Shiki 体积（全量语法包有几十 MB） */
const LANGS = [
  "python",
  "javascript",
  "typescript",
  "tsx",
  "jsx",
  "json",
  "bash",
  "html",
  "css",
  "markdown",
  "sql",
  "yaml",
  "java",
  "go",
  "rust",
  "diff",
  "xml",
  "ini",
];
const THEME = "github-light";

/**
 * Shiki 高亮结果缓存：key = 归一语言 + 代码文本。
 *
 * 同一段代码在一次会话里会被反复渲染（**输入一个字就会重渲染整段历史消息**），
 * 不缓存的话每次都要重跑一遍 WASM 高亮——长会话里这是最主要的卡顿来源。
 *
 * 上限 300 条，超了整批清空：宁可丢缓存，也不要让内存无限涨。
 */
const hlCache = new Map<string, string>();
const HL_CACHE_MAX = 300;

let hlPromise: Promise<any> | null = null;

/** 动态 import：Shiki 单独成 chunk，不拖慢首屏 */
function getHighlighter() {
  if (!hlPromise) {
    hlPromise = import("shiki").then(({ createHighlighter }) =>
      createHighlighter({ themes: [THEME], langs: LANGS })
    );
  }
  return hlPromise;
}

/** 代码块：Shiki 高亮 + 语言标签 + 一键复制 */
function CodeBlock({ lang, text }: { lang?: string; text: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    // 不在预加载列表里的语言降级为纯文本，避免 Shiki 抛错
    const l = lang && LANGS.includes(lang) ? lang : "text";
    const ck = `${l} ${text}`;
    const cached = hlCache.get(ck);
    if (cached !== undefined) {
      setHtml(cached);
      return;
    }
    getHighlighter()
      .then((hl) => {
        if (!alive) return;
        try {
          const out = hl.codeToHtml(text, { lang: l, theme: THEME });
          if (hlCache.size >= HL_CACHE_MAX) hlCache.clear();
          hlCache.set(ck, out);
          setHtml(out);
        } catch {
          setHtml(null);
        }
      })
      .catch(() => setHtml(null));
    return () => {
      alive = false;
    };
  }, [lang, text]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <div className="kp-md-code">
      <div className="kp-md-code-bar">
        <span>{lang || "code"}</span>
        <button type="button" onClick={copy} className={BTN}>
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      {html ? (
        // Shiki 生成的 HTML 已对代码内容转义，可安全插入
        <div dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        // 高亮未就绪 / 语言不支持时的降级
        <pre>
          <code>{text}</code>
        </pre>
      )}
    </div>
  );
}

const components: Components = {
  code({ className, children, node, ...props }) {
    const m = /language-([\w+-]+)/.exec(className || "");
    const text = String(children ?? "").replace(/\n$/, "");
    if (m) {
      const lang = m[1];
      // markdown 内联渲染：```mermaid / ```vega-lite / ```echarts 直接渲染成图，
      // 不再当普通代码高亮。与制品卡片共用 MermaidView / ChartView（吃源码字符串）。
      if (lang === "mermaid") return <MermaidView src={text} />;
      if (lang === "echarts") return <ChartView src={text} engine="echarts" />;
      if (lang === "vega-lite" || lang === "chart")
        return <ChartView src={text} engine="vega-lite" />;
      return <CodeBlock lang={lang} text={text} />;
    }
    return (
      <code className="kp-md-inline-code" {...props}>
        {children}
      </code>
    );
  },
  a({ children, href, node, ...props }) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    );
  },
};

/**
 * Markdown 渲染（助手回复 / 工作台 .md 预览共用）。
 *
 * 组合：react-markdown（渲染）+ remark-gfm（表格等）+ remend（流式补全）
 * + Shiki（代码高亮）。
 *
 * remend 是关键：流式输出时文本是**残缺**的（未闭合的 `**`、反引号、`[`](` 等），
 * 直接给 react-markdown 解析会在闭合瞬间跳变闪烁。remend 先把残缺标记补全，
 * 消除闪烁——Streamdown 内部用的也是它。
 */
function MarkdownView({
  text,
  mode = "streaming",
}: {
  text: string;
  /** static = 内容已完整（工作台文件预览），跳过 remend 补全 */
  mode?: "streaming" | "static";
}) {
  const md = mode === "streaming" ? remend(text) : text;
  return (
    <div className="kp-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {md}
      </ReactMarkdown>
    </div>
  );
}

// memo：输入框每敲一个字都会让 ChatPage 重渲染，历史消息的 markdown 不该跟着
// 重新解析一遍（remend + react-markdown + Shiki 是这条链上最贵的一段）。
// props 只有 text / mode 两个原始值，memo 不会出现「因为新引用而失效」。
export default memo(MarkdownView);
