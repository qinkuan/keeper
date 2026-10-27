import { lazy, memo, Suspense, useState, useMemo, type ReactNode } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Copy,
  Loader2,
  RefreshCw,
  User,
} from "lucide-react";
import { toast } from "./ui/Toast";
import { CollapseItem } from "./ui/Collapse";
import MarkdownView from "./MarkdownView";
import { cn } from "../lib/utils";
import { attachmentUrl } from "../api/client";

/**
 * 产物卡片按需加载：它内部用 Monaco 做代码/diff 展示（3.9MB）。
 * 静态引入的话，只要有一条消息带产物，整页历史都要等 monaco 下载完。
 */
const ArtifactCard = lazy(() => import("./ArtifactCard"));
import type { Artifact, ChatMessage } from "../api/types";
import { StepUsageTag, UsageTag } from "./UsageTag";

/** 截断成一行预览（去掉换行，避免占好几行） */
function clip(text: string, n: number): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n) + "…" : flat;
}

/**
 * 可折叠的一段（「调用 / 结果」）：**默认收起**，点标题才展开。
 *
 * 几十步的规划过程如果每步都把入参和工具返回全文铺开，整个面板会被几万字符
 * 淹没，真正要看的思考链和用量反而找不到了。收起时给一行预览，足够判断要不要展开。
 */
function FoldBlock({
  label,
  head,
  children,
  defaultOpen = false,
}: {
  label: string;
  /** 收起状态下显示的一行摘要 */
  head?: React.ReactNode;
  children?: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mt-0.5">
      <span
        onClick={() => setOpen(!open)}
        className="inline-flex cursor-pointer select-none items-center gap-1 text-[12px] text-ink-faint transition-colors hover:text-ink-muted"
        title={open ? "点击收起" : "点击展开"}
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        <span>{label}：</span>
        {!open && head}
      </span>
      {open && <div>{children}</div>}
    </div>
  );
}

/** 消息动作条里的小图标按钮：默认很淡，hover 才变深 */
function MsgAction({
  title,
  onClick,
  children,
}: {
  title: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-black/[0.05] hover:text-ink dark:hover:bg-white/10"
    >
      {children}
    </button>
  );
}

/** 时间戳：今天显示 HH:mm，更早显示 MM-DD HH:mm */
function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

/**
 * 单条消息气泡。
 *
 * 包了 memo：ChatPage 的输入框是页面级 state，**每敲一个字都会重渲染整个页面**。
 * 回调因此设计成「接收 messageId」而不是「闭包捕获 id」——后者每次渲染都是新
 * 函数，会让 memo 直接失效（这是给组件加 memo 最常见的坑）。
 */
/** 用户气泡里的 @标签 渲染：把 @标签 渲染成高亮 chip（不附绝对路径，避免冗余；
 * 模型侧需要路径时走 chat 请求的 mentions 结构化字段，气泡只负责展示 @标签 本身） */
function MentionChips({
  raw,
  mentions,
}: {
  raw: string;
  mentions?: { label: string; resolved: string }[];
}) {
  if (!mentions || !mentions.length) return <>{raw}</>;
  const re = new RegExp(
    "@(" +
      mentions
        .map((m) => m.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
        .join("|") +
      ")",
    "g"
  );
  const nodes: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(raw)) !== null) {
    if (m.index > last)
      nodes.push(<span key={`t${i++}`}>{raw.slice(last, m.index)}</span>);
    nodes.push(
      <span key={`c${i++}`}>
        <span className="rounded bg-brand/15 px-px text-brand">@{m[1]}</span>
      </span>
    );
    last = re.lastIndex;
  }
  if (last < raw.length)
    nodes.push(<span key={`t${i++}`}>{raw.slice(last)}</span>);
  return <>{nodes}</>;
}

function ChatBubble({
  msg,
  agentId,
  sessionId,
  isLocal = true,
  onUsageDetail,
  onUsageDump,
  onRegenerate,
}: {
  msg: ChatMessage;
  agentId: string;
  /** 消息头显示的 agent 名。**现在不展示了**（只留「已完成 · 用时」），
   *  但类型留着：调用方还在传，直接删会让它们编译不过。 */
  agentName?: string;
  /** 这条消息所属会话：附件预览 URL 要用（附件缓存在会话私有临时区） */
  sessionId?: string;
  /** 是否本机部署（决定产物卡片能否「打开文件夹」）；未传时按本机处理 */
  isLocal?: boolean;
  /** 点用量里的「查看详情」：打开本轮逐步的时间线可视化（传 messageId 而非闭包） */
  onUsageDetail?: (messageId: string) => void;
  /** 点用量里的「查看 dump」：打开该轮完整 prompt（仅落盘开启时可见） */
  onUsageDump?: (messageId: string) => void;
  /** 重新生成这条回答（传 messageId 而不是闭包，避免 memo 失效） */
  onRegenerate?: (messageId: string) => void;
}) {
  const isUser = msg.role === "user";
  const timeText = msg.created_at ? formatTime(msg.created_at) : "";
  /** 「自主规划过程」的展开态：由这里持有，才好在流式结束时按需变化 */
  const [stepsOpen, setStepsOpen] = useState(false);

  // 内联多模态：FINAL 文字里用 [[art:ref]] 把某块产物插到指定位置就地渲染，
  // 没被引用的产物仍沉到消息底部（见下方 artifacts 区块）。
  const artByRef = useMemo(() => {
    const m = new Map<string, Artifact>();
    for (const a of msg.artifacts ?? []) if (a.ref) m.set(a.ref, a);
    return m;
  }, [msg.artifacts]);
  const referencedRefs = useMemo(() => {
    const s = new Set<string>();
    const re = /\[\[art:([^\]]+)\]\]/g;
    let mm: RegExpExecArray | null;
    while ((mm = re.exec(msg.text ?? "")) !== null) s.add(mm[1].trim());
    return s;
  }, [msg.text]);
  const bottomArtifacts = (msg.artifacts ?? []).filter(
    (a) => !(a.ref && referencedRefs.has(a.ref))
  );
  const renderInline = (text: string): ReactNode[] => {
    const nodes: ReactNode[] = [];
    if (!text) return nodes;
    const re = /\[\[art:([^\]]+)\]\]/g;
    let last = 0,
      i = 0,
      m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      const before = text.slice(last, m.index);
      if (before)
        nodes.push(
          <MarkdownView
            key={`t${i++}`}
            text={before}
            mode={msg.streaming ? "streaming" : "static"}
          />
        );
      const ref = m[1].trim();
      const art = artByRef.get(ref);
      nodes.push(
        art ? (
          <div key={`a${i++}`} className="my-2">
            <ArtifactCard
              artifact={art}
              agentId={agentId}
              messageId={msg.id}
              isLocal={isLocal}
            />
          </div>
        ) : (
          <span
            key={`p${i++}`}
            className="rounded bg-black/[0.05] px-1 text-[12px] text-ink-faint"
          >
            [{ref}]
          </span>
        )
      );
      last = re.lastIndex;
    }
    const tail = text.slice(last);
    if (tail)
      nodes.push(
        <MarkdownView
          key={`t${i++}`}
          text={tail}
          mode={msg.streaming ? "streaming" : "static"}
        />
      );
    return nodes;
  };

  return (
    <div
      className={cn(
        // group/bubble：让动作条在 hover 整条消息时浮现（而不是 hover 图标本身）
        "kp-bubble-in group/bubble mb-5 flex items-start gap-2.5",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      {/* 头像：**两个角色都放在内容列前面**。
          用户消息整行是 flex-row-reverse（气泡靠右），DOM 里靠前的元素会被排到
          视觉右侧——所以头像必须写在前面，用户头像才会落在气泡右边；写在后面
          就会跑到气泡左边、压进气泡上沿（那是之前"布局有问题"的成因）。
          助手消息是正常行序，头像在前 = 在左，与用户侧刚好镜像。 */}
      {isUser ? (
        /* 用户头像（右）：**不画底**，就是一个裸的人形图标（品牌色描边）。
           助手那边是实心的机器人圆标，用户这边如果再套一个底，两个头像就像
           两枚按钮；去掉底之后，一个图标 + 一个机器人，各是各的。
           容器尺寸保留（26px），只为和气泡顶部对齐。 */
        <div className="mt-[2px] flex h-[26px] w-[26px] shrink-0 items-center justify-center text-brand">
          <User className="h-[19px] w-[19px]" strokeWidth={2} />
        </div>
      ) : (
        /* 助手头像（左）：圆形品牌渐变 + 同色投影 + 顶部内高光。
            内高光那一条是"看起来像玻璃/珐琅"的最小成本做法，纯色圆点会显得很平。 */
        <div className="mt-[3px] flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full bg-brand-gradient text-white shadow-[0_2px_6px_-1px_rgba(66,133,244,0.5),inset_0_1px_0_rgba(255,255,255,0.3)]">
          <Bot className="h-[14px] w-[14px]" strokeWidth={2.2} />
        </div>
      )}

      {/*
        列宽：助手消息占满整行（对话式产品的通行做法——正文才是主角，不套气泡，
        靠头像和留白区分角色）；用户消息仍是气泡，限宽 72% 并靠右。
      */}
      <div
        className={cn(
          "flex min-w-0 flex-col",
          isUser ? "max-w-[72%] items-end" : "flex-1 items-start"
        )}
      >
      {/*
        消息头（仅助手）：只留状态（进行中 / 已完成 · 用时）。
        原来这里还写着 agent 的名字，但对话区上方已经有 agent 身份了，
        每条回答再顶一行名字纯属噪音——去掉后只留"这条答完了、用了多久"。
      */}
      {!isUser && (
        <div className="mb-1.5 flex items-center gap-2 text-[12px]">
          {msg.streaming ? (
            <span className="flex items-center gap-1.5 text-ink-muted">
              <span className="kp-pulse-dot h-1.5 w-1.5 rounded-full bg-brand" />
              进行中
            </span>
          ) : (
            timeText && (
              <span className="text-ink-faint">已完成 · {timeText}</span>
            )
          )}
        </div>
      )}

      <div
        className={cn(
          "w-full text-[13.5px] leading-[1.72] text-ink",
          // 用户气泡：品牌色的极淡渐变底 + 无边框。比纯色底多一层"从左上到右下"
          // 的明度变化，斜着的方向和产品统一的气晕光一致，界面就不散。
          isUser &&
            "rounded-[14px] rounded-br-[5px] bg-brand-gradient-soft px-3.5 py-2.5 shadow-[inset_0_0_0_1px_rgba(66,133,244,0.48)]"
        )}
      >
        {msg.streaming && !msg.text ? (
          // 流式还没产出正文：给进度反馈（有步骤就说执行到第几步），避免看起来卡住
          <span className="text-[13px] text-ink-muted">
            {msg.steps?.length
              ? `已执行 ${msg.steps.length} 步，正在整理回答…`
              : "正在思考…"}
            <span className="kp-caret" />
          </span>
        ) : (
          <>
            {/* 助手回复基本都是 Markdown（标题 / 列表 / 表格 / 代码块）→ 渲染；
                用户消息保持纯文本，避免 a_b 变斜体、#1 变标题这类误伤 */}
            {isUser ? (
              msg.mentionRaw ? (
                <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  <MentionChips raw={msg.mentionRaw} mentions={msg.mentions} />
                </div>
              ) : (
                <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  {msg.text}
                </div>
              )
            ) : (
              <div>
                {renderInline(msg.text)}
                {msg.streaming && <span className="kp-caret" />}
              </div>
            )}
          </>
        )}

        {/* 被用户叫停：说明内容是「半截」的，避免看起来像回答不完整 */}
        {msg.canceled && !msg.streaming && (
          <div className="mt-1.5 text-[12px] text-ink-faint">已停止生成</div>
        )}

        {msg.role === "assistant" && msg.llm && (
          <span className="mt-1.5 inline-flex items-center rounded-md bg-brand/10 px-1.5 py-0.5 text-[11px] leading-4 text-brand">
            AI 生成
          </span>
        )}

        {msg.role === "assistant" && !!msg.steps?.length && (
          <CollapseItem
            // 始终收起：过程细节由用户主动点开再看，正文才是主角。
            // 流式期间的进度靠标题里的「N 步」实时递增 + 正文光标反馈，不再自动展开。
            open={stepsOpen}
            onToggle={() => setStepsOpen((v) => !v)}
            className="mt-3 border-line-soft bg-surface"
            label={
              <span className="flex items-center gap-1.5 text-[13px]">
                <span className="rounded-md bg-brand/12 px-1.5 py-0.5 text-[11px] leading-4 text-brand">
                  Agent
                </span>
                自主规划过程（{msg.steps.length} 步）
                {/* 收起状态下也要能看出过程还在推进 */}
                {msg.streaming && <span className="text-ink-muted">· 进行中</span>}
              </span>
            }
          >
              <div className="text-[12.5px] leading-[1.7]">
                {msg.steps.map((s, i) => {
                      // 流式进行中：最后一步的结果默认展开，方便看到最新进展
                      const streamingLast =
                        !!msg.streaming && i === msg.steps.length - 1;
                      return (
                      <div
                        key={i}
                        className="mb-2 border-l-2 border-line pl-2"
                        data-streaming-last={streamingLast ? "1" : undefined}
                      >
                        {s.thought && (
                          <div>
                            <span className="text-ink-muted">思考：</span>
                            {s.thought}
                          </div>
                        )}
                        {/* 调用与结果的正文都默认收起：几十步的过程如果全展开，
                            一眼望不到头，真正要看的（思考链 + 用量）反而被淹没了。 */}
                        {s.tool && (
                          <FoldBlock
                            label="调用"
                            head={
                              <>
                                <span className="mx-1 inline-flex rounded-md bg-blue-500/15 px-1.5 py-0.5 text-[11px] leading-4 text-blue-600 dark:text-blue-300">
                                  {s.tool}
                                </span>
                                {s.args && Object.keys(s.args).length > 0 && (
                                  <span className="text-ink-muted">
                                    {clip(JSON.stringify(s.args), 80)}
                                  </span>
                                )}
                              </>
                            }
                          >
                            {s.args && Object.keys(s.args).length > 0 && (
                              <pre className="mt-0.5 max-h-[240px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line-soft bg-surface p-1.5 font-mono text-[12px]">
                                {JSON.stringify(s.args, null, 2)}
                              </pre>
                            )}
                          </FoldBlock>
                        )}
                        {s.observation && (
                          <FoldBlock
                            label={
                              s.kind === "ask_human"
                                ? "提问"
                                : s.kind === "human_answer"
                                ? "用户补充"
                                : "结果"
                            }
                            head={
                              <span className="text-ink-muted">
                                {clip(s.observation, 100)}
                                {s.observation.length > 100
                                  ? ` …（共 ${s.observation.length} 字符）`
                                  : ""}
                              </span>
                            }
                            defaultOpen={streamingLast}
                          >
                            <div className="mt-0.5 max-h-[320px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line-soft bg-surface p-1.5 font-mono text-[12px]">
                              {s.observation}
                            </div>
                          </FoldBlock>
                        )}
                        {s.usage && (
                          <div className="mt-0.5">
                            <StepUsageTag usage={s.usage} />
                          </div>
                        )}
                      </div>
                      );
                    })}
              </div>
          </CollapseItem>
        )}

        {/* 本轮用量：**只在助手消息下渲染一次**。
            后端按「本轮锚点」聚合用量，而锚点就是触发这轮的用户消息 id，所以
            user 消息上也会带回一份**完全相同**的用量——再渲染一遍就是重复，
            而且容易让人以为这是「提问本身的开销」。助手消息里有就够了。 */}
        {msg.role === "assistant" && msg.usage && (
          <div className="mt-2 text-left">
            <UsageTag
              usage={msg.usage}
              wallMs={msg.duration_ms}
              onDetail={
                onUsageDetail ? () => onUsageDetail(msg.id) : undefined
              }
              onDump={onUsageDump ? () => onUsageDump(msg.id) : undefined}
            />
          </div>
        )}

        {msg.role === "assistant" && bottomArtifacts.length > 0 && (
          <div className="mt-3 space-y-2">
            <Suspense
              fallback={
                <div className="rounded-lg border border-line-soft bg-surface px-3 py-2 text-[12px] text-ink-muted">
                  加载产物预览…
                </div>
              }
            >
              {bottomArtifacts.map((a) => (
                <ArtifactCard
                  key={a.id}
                  agentId={agentId}
                  messageId={msg.id}
                  artifact={a}
                  isLocal={isLocal}
                />
              ))}
            </Suspense>
          </div>
        )}

        {/* 用户上传的附件：只挂在 user 消息上（artifacts 反过来只挂 assistant）。
            与 ArtifactCard 分开而不是复用它：那个面向「agent 产出的文件」，有
            diff / reveal / 源码切换这一整套；附件只需要「看清楚我当时传了什么」，
            一个缩略图 + 一个下载就够了。复用会让卡片上半屏功能全点不动。 */}
        {isUser && !!msg.attachments?.length && (
          <div className="mt-2 flex flex-wrap gap-2">
            {msg.attachments.map((a) => (
              <a
                key={a.id}
                href={attachmentUrl(agentId, sessionId || "", a.id)}
                target="_blank"
                rel="noreferrer"
                title={`${a.name} · ${(a.size / 1024).toFixed(0)} KB · 点击打开预览`}
                className="group block h-[64px] w-[64px] overflow-hidden rounded-lg border border-line-soft bg-surface"
              >
                <img
                  src={attachmentUrl(agentId, sessionId || "", a.id)}
                  alt={a.name}
                  className="h-full w-full object-cover transition-transform group-hover:scale-[1.04]"
                />
              </a>
            ))}
          </div>
        )}

        {msg.error && (
          <div className="mt-3 rounded-lg border border-red-500/25 bg-red-500/5 px-3 py-2 text-[13px] text-red-600 dark:text-red-300">
            {msg.error}
          </div>
        )}

        {/* 只在「非流式且确实什么都没有」时才提示暂无回答：
            流式期间上面已经有进度反馈，这里再出现就会重复显示两种空态 */}
        {msg.role === "assistant" &&
          !msg.streaming &&
          !msg.text &&
          !msg.error &&
          !msg.steps?.length && (
            <div className="mt-3">
              <div className="py-4 text-center text-[13px] text-ink-faint">
                暂无回答
              </div>
            </div>
          )}
      </div>

        {/*
          消息动作条（仅助手）：截图里正文下方有一排**很淡的图标**——复制、
          反馈、朗读、重生成、更多。这里的策略是：默认 `opacity-0`，hover 整块
          消息时才浮现。对话产品的动作条常驻会不断打断阅读。
        */}
        {!isUser && !msg.streaming && msg.text && (
          <div className="mt-1.5 flex items-center gap-0.5 opacity-0 transition-opacity duration-150 group-hover/bubble:opacity-100">
            <MsgAction
              title="复制回答"
              onClick={() => {
                navigator.clipboard?.writeText(msg.text ?? "");
                toast.success("已复制");
              }}
            >
              <Copy className="h-3.5 w-3.5" />
            </MsgAction>
            <MsgAction
              title="重新生成"
              onClick={() => onRegenerate?.(msg.id)}
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </MsgAction>
          </div>
        )}
      </div>
    </div>
  );
}

// props 里的 msg 是**对象**，所以它能不能挡住重渲染取决于引用是否稳定——
// 流式更新走 `prev.map(m => m.id === id ? {...m, ...patch} : m)`，未变化的消息
// 保持原引用，因此历史消息在输入时会被 memo 挡住。
export default memo(ChatBubble);
