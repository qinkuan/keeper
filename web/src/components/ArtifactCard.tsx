import { useEffect, useState } from "react";
import { Download, Eye, File, FolderOpen, Loader2 } from "lucide-react";
import Editor, { DiffEditor } from "@monaco-editor/react";
import type { Artifact } from "../api/types";
import {
  artifactUrl,
  artifactDiffUrl,
  apiRevealArtifact,
  apiDeployInfo,
} from "../api/client";
import { monacoLangOf } from "../monacoLang";
/* 主题在这里注册（beforeMount 拿得到 monaco 实例），所以产物卡片不必为了换个
   配色把 3.9MB 的 monaco 打进自己的包里 —— 见 monacoTheme.ts 的说明。 */
import { applyKeeperTheme, KEEPER_THEME } from "../monacoTheme";
import MarkdownView from "./MarkdownView";
import BlockView from "./BlockView";
import ImageViewer from "./ImageViewer";
import { isBlockArtifact, isMonacoKind, isUrlKind, opensInNewTab, viewerKindOf } from "./viewers";

interface ArtifactCardProps {
  artifact: Artifact;
  agentId: string;
  messageId: string;
  /** 部署形态（本机/远程）；不传则前端自行查询 deploy-info。 */
  isLocal?: boolean;
}

function fmtSize(n?: number): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 小按钮：卡片头部那排操作统一的观感（24px 高、描边、hover 变蓝） */
const BTN =
  "inline-flex h-6 items-center gap-1 rounded-md border border-line px-1.5 text-[12px] text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong";
const ICON_BTN =
  "flex h-6 w-6 items-center justify-center rounded-md border border-line text-ink-muted transition-all duration-150 hover:bg-surface hover:text-ink-strong disabled:opacity-50";

const PLACEHOLDER_STYLE: React.CSSProperties = {
  padding: 16,
  color: "var(--kp-text-muted)",
  fontSize: 12,
  textAlign: "center",
};

const MONACO_OPTIONS = {
  readOnly: true,
  minimap: { enabled: false },
  fontSize: 12,
  scrollBeyondLastLine: false,
  wordWrap: "on" as const,
};

export function ArtifactCard({
  artifact,
  agentId,
  messageId,
  isLocal,
}: ArtifactCardProps) {
  // 非文件块（chart / mermaid / …）整张交给 BlockView 按 kind 渲染；
  // 文件块才走下面的 Monaco / iframe 逻辑。两者互不干扰。
  if (isBlockArtifact(artifact)) {
    return (
      <BlockView
        artifact={artifact}
        agentId={agentId}
        messageId={messageId}
        isLocal={isLocal}
      />
    );
  }

  // agent 直接用 fs.write_file 写出的 .mmd / .mermaid 文件（无 kind 标记）：
  // 同样按 mermaid 渲染。MermaidBlock 在无 src 时会去 fetch 文件内容。
  if (/\.(mmd|mermaid)$/i.test(artifact.name)) {
    return (
      <BlockView
        artifact={{ ...artifact, kind: "mermaid" }}
        agentId={agentId}
        messageId={messageId}
        isLocal={isLocal}
      />
    );
  }

  const previewSrc = artifactUrl(agentId, messageId, artifact.id);
  const downloadSrc = artifactUrl(agentId, messageId, artifact.id, true);
  const [local, setLocal] = useState<boolean | null>(isLocal ?? null);
  const [revealing, setRevealing] = useState(false);

  // 展示器类型：**和文件工作台共用一张表**（components/viewers.ts），避免同一份
  // 文件在「对话里的产物卡片」和「工作台」里长得不一样。
  const kind = viewerKindOf(artifact.name);
  const lang = monacoLangOf(artifact.name) ?? "plaintext";
  const [expanded, setExpanded] = useState(false);
  // markdown / html 默认看渲染效果，可切回源码
  const [showSource, setShowSource] = useState(false);
  // iframe / img 直接用 URL，不需要先把内容读成文本；
  // Markdown 要文本才能渲染，html 切到「源码」时也要
  const needsText =
    kind === "markdown" || (isMonacoKind(kind) && (!isUrlKind(kind) || showSource));
  // 是否叠加 git 改动标注（默认开；关闭则只看干净文件）
  const [anno, setAnno] = useState(true);
  const [codeText, setCodeText] = useState<string | null>(null);
  const [loadingCode, setLoadingCode] = useState(false);

  const [diffStatus, setDiffStatus] = useState<string>("");
  const [diffOriginal, setDiffOriginal] = useState<string>("");
  const [diffModified, setDiffModified] = useState<string>("");
  const [loadingDiff, setLoadingDiff] = useState(false);

  useEffect(() => {
    if (isLocal !== undefined) {
      setLocal(isLocal);
      return;
    }
    let active = true;
    apiDeployInfo(agentId)
      .then((d) => active && setLocal(d.local))
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [agentId, isLocal]);

  const reveal = async () => {
    if (revealing) return;
    setRevealing(true);
    try {
      await apiRevealArtifact(agentId, messageId, artifact.id);
    } catch {
      /* 忽略：前端已降级为只显示下载 */
    } finally {
      setRevealing(false);
    }
  };

  /** 取文本（Markdown 渲染 / Monaco 显示 / html 切源码时才需要） */
  const loadText = async () => {
    setLoadingCode(true);
    setLoadingDiff(true);
    try {
      const [codeRes, diffRes] = await Promise.all([
        fetch(previewSrc),
        fetch(artifactDiffUrl(agentId, messageId, artifact.id)),
      ]);
      setCodeText(await codeRes.text());
      let data: { status?: string; original?: string; modified?: string } = {};
      try {
        data = await diffRes.json();
      } catch {
        data = { status: "error" };
      }
      setDiffStatus(data.status ?? "ok");
      setDiffOriginal(data.original ?? "");
      setDiffModified(data.modified ?? "");
      // 有实际改动时默认叠加 diff 标注；无改动 / 非 git 仅展示文件
      setAnno(data.status === "ok" || data.status === "untracked_new");
    } catch {
      setDiffStatus("error");
    } finally {
      setLoadingCode(false);
      setLoadingDiff(false);
    }
  };

  // 展开后才取文本，且只在真正需要时取：html / 图片 / pdf 用 URL 直接渲染，
  // 但 html 一切到「源码」就得有内容——所以这里盯住 showSource 变化。
  useEffect(() => {
    if (!expanded || !needsText || codeText !== null || loadingCode) return;
    void loadText();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, needsText, showSource]);

  const toggle = () => setExpanded((v) => !v);

  // 按钮文案随类型变：「查看代码」用在图片上会让人以为点开是源码
  const toggleLabel = (() => {
    if (expanded) return "收起";
    switch (kind) {
      case "markdown":
        return "查看文档";
      case "html":
        return "网页预览";
      case "image":
        return "查看图片";
      case "video":
        return "播放视频";
      case "pdf":
        return "查看 PDF";
      default:
        return "查看代码";
    }
  })();
  const canInline = kind !== "binary";
  // html / pdf / 视频交给浏览器新窗口：内联在 360px 里没法看；
  // 浏览器自己的查看器能缩放、全屏、另存、还能分享链接。
  // 图片例外：走内联 PanZoomStage 预览（缩放/平移/双击），更贴合会话列。
  const useNewTab = opensInNewTab(kind) && kind !== "image";

  const isPreviewable =
    artifact.mime.startsWith("image/") ||
    artifact.mime.startsWith("text/") ||
    ["application/json", "application/javascript"].includes(artifact.mime);

  const size = artifact.size;
  const fileIconColor = artifact.mime.startsWith("image/") ? "#3fa46a" : "var(--kp-primary)";

  // 文件相对 HEAD 确有改动（含未跟踪新文件）→ 可切换「改动标注」
  const gitChanges = diffStatus === "ok" || diffStatus === "untracked_new";

  return (
    <div style={{ marginTop: 6 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: 8,
          border: "1px solid var(--kp-border-soft)",
          borderRadius: 8,
          background: "var(--kp-surface)",
        }}
      >
        <File
          className="h-[18px] w-[18px] shrink-0"
          style={{ color: fileIconColor }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontWeight: 500,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {artifact.name}
          </div>
          <div
            style={{
              fontSize: 12,
              color: "var(--kp-text-muted)",
              display: "flex",
              gap: 8,
            }}
          >
            <span>{artifact.mime}</span>
            {size != null && <span>{fmtSize(size)}</span>}
          </div>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {useNewTab ? (
            <a
              href={previewSrc}
              target="_blank"
              rel="noreferrer"
              title="浏览器新窗口打开"
              className={BTN}
            >
              <Eye className="h-3 w-3" />
              {toggleLabel}
            </a>
          ) : canInline ? (
            <button
              type="button"
              onClick={toggle}
              title={expanded ? "收起" : "在对话内查看"}
              className={BTN}
            >
              {toggleLabel}
            </button>
          ) : (
            isPreviewable && (
              <a
                href={previewSrc}
                target="_blank"
                rel="noreferrer"
                title="新标签页预览"
                className={BTN}
              >
                <Eye className="h-3 w-3" />
                预览
              </a>
            )
          )}
          {kind === "image" && (
            <a
              href={previewSrc}
              target="_blank"
              rel="noreferrer"
              title="新窗口打开原图"
              className={BTN}
            >
              <Eye className="h-3 w-3" />
              新窗口
            </a>
          )}
          <a href={downloadSrc} title="下载" className={BTN}>
            <Download className="h-3 w-3" />
            下载
          </a>
          {local && (
            <button
              type="button"
              title="在文件管理器打开所在目录"
              disabled={revealing}
              onClick={reveal}
              className={ICON_BTN}
            >
              {revealing ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <FolderOpen className="h-3 w-3" />
              )}
            </button>
          )}
        </div>
      </div>

      {canInline && expanded && (
        <div
          style={{
            marginTop: 8,
            border: "1px solid #eee",
            borderRadius: 6,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "4px 8px",
              background: "var(--kp-surface)",
              fontSize: 12,
              color: "#666",
            }}
          >
            <span>
              {artifact.name} · {kind === "markdown" ? kind : lang}
            </span>
            <div style={{ display: "flex", gap: 4 }}>
              {kind === "markdown" && (
                <button
                  type="button"
                  onClick={() => setShowSource((v) => !v)}
                  className={BTN}
                >
                  {showSource ? "预览" : "源码"}
                </button>
              )}
              {isMonacoKind(kind) && gitChanges && !showSource && (
                <button
                  type="button"
                  onClick={() => setAnno((a) => !a)}
                  className={BTN}
                >
                  {anno ? "隐藏改动标注" : "显示改动标注"}
                </button>
              )}
            </div>
          </div>
          <div style={{ borderTop: "1px solid #eee" }}>
            {/* 图片：内联缩放/平移预览（PanZoomStage） */}
            {kind === "image" ? (
              <ImageViewer src={previewSrc} alt={artifact.name} />
            ) : kind === "markdown" && !showSource ? (
              <div
                style={{
                  padding: 16,
                  maxHeight: 360,
                  overflow: "auto",
                  background: "#fff",
                }}
              >
                <MarkdownView text={codeText ?? ""} mode="static" />
              </div>
            ) : loadingCode || loadingDiff ? (
              <div style={PLACEHOLDER_STYLE}>加载中…</div>
            ) : diffStatus === "missing" ? (
              <div style={PLACEHOLDER_STYLE}>文件不存在</div>
            ) : diffStatus === "error" ? (
              <div style={PLACEHOLDER_STYLE}>加载失败</div>
            ) : diffStatus === "not_git" ? (
              <div>
                <Editor
                  height="360px"
                  language={lang}
                  theme={KEEPER_THEME}
                  beforeMount={applyKeeperTheme}
                  value={codeText ?? ""}
                  options={MONACO_OPTIONS}
                />
                <div style={{ padding: "4px 8px", fontSize: 12, color: "var(--kp-text-muted)" }}>
                  不在 git 仓库内，仅展示文件内容
                </div>
              </div>
            ) : gitChanges && anno ? (
              <DiffEditor
                height="360px"
                language={lang}
                theme={KEEPER_THEME}
                beforeMount={applyKeeperTheme}
                original={diffOriginal}
                modified={diffModified}
                options={{ ...MONACO_OPTIONS, renderSideBySide: false }}
              />
            ) : (
              <Editor
                height="360px"
                language={lang}
                theme={KEEPER_THEME}
                beforeMount={applyKeeperTheme}
                value={codeText ?? diffModified}
                options={MONACO_OPTIONS}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default ArtifactCard;
