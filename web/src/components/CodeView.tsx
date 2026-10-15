import { useEffect, useRef, useState } from "react";
import { Download, Eye, EyeOff, Loader2, Save } from "lucide-react";
import { Pill } from "./ui/Pill";
import { toast } from "./ui/Toast";
import { cn } from "../lib/utils";

/** 卡片头部的小按钮（与 ArtifactCard 保持同一套观感） */
const BTN =
  "inline-flex h-6 items-center gap-1 rounded-md border border-line px-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50";
const ICON_BTN =
  "flex h-6 w-6 items-center justify-center rounded-md border border-line text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50";

/** 空态 / 降级态：居中一竖列说明 + 一个动作 */
function Fallback({
  title,
  desc,
  action,
}: {
  title: string;
  desc?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
      <div className="text-[13px] text-ink">{title}</div>
      {desc && <div className="text-[12px] text-ink-muted">{desc}</div>}
      {action}
    </div>
  );
}
import Editor, { DiffEditor } from "@monaco-editor/react";
import { apiWorkspaceFile, apiSaveWorkspaceFile, workspaceFileRawUrl } from "../api/client";
// monaco 的本地化配置（workers + loader.config）必须在使用前执行一次。
// 放在这里而不是入口：它静态 import 了 monaco-editor（3.9MB），跟着本组件
// 的 lazy import 一起加载，不开工作台就不下载。
import "../monaco";
import { monacoLangOf } from "../monacoLang";
import MarkdownView from "./MarkdownView";
import {
  MAX_EDIT_BYTES,
  isEditableKind,
  isMonacoKind,
  looksBinary,
  opensInNewTab,
  viewerKindOf,
} from "./viewers";
import type { ViewerKind } from "./viewers";

interface CodeViewProps {
  agentId: string;
  sessionId?: string;
  /** 当前打开的文件（相对工作空间根），null 表示未选 */
  path: string | null;
  /** 工作空间是否只读（决定能否编辑/保存） */
  readOnly: boolean;
  /** 保存成功后通知外层（刷新文件浏览器 git 角标） */
  onSaved?: () => void;
}

const MONACO_OPTIONS = {
  minimap: { enabled: false },
  fontSize: 13,
  scrollBeyondLastLine: false,
  wordWrap: "on" as const,
  automaticLayout: true,
};

function gitTag(status: string): React.ReactNode {
  switch (status) {
    case "ok":
      return <Pill tone="orange">相对 HEAD 有改动</Pill>;
    case "untracked_new":
      return <Pill tone="blue">未跟踪（新增）</Pill>;
    case "no_changes":
      return <Pill tone="muted">无改动</Pill>;
    case "not_git":
      return <Pill tone="muted">不在 git</Pill>;
    case "missing":
      return <Pill tone="red">文件缺失</Pill>;
    default:
      return null;
  }
}

export default function CodeView({
  agentId,
  sessionId,
  path,
  readOnly,
  onSaved,
}: CodeViewProps) {
  const [text, setText] = useState("");
  const [original, setOriginal] = useState("");
  const [gitStatus, setGitStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [showDiff, setShowDiff] = useState(false);
  /** .md 默认渲染预览；要改原文就点「源码」切回 Monaco */
  const [showPreview, setShowPreview] = useState(true);

  // 用 ref 持有最新内容与保存函数，避免 Monaco 命令闭包捕获到旧值
  const textRef = useRef("");
  const saveRef = useRef<() => void>(() => {});

  useEffect(() => {
    if (!sessionId || !path) return;
    let active = true;
    setLoading(true);
    setDirty(false);
    apiWorkspaceFile(agentId, sessionId, path)
      .then((data) => {
        if (!active) return;
        setText(data.text);
        textRef.current = data.text;
        setOriginal(data.git.original ?? "");
        setGitStatus(data.git.status ?? "ok");
        // 默认用普通编辑器查看完整文件，改动标注按需点击「显示改动」开启
        setShowDiff(false);
      })
      .catch(() => {
        if (active) setGitStatus("error");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [agentId, sessionId, path]);

  const gitChanges = gitStatus === "ok" || gitStatus === "untracked_new";

  const save = async () => {
    if (!sessionId || !path || readOnly) return;
    const content = textRef.current;
    try {
      const res = await apiSaveWorkspaceFile(agentId, sessionId, path, content);
      setDirty(false);
      setOriginal(res.git.original ?? "");
      setGitStatus(res.git.status ?? "ok");
      toast.success("已保存");
      onSaved?.();
    } catch (e: any) {
      toast.error(e?.message || "保存失败");
    }
  };
  // 每次渲染都把最新 save 挂到 ref，Ctrl/Cmd+S 永远调最新版本
  saveRef.current = save;

  const handleChange = (v: string | undefined) => {
    textRef.current = v ?? "";
    setText(v ?? "");
    setDirty(true);
  };

  const handleMount = (editor: any, monaco: any) => {
    // 仅原始(HEAD)侧只读，修改侧可编辑
    editor.getOriginalEditor?.().updateOptions({ readOnly: true });
    // DiffEditor 不支持 onChange prop，监听修改侧内容变化
    editor.getModifiedEditor?.().onDidChangeModelContent(() => {
      handleChange(editor.getModifiedEditor().getValue());
    });
    editor.addCommand?.(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () =>
      saveRef.current()
    );
  };

  if (!sessionId || !path) {
    return <Fallback title="从左侧文件浏览器选择一个文件查看 / 编辑" />;
  }

  /** 原始内容地址：图片 / PDF 内联预览，或加 dl=1 下载 */
  const rawUrl = (dl = false) =>
    workspaceFileRawUrl(agentId, sessionId ?? "", path ?? "", dl);

  const lang = monacoLangOf(path) ?? "plaintext";
  // 扩展名判定为主、内容探测兜底：扩展名说它是文本、实际是二进制时降级为
  // 「不可预览」，否则 Monaco 里塞满 \x00 与替换字符，既看不懂又卡。
  const kind: ViewerKind = !loading && looksBinary(text) ? "binary" : viewerKindOf(path ?? "");
  const canEdit = isEditableKind(kind);
  const tooBig = (text?.length ?? 0) > MAX_EDIT_BYTES;

  const editorArea = loading ? (
    <div className="flex items-center justify-center gap-2 p-6 text-[13px] text-ink-muted">
      <Loader2 className="h-4 w-4 animate-spin" />
      加载文件…
    </div>
  ) : (
    <>
      {gitChanges && showDiff ? (
        <DiffEditor
          height="100%"
          language={lang}
          /* keeper-light：在 monaco.ts 里定义，把编辑器表面色对齐应用画布 */
          theme="keeper-light"
          original={original}
          modified={text}
          onMount={handleMount}
          options={{ ...MONACO_OPTIONS, readOnly, renderSideBySide: false }}
        />
      ) : (
        <Editor
          height="100%"
          language={lang}
          theme="keeper-light"
          value={text}
          onMount={(editor, monaco) =>
            editor.addCommand?.(
              monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS,
              () => saveRef.current()
            )
          }
          onChange={(v) => handleChange(v)}
          options={{ ...MONACO_OPTIONS, readOnly: readOnly || !canEdit }}
        />
      )}
    </>
  );

  /** 图片：按容器宽度自适应 */
  const imageArea = (
    <div style={{ height: "100%", overflow: "auto", padding: 16, textAlign: "center" }}>
      <img
        src={rawUrl()}
        alt={path ?? ""}
        style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
      />
    </div>
  );

  /** PDF：交给浏览器内建的 PDF 视图 */
  const pdfArea = (
    <iframe
      src={rawUrl()}
      title={path ?? undefined}
      style={{ width: "100%", height: "100%", border: "none" }}
    />
  );

  /** 视频：浏览器内建播放器（controls 已带进度/音量/全屏） */
  const videoArea = (
    <div style={{ height: "100%", display: "flex", alignItems: "center", background: "#000" }}>
      <video
        src={rawUrl()}
        controls
        style={{ width: "100%", maxHeight: "100%" }}
      />
    </div>
  );

  /**
   * HTML：用浏览器渲染。
   *
   * sandbox 里**刻意不含 allow-same-origin**：这样页面里的脚本拿不到本站的
   * cookie / localStorage，也碰不到父页面——agent 生成的 HTML 属于低可信内容。
   * 只给 allow-scripts 是为了让交互效果（图表、动画）能正常跑。
   */
  const htmlArea = (
    <iframe
      src={rawUrl()}
      title={path ?? undefined}
      sandbox="allow-scripts"
      style={{ width: "100%", height: "100%", border: "none", background: "#fff" }}
    />
  );

  /** 二进制 / 不可预览：只给元信息与下载，不硬塞进编辑器 */
  const binaryArea = (
    <Fallback
      title={path ?? ""}
      desc={`这个文件是二进制格式（${
        kind === "binary" ? "无法预览" : "当前展示器不支持"
      }）`}
      action={
        <a href={rawUrl(true)} className={BTN}>
          <Download className="h-3 w-3" />
          下载文件
        </a>
      }
    />
  );

  /** 文件过大：不进编辑器，避免 Monaco 卡死浏览器 */
  const tooBigArea = (
    <Fallback
      title={`文件过大（约 ${Math.round((text?.length ?? 0) / 1024)} KB）`}
      desc="编辑器渲染会卡顿，已跳过预览"
      action={
        <a href={rawUrl(true)} className={BTN}>
          <Download className="h-3 w-3" />
          下载查看
        </a>
      }
    />
  );

  // 按类型分派：markdown 渲染文档、monaco 类编辑、图片/pdf/二进制各自专用展示器
  const body = loading
    ? editorArea
    : tooBig
      ? tooBigArea
      : kind === "image"
        ? showPreview
          ? imageArea
          : editorArea // SVG 看成 XML 源码时用编辑器
        : kind === "html"
          ? showPreview
            ? htmlArea
            : editorArea // 切「源码」看 HTML 文本
          : kind === "video"
            ? videoArea
            : kind === "pdf"
              ? pdfArea
          : kind === "binary"
            ? binaryArea
            : kind === "markdown" && showPreview
              ? (
                  <div style={{ height: "100%", overflow: "auto", padding: 20 }}>
                    <MarkdownView text={text} mode="static" />
                  </div>
                )
              : editorArea;

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      {/* 头部：文件名 + git 状态 + 操作 */}
      <div className="flex items-center gap-2 border-b border-line-soft px-2.5 py-1.5 text-[13px]">
        <span className="min-w-0 truncate font-medium text-ink-strong">
          {path}
        </span>
        {gitTag(gitStatus)}
        <Pill tone="muted">{kind}</Pill>
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {/* 预览/源码切换：markdown/html 默认看渲染结果，SVG 默认看图 */}
          {(kind === "markdown" || kind === "html" || kind === "image") && (
            <button
              type="button"
              onClick={() => setShowPreview((v) => !v)}
              className={BTN}
            >
              {showPreview ? "源码" : "预览"}
            </button>
          )}
          {isMonacoKind(kind) && gitChanges && (
            <button
              type="button"
              onClick={() => setShowDiff((v) => !v)}
              className={BTN}
            >
              {showDiff ? (
                <EyeOff className="h-3 w-3" />
              ) : (
                <Eye className="h-3 w-3" />
              )}
              {showDiff ? "隐藏改动" : "显示改动"}
            </button>
          )}
          {/* 这些类型在浏览器自己的查看器里更好用（能缩放/全屏/另存/分享链接） */}
          {opensInNewTab(kind) && (
            <a
              href={rawUrl()}
              target="_blank"
              rel="noreferrer"
              title="浏览器新窗口打开"
              className={ICON_BTN}
            >
              <Eye className="h-3 w-3" />
            </a>
          )}
          <a href={rawUrl(true)} title="下载原始文件" className={ICON_BTN}>
            <Download className="h-3 w-3" />
          </a>
          {!readOnly && canEdit && (
            <button
              type="button"
              disabled={!dirty}
              onClick={save}
              className={cn(
                "inline-flex h-6 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-white transition-colors disabled:cursor-not-allowed",
                dirty ? "bg-brand-gradient shadow-brand hover:brightness-110" : "bg-brand/30"
              )}
            >
              <Save className="h-3 w-3" />
              保存{dirty ? " *" : ""}
            </button>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1">{body}</div>
    </div>
  );
}
