import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { DragEvent } from "react";

import {
  ArrowUp,
  FileText,
  List,
  ListChecks,
  Loader2,
  Plus,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  UploadCloud,
} from "lucide-react";
import { cn } from "../lib/utils";
import { X } from "lucide-react";
import { SegmentedControl } from "../components/ui/Segmented";
import { Hint as AlertHint } from "../components/ui/Page";
import { toast } from "../components/ui/Toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ChatBubble from "../components/ChatBubble";
import SessionList from "../components/SessionList";
import AskBox from "../components/AskBox";
/*
 * 编码工作台按需加载：monaco 有 3.9MB，而多数人多数时候不开工作台。
 * 静态 import 的话，进对话页就要先下载它——首屏被一个用不到的编辑器拖住。
 */
const FileExplorer = lazy(() => import("../components/FileExplorer"));
const CodeView = lazy(() => import("../components/CodeView"));
/* 浏览器工作台同样按需加载：不开工作台的人不该为它付首屏。
   这里只有 Electron 嵌入的那一份（WebContentsView）。曾还有个Agent 浏览器工作台
   （Playwright 自带的独立 Chromium 窗口），**已删**：它够不到用户自己 Chrome 里的
   页面 —— 那条链路现在由浏览器扩展接管，见 keeper「浏览器」页。 */
const BrowserPanel = lazy(() => import("../components/BrowserPanel"));
import AgentInfoDrawer from "../components/AgentInfoDrawer";
import UsageDetailModal from "../components/UsageDetailModal";
import { ModelPicker } from "../components/ModelPicker";
import {
  WorkbenchPicker,
  type WorkbenchType,
} from "../components/WorkbenchPicker";
import { WorkspacePicker } from "../components/WorkspacePicker";
import { ThinkingPicker } from "../components/ThinkingPicker";
import { ModePicker } from "../components/ModePicker";
import PromptDumpModal from "../components/PromptDumpModal";
import { UsageTag, fmtTokens } from "../components/UsageTag";
import MentionPicker from "../components/MentionPicker";
import {
  loadMention,
  resolveSnippet,
  filterMentionItems,
  MENTION_EVENT,
  type MentionConfig,
  type MentionItem,
} from "../lib/mention";
import { ADD_QUOTE_EVENT, type ChatQuote } from "../lib/quote";
import type { UsageSummary } from "../api/types";


/** 可拖拽竖分隔条：拖动时按水平位移回调，由父级调整列宽。 */
function Resizer({ onResize }: { onResize: (dx: number) => void }) {
  const dragging = useRef(false);
  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (dragging.current) onResize(e.movementX);
    };
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [onResize]);
  return (
    <div
      onMouseDown={() => {
        dragging.current = true;
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      }}
      style={{
        width: 6,
        flex: "0 0 auto",
        cursor: "col-resize",
        background: "var(--kp-border-soft)",
      }}
    />
  );
}
import {
  apiAbortChat,
  apiChat,
  apiChatStream,
  apiResumeChatStream,
  newRunId,
  apiDeployInfo,
  apiListAgents,
  apiListTasks,
  apiListUserSpaces,
  apiGetMySpace,
  apiSessionActivity,
  apiSessionMessages,
  apiSessions,
  apiSessionWorkspace,
  apiUpdateSessionWorkspace,
  apiSessionMetrics,
  apiBudget,
  apiGetSettings,
  apiUploadAttachment,
  attachmentUrl,
} from "../api/client";
import type {
  Attachment,
  ChatMessage,
  SessionKind,
  SessionWorkspace,
  UserSpace,
  ReasoningEffort,
  RunMode,
} from "../api/types";

/*
 * 任务视图按需加载：TaskPage 有 49KB 源码，而且它静态引入了 ArtifactCard，
 * 后者又用 Monaco 做代码/diff 展示——静态引入的话，即使只用「会话」模式，
 * 首屏也要把 monaco 一起下载下来。
 */
const TaskPage = lazy(() => import("./TaskPage"));

const newId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

/**
 * 每个 agent 单独记会话 id，避免在不同 agent 间串号。
 *
 * 后面那段 `.${kind}` 是为了**把同一个 agent 的三类会话分开记**：不加的话，
 * 在浏览器页刷新会恢复到「对话」页最后那条会话（反之亦然），而那条会话在当前
 * 列表里根本不存在——表现为「一进来就是个陌生会话，且列表里找不到它」。
 */
const sessionKey = (agentId: string, kind: SessionKind) =>
  `keeper.sessionId.${agentId}.${kind}`;

const welcomeMsg: ChatMessage = {
  id: "welcome",
  role: "assistant",
  text: "你好，我是这个 agent。你可以问我任何关于它能力范围内的问题，我会处理后回答。",
};

/** 新会话在拿到真实 session_id 之前，流式气泡先落这个临时桶 */
const PENDING_BUCKET = "__pending__";

/**
 * 消息列表**按会话索引**：桶 key -> 该会话的消息。
 *
 * 桶 key 与 `streamRuns` 完全一致（真实 sessionId，或新会话时的 PENDING_BUCKET），
 * 两套状态因此永远指向同一个会话。
 */
type MsgsBySession = Record<string, ChatMessage[]>;

/** 空桶的占位。必须是模块级常量：每次渲染现造一个数组，会把下游所有引用比较打乱。 */
const EMPTY_WITH_WELCOME: ChatMessage[] = [welcomeMsg];

/* ══════════════ 临时诊断（KEEPER_DIAG）══════════════
 * 定位「会话页永久显示『正在思考』」。查完请整体删除：搜索 KEEPER_DIAG。
 *
 * 为什么需要它：这个问题已经在同一个会话上复现三次，而清残留的兜底加了三道
 * 都没打中——说明我对"页面为什么认为在跑"的推断有盲区。靠读代码继续猜只会
 * 再错，所以改成把每一环的实际取值打出来，让事实说话。
 *
 * 用法：打开 DevTools Console，过滤 keeper:diag。只在**值变化时**打印，
 * 否则 3 秒一次的探测会把控制台刷满。
 */
const DIAG_ENABLED = true;
const _diagSeen: Record<string, string> = {};
function diag(key: string, value: unknown) {
  if (!DIAG_ENABLED) return;
  const s = typeof value === "string" ? value : JSON.stringify(value);
  if (_diagSeen[key] === s) return;
  _diagSeen[key] = s;
  // eslint-disable-next-line no-console
  console.log(`[keeper:diag] ${key} = ${s}`);
}

/** 某会话的分页游标（见组件内 paging） */
type PagingState = {
  /** 这一段前面还有没有更早的消息 → 要不要显示「加载更早的消息」 */
  hasMore: boolean;
  /** 这一段里最小的 seq，拿它当下次请求的 before_seq */
  oldestSeq: number | null;
};

/** 一个正在流式跑着的会话：用户消息 + 占位回复 + 中断句柄 */
type StreamRun = {
  user: ChatMessage;
  assistant: ChatMessage;
  runId: string;
  abort: AbortController | null;
  /** 这个桶是不是**重连**建起来的（而不是本页自己发起的流）。
   *
   * 区分它，是为了让"清残留"能精确下手：重连的桶在服务端那一轮结束后就该拆掉，
   * 而本页自己发起的流即使此刻服务端还没登记上 run，也是**真的在跑**——
   * 一刀切会把用户正在进行的那一轮的本地显示直接掐掉。 */
  resumed?: boolean;
};

export default function ChatPage({
  agentId,
  sessionKind = "chat",
}: {
  agentId: string;
  /**
   * 这个页面只管哪一类会话：默认「对话」页（chat / task 两类由 tab 切换），
   * 浏览器页传 "browser" —— 那时列表只列浏览器会话，也不再显示 tab 切换。
   *
   * 用 prop 而不是内部再存一份：类别只决定「这一页列什么」，从外面传进来才是
   * 单一事实来源——两个 state 各记一份迟早会在切路由时不一致，而不一致的表现是
   * 「列表空着但会话还在」，极难定位。
   */
  sessionKind?: "chat" | "browser";
}) {
  /** 页面是否被锁在浏览器类别（锁定时不显示会话/任务切换） */
  const browserOnly = sessionKind === "browser";
  /**
   * 消息列表**按会话索引**。
   *
   * 为什么不能是单个数组：它其实只是「当前视图所在会话」的列表，却会被一堆**异步
   * 回调**写——loadMessages、流式的 onDone/onError、切会话、resume 收尾……扁平数组
   * 下这些回调无从知道该写到哪个会话，于是连着出过三类 bug：加载失败整屏清空、
   * A 的回复写进 B、消息按 sessionId 落到 A 而 UI 显示 B。
   *
   * 按会话索引后，「往哪写」由调用点自己指定，串会话这类错误从**结构上**不可能发生，
   * 之前打的那些归属校验补丁也就都不需要了。
   */
  const [msgsBySession, setMsgsBySession] = useState<MsgsBySession>({});
  const [input, setInput] = useState("");

  // 输入框 `@` 快捷项：检测到光标前出现 `@查询` 时弹出候选，选中后把短语插入消息。
  const [mention, setMention] = useState<{ start: number; query: string } | null>(
    null
  );
  const [activeMention, setActiveMention] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const [mentionCfg, setMentionCfg] = useState<MentionConfig>(() =>
    loadMention()
  );
  useEffect(() => {
    const onUpd = () => setMentionCfg(loadMention());
    window.addEventListener(MENTION_EVENT, onUpd);
    return () => window.removeEventListener(MENTION_EVENT, onUpd);
  }, []);
  const filteredMentions = mention
    ? filterMentionItems(mentionCfg.items, mention.query)
    : [];

  // 把输入串里「@标签」且标签命中设置项的子串识别为 mention（用于高亮 + 发送时展开）。
  const computeMentions = (
    text: string
  ): { start: number; end: number; item: MentionItem }[] => {
    const labels = mentionCfg.items;
    if (!labels.length) return [];
    const re = new RegExp(
      "@(" +
        labels.map((i) => i.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") +
        ")",
      "g"
    );
    const out: { start: number; end: number; item: MentionItem }[] = [];
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      const item = labels.find((it) => it.label === m![1]);
      if (item) out.push({ start: m.index, end: m.index + m[0].length, item });
    }
    return out;
  };

  // 发送前把 @标签 展开成对应短语（占位符在此时解析成真实绝对路径）
  const expandMentions = (text: string): string => {
    const ranges = computeMentions(text);
    if (!ranges.length) return text;
    const userSpacePath = mySpacePath || workspace?.user_space?.path || "";
    const sessionPath =
      workspace?.agent_space || mentionCfg.root + (activeSessionId || "");
    let out = text;
    for (let k = ranges.length - 1; k >= 0; k--) {
      const r = ranges[k];
      out =
        out.slice(0, r.start) +
        resolveSnippet(r.item.snippet, { sessionPath, userSpacePath }) +
        out.slice(r.end);
    }
    return out;
  };

  // 镜像层：把命中 mention 的 @标签 渲染成微微高亮的 chip。
  // 注意：chip 文本与 textarea 里那段 @标签 完全一致（只多一层底色），
  // 所以不会产生宽度错位，光标始终对得齐。
  const renderBackdrop = (text: string): ReactNode => {
    const ranges = computeMentions(text);
    if (!ranges.length) return text;
    const nodes: ReactNode[] = [];
    let last = 0;
    let i = 0;
    for (const r of ranges) {
      if (r.start > last)
        nodes.push(<span key={`t${i++}`}>{text.slice(last, r.start)}</span>);
      nodes.push(
        <span key={`c${i++}`} className="rounded bg-brand/15 px-px text-brand">
          {text.slice(r.start, r.end)}
        </span>
      );
      last = r.end;
    }
    if (last < text.length)
      nodes.push(<span key={`t${i++}`}>{text.slice(last)}</span>);
    return nodes;
  };

  /** 根据光标前的文本判断是否处于 `@查询` 状态，并记下 `@` 的位置 */
  const updateMention = (val: string, caret: number) => {
    const before = val.slice(0, caret);
    // `@` 前面是行首或非字母/数字即触发（中文、标点后也能唤出）；
    // 前面是字母/数字则不触发，避免误伤邮箱 / URL（如 a@b.com）。
    const m = before.match(/(^|[^A-Za-z0-9])@([^\s@]*)$/);
    if (m) {
      setMention({ start: caret - m[2].length - 1, query: m[2] });
      setActiveMention(0);
    } else {
      setMention(null);
    }
  };

  const onInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setInput(val);
    updateMention(val, e.target.selectionStart ?? val.length);
  };

  // 选中后插入「@标签」原文（不在此展开）：输入框里看到的是高亮 chip，
  // 真正展开成提示词发生在发送时（expandMentions）。
  const selectMention = (item: MentionItem) => {
    if (!mention || !item) {
      setMention(null);
      return;
    }
    const el = textareaRef.current;
    const caret = el?.selectionStart ?? input.length;
    const insert = "@" + item.label;
    const next = input.slice(0, mention.start) + insert + input.slice(caret);
    setInput(next);
    setMention(null);
    const pos = mention.start + insert.length;
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(pos, pos);
    });
  };

  const onInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention && filteredMentions.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveMention((i) => Math.min(i + 1, filteredMentions.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveMention((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        selectMention(filteredMentions[activeMention]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMention(null);
        return;
      }
    }
    // 非候选项态下：退格/删除「紧挨着 mention chip」时整块删掉 @标签（而不是逐字删）
    if (!mention) {
      const el = textareaRef.current;
      const caret = el?.selectionStart ?? input.length;
      if (caret === (el?.selectionEnd ?? caret)) {
        const ranges = computeMentions(input);
        if (e.key === "Backspace") {
          const r = ranges.find((x) => x.end === caret);
          if (r) {
            e.preventDefault();
            const next = input.slice(0, r.start) + input.slice(r.end);
            setInput(next);
            requestAnimationFrame(() => {
              el?.focus();
              el?.setSelectionRange(r.start, r.start);
            });
            return;
          }
        } else if (e.key === "Delete") {
          const r = ranges.find((x) => x.start === caret);
          if (r) {
            e.preventDefault();
            const next = input.slice(0, r.start) + input.slice(r.end);
            setInput(next);
            requestAnimationFrame(() => {
              el?.focus();
              el?.setSelectionRange(r.start, r.start);
            });
            return;
          }
        }
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };
  /**
   * 已上传、还没发出去的附件。
   *
   * 为什么单独一份 state 而不只靠输入框里的「【文件：xxx】」文本：文本是给**人看**
   * 的，这份是给**请求**用的。真发出去时 body 要带结构化的附件列表（含 file_id），
   * 从文本里反解析既脆弱又没必要——删掉那段文字也不该影响要发的东西。
   *
   * 所以两者是「一个动作的两个投影」：上传时一起加，删除时一起删。
   */
  const [pendingFiles, setPendingFiles] = useState<Attachment[]>([]);
  // 从「我的空间」预览"添加"进来的引用片段（文件路径 + 选中内容）
  const [pendingQuotes, setPendingQuotes] = useState<ChatQuote[]>([]);
  useEffect(() => {
    const onAdd = (e: Event) => {
      const q = (e as CustomEvent<ChatQuote>).detail;
      if (!q?.path || !q.text) return;
      setPendingQuotes((prev) =>
        prev.some((p) => p.path === q.path && p.text === q.text)
          ? prev
          : [...prev, q]
      );
    };
    window.addEventListener(ADD_QUOTE_EVENT, onAdd);
    return () => window.removeEventListener(ADD_QUOTE_EVENT, onAdd);
  }, []);
  /** 附件上传中：按钮转圈，并挡住重复点击（同一次会话只让一个在上传） */
  const [uploading, setUploading] = useState(false);
  /** 整块聊天面板是否正处于「拖文件悬停」态（用于显示高亮遮罩） */
  const [isDragging, setIsDragging] = useState(false);
  /** 拖放计数器：子元素会反复触发 dragenter/leave，用计数归零判断真正离开面板 */
  const dragCounterRef = useRef(0);
  /**
   * 正在流式跑着的会话，**按 sessionId 分桶**。
   *
   * 与 `msgsBySession` 用**同一套桶 key**：气泡在哪、消息在哪，必须始终是同一个会话，
   * 否则就会出现「消息落 A、气泡显示 B」。
   *
   * 为什么不照抄 TaskPage 的单个 streamMsg：那边是「同一时刻只有一条」的场景。
   * 这里不同会话可以并发（后端只对同一会话串行），用单个变量的话 A 跑完执行
   * setStreamMsg(null) 会把 B 的气泡一起清掉。故按会话分桶。
   */
  const [streamRuns, setStreamRuns] = useState<Record<string, StreamRun>>({});
  /** 点某条消息用量里的「查看详情」：按需拉该轮时间线（对历史消息同样有效） */
  const [detailMessageId, setDetailMessageId] = useState<string | null>(null);
  /** 点「查看 dump」：看该轮完整 prompt（仅落盘开启时才显示入口） */
  const [dumpMessageId, setDumpMessageId] = useState<string | null>(null);
  /** 当前这次流式运行：点「停止」时用它中断（前端断流 + 通知后端收尾） */
  const abortRef = useRef<AbortController | null>(null);
  const runIdRef = useRef<string>("");
  /** 正在流式的那条占位消息：停止时要就地收尾（补全文案、去掉光标） */
  const placeholderRef = useRef<string>("");
  /** 隐藏的文件选择器：点「+」时才 click() 它。
   *  用 ref 而不是 state 控制显隐：那个 input 从头到尾不可见，没有渲染上的意义，
   *  而 ref 能在不改渲染的前提下拿到它。 */
  const fileInputRef = useRef<HTMLInputElement>(null);
  /**
   * 本页是否正有一条**本地流**在跑（send() 期间为 true）。
   *
   * 为什么需要它：会话探测（下面那个每 3 秒问 /activity 的 effect）在状态变化时
   * 会 ``loadMessages``，而那是**替换**语义——它会把 msgs 整个换成服务端版本。
   * 服务端的 user 消息 id 是后端生成的，与本地 ``activeRun.user``（前端生成的 id）
   * 必然不同，于是 ``visibleMsgs`` 会把两条 user 同时显示出来：**同一条提问出现
   * 两个气泡**。
   *
   * 正常轮次里这个窗口只有 3 秒、且用户从打开会话到发消息通常早已越过首次探测，
   * 所以不容易撞上。但**附件轮次会稳定复现**：上传时后端当场建会话，
   * activeSessionId 从 undefined 变成真实 id → 探测 effect 重新挂载、lastKey
   * 重置 → 3 秒后首次探测就算「变化」→ 立刻 loadMessages，正好落在流式进行中。
   *
   * 用 ref 而不是 state：探测那个 effect 的依赖里刻意不放这些（放了会让它每轮
   * 重启成风暴），所以只能靠 ref 读到实时值。
   */
  const localStreamingRef = useRef(false);
  /* 浏览器页的两个子类别：手动「对这一页提问」的 browser，与网页 Agent 自动操作的
     browser-automation。两者各自成 tab（与「对话」页 chat/task 两个 tab 同款样式），
     各自记住上次看的是哪条会话。
     ⚠ 这两个声明必须放在下面的 remembered 之前：remembered 会在 useState 惰性初始化
     时读 effKind，而 const 有暂时性死区（TDZ）——声明在后面，首屏渲染调用 remembered()
     就会抛 "Cannot access 'effKind' before initialization"，整个 React 树崩掉变白屏
     （tsc/madge 都查不出这种错，只有运行时首屏才暴露）。 */
  const [browserTab, setBrowserTab] =
    useState<"browser" | "browser-automation">("browser");
  /* 当前生效的会话类别：浏览器页随子 tab 切换，其余页恒为 chat（task 视图走另一条
     会话通道，不写这个键）。所有「记住上次会话」的 localStorage 键都按它分桶。 */
  const effKind: SessionKind = browserOnly ? browserTab : (sessionKind ?? "chat");
  /** 上次看的那条会话。侧栏「对话」不再预建会话，所以进页面时没有外部指定的 id，
     只有这台机器上"最近在聊哪个"的记忆；没有就直接进空态（发第一条消息时后端才建）。 */
  const remembered = () =>
    localStorage.getItem(sessionKey(agentId, effKind)) || undefined;
  const [sessionId, setSessionId] = useState<string | undefined>(remembered);
  /** 会话视图选中的会话：与「任务视图」用的任务会话分开保存，避免互相顶掉 */
  const [chatSessionId, setChatSessionId] = useState<string | undefined>(
    remembered
  );
  const qc = useQueryClient();
  /** 可观测：当前会话的累计用量（消息列表里看单轮，这里看整会话总计） */
  const activeSessionId = chatSessionId || sessionId;
  /** 视图正在看的那个桶。与 streamRuns 取同一个 key，两套状态因此不会分叉。 */
  const curBucket = activeSessionId || PENDING_BUCKET;
  /** 当前会话的消息列表；该会话还没加载过时显示欢迎语 */
  const msgs = msgsBySession[curBucket] ?? EMPTY_WITH_WELCOME;
  /**
   * msgs 的最新值快照。
   *
   * 重连的 useEffect 要读「当前 msgs」来定位进行中气泡对应的 user 消息，但不能
   * 把 msgs 放进依赖——否则每来一帧都会把刚建好的重连流拆了重建（表现为过程
   * 反复从零开始）。
   */
  const msgsRef = useRef<ChatMessage[]>(msgs);
  msgsRef.current = msgs;
  /**
   * 只更新指定桶，绝不碰别的会话。
   *
   * 所有写消息的地方都必须走它，且**自己指名道姓**地给出桶 key——这是这次重构的
   * 全部意义：异步回调不再依赖「当前视图在哪」，而是自带归属。
   */
  const updateMsgs = useCallback(
    (bucket: string, fn: (prev: ChatMessage[]) => ChatMessage[]) => {
      setMsgsBySession((all) => ({ ...all, [bucket]: fn(all[bucket] ?? []) }));
    },
    []
  );
  /**
   * 每个会话的分页游标：这一段前面还有没有更早的、以及最早的 seq。
   *
   * 会话可以很长（一条消息一轮，对话上百轮就超过接口单次的 limit=200），而接口只给
   * 最近一段——没有游标的话，更早的历史在界面上就彻底不可达。curBucket 之外的值在切换
   * 会话后依然保留，所以加载过的早消息不会因为切一次会话就白加载。
   */
  const [paging, setPaging] = useState<Record<string, PagingState>>({});
  /** 「加载更早的消息」是否在途（防连点） */
  const [loadingOlder, setLoadingOlder] = useState(false);
  /**
   * 当前视图对应的流式运行。新会话还没有 id，先落在 PENDING_BUCKET 上；
   * 拿到真实 session_id 后 `send` 会把桶搬过去，这里自动跟上。
   *
   * `sending` 由它派生而不是独立 state：这样「A 在跑」不会禁掉 B 的输入框，
   * 而切回 A 时输入框又是禁用的——单一 boolean 做不到这个。
   *
   * 但它只覆盖**本地流**。整页刷新后那条「进行中」气泡是从 msgs 里重建的，
   * 没有对应的桶——所以还要把 msgs 里 streaming 的那条也算进来，否则刷新回来
   * 输入框不被禁用，用户能在上一轮没跑完时又发一条。
   */
  const activeRun = activeSessionId
    ? streamRuns[activeSessionId]
    : streamRuns[PENDING_BUCKET];
  const recoveredRun = !activeRun ? msgs.find((m) => m.streaming) : undefined;
  const sending = !!activeRun?.assistant.streaming || !!recoveredRun;
  /* ── KEEPER_DIAG ──「页面为什么认为在跑」的直接证据：
     sending 有两个来源，分别是桶(activeRun) 和 msgs 里的残留气泡(recoveredRun)，
     打出来就能看出是哪一路，而不必再猜。 */
  diag("state.sid", (activeSessionId || PENDING_BUCKET).slice(0, 10));
  diag("state.activeRun", activeRun
    ? { runId: (activeRun.runId || "").slice(0, 8), resumed: !!activeRun.resumed, streaming: !!activeRun.assistant.streaming }
    : null);
  diag("state.recoveredRun", recoveredRun
    ? { id: String(recoveredRun.id).slice(0, 22), runId: (recoveredRun.runId || "").slice(0, 8) }
    : null);
  diag("state.sending", !!sending);
  /**
   * 需要**重连**的 run_id：来自刷新后重建的「进行中」气泡（后端注册表带回来的）。
   *
   * 本地发起的流不需要——run_id 就在 send() 的闭包里，流本身也还活着。所以这里
   * 只在没有活跃桶时才取值：一个稳定的字符串，正好当 useEffect 的依赖。
   */
  const recoverRunId = !activeRun
    ? msgs.find((m) => m.streaming && m.runId)?.runId || null
    : null;
  const sessionUsageQ = useQuery({
    queryKey: ["session-usage", activeSessionId],
    queryFn: () => apiSessionMetrics(activeSessionId as string),
    enabled: !!activeSessionId,
  });
  const sessionUsage = sessionUsageQ.data ?? null;
  /** 预算 / 配额：上限用环境变量配置，为 0（未配置）时不展示 */
  const budgetQ = useQuery({
    queryKey: ["session-budget", activeSessionId],
    queryFn: () => apiBudget({ session_id: activeSessionId as string }),
    enabled: !!activeSessionId,
  });
  const budget = budgetQ.data;
  /** 落盘开关（与设置页共用缓存）：没开就不显示「查看 dump」入口 */
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: apiGetSettings,
  });
  const dumpEnabled = !!settingsQ.data?.prompt_dump?.enabled;

  // 新会话（未建 session）时待选的用户空间；"" = 默认自动目录
  const [userSpaceId, setUserSpaceId] = useState<string>("");
  // 当前会话生效的工作空间（已有会话才拉取）
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  // 思考模式（新会话待选值）：模型是否"先想再答"，及其强度。已有会话的值从
  // workspace 读回（见 loadWorkspace），这里只承载"还没建会话时"的待选状态。
  const [thinkingMode, setThinkingMode] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("low");
  // 运行模式（新会话待选值）：普通/快捷/程序化。已有会话从 workspace.mode 读回。
  const [runMode, setRunMode] = useState<RunMode>("normal");
  const listRef = useRef<HTMLDivElement>(null);

  // 工作模式：会话视图 / 任务视图（合并原「会话模式」「任务模式」）
  //
  // 浏览器页（sessionKind="browser"）不使用这个开关：那一页只列浏览器会话，
  // 切到 task 会列出一列表不属于这里的任务，而在 send() 里还会把浏览器会话的
  // 历史混进来。所以那边根本不渲染切换控件，mode 恒为 "chat"。
  const [mode, setMode] = useState<"chat" | "task">("chat");
  // 工作台：关 / 编码工作台（文件浏览 + 编辑器）/ Agent 浏览器 —— 三选一，可叠加在左侧
  const [workbenchType, setWorkbenchType] = useState<WorkbenchType>("off");
  const [drawerOpen, setDrawerOpen] = useState(false);
  // 会话浮层动画：mounted 控制挂载，closing 控制播放退场动画
  const [drawerMounted, setDrawerMounted] = useState(false);
  const [drawerClosing, setDrawerClosing] = useState(false);
  useEffect(() => {
    if (drawerOpen) {
      setDrawerClosing(false);
      setDrawerMounted(true);
    } else {
      setDrawerClosing(true);
      const t = setTimeout(() => setDrawerMounted(false), 360);
      return () => clearTimeout(t);
    }
  }, [drawerOpen]);
  const closeDrawer = () => setDrawerOpen(false);
  const [activeFilePath, setActiveFilePath] = useState<string | null>(null);
  // 保存文件后自增，触发文件浏览器刷新 git 角标
  const [explorerRefresh, setExplorerRefresh] = useState(0);
  // 编码工作台：文件浏览器宽度（内部可拖拽）
  const [explorerWidth, setExplorerWidth] = useState(248);
  // 工作台宽度（与主区域间可拖拽）。三种形态共用这一份宽度：从编码切到浏览器时
  // 右侧边界必须停在原处，否则切换动作本身会让内容区宽度变化，看着像"页面跳了"
  const [workbenchWidth, setWorkbenchWidth] = useState(560);
  const resizeWorkbench = useCallback(
    (dx: number) =>
      setWorkbenchWidth((w) => Math.max(360, Math.min(1100, w + dx))),
    []
  );
  // Agent 配置抽屉（入口在顶栏右侧）
  const [configOpen, setConfigOpen] = useState(false);

  // 用户空间列表（选择器用）
  const usQ = useQuery({ queryKey: ["user-spaces"], queryFn: apiListUserSpaces });
  const userSpaces: UserSpace[] = usQ.data?.user_spaces ?? [];

  // 「我的空间」路径（来自 settings.yaml，单空间）：@我的空间 mention 解析用
  const mySpaceQ = useQuery({ queryKey: ["my-space"], queryFn: apiGetMySpace });
  const mySpacePath = mySpaceQ.data?.path ?? "";

  // 部署形态：本机才显示「打开文件夹」（远程部署只支持下载）
  const deployQ = useQuery({
    queryKey: ["deploy-info", agentId],
    queryFn: () => apiDeployInfo(agentId),
  });
  const isLocal = deployQ.data?.local ?? true;

  // 稳定引用：这两个回调会传给 memo 化的 ChatBubble，写成内联箭头函数的话
  // 每次渲染都是新引用，memo 会被直接打掉（长会话输入卡顿的元凶之一）。
  const openUsageDetail = useCallback((mid: string) => setDetailMessageId(mid), []);
  const openUsageDump = useCallback((mid: string) => setDumpMessageId(mid), []);

  /**
   * 加载更早的消息。
   *
   * 接口单次只给最近一段（limit 条），会话长了更早的历史就取不到——这个按钮往前翻
   * 一页，并把结果**前插**进该会话的桶（不是替换），所以已经加载过的都还在。
   *
   * 最容易做砸的是**视线**：内容插在顶部，浏览器会保持 scrollTop 不变，于是用户正
   * 看的那条消息被顶到视口外去了——表现为一加载就"跳走"。所以要在插入后把 scrollTop
   * 加上新增的那段高度，把原视口钉回原处。
   */
  const loadOlder = useCallback(async () => {
    const bucket = curBucket;
    const st = paging[bucket];
    if (loadingOlder || !st?.hasMore || st.oldestSeq == null) return;
    setLoadingOlder(true);
    const el = listRef.current;
    const prevHeight = el?.scrollHeight ?? 0;
    const prevTop = el?.scrollTop ?? 0;
    try {
      const resp = await apiSessionMessages(agentId, bucket, 200, st.oldestSeq);
      const older = (resp.messages || []).filter((m) => m.id !== "welcome");
      updateMsgs(bucket, (prev) => {
        const hasWelcome = prev[0]?.id === "welcome";
        const rest = hasWelcome ? prev.slice(1) : prev;
        const seen = new Set(rest.map((m) => m.id));
        // 去重是必须的：翻页边界可能与已加载的有重叠（这一轮新消息把 seq 顶掉了）
        const add = older.filter((m) => !seen.has(m.id)).map((m) => ({
          id: m.id,
          role: m.role,
          text: m.content,
          created_at: m.created_at ?? null,
          artifacts: m.artifacts,
          steps: m.steps ?? [],
          // 用户上传的附件：后端原样带回（含 file_id / cache_path），前端据此
          // 在气泡里渲染缩略图并支持预览。漏掉这一项，历史里就只能看到文字。
          attachments: m.attachments ?? [],
          usage: m.usage ?? null,
          duration_ms: m.duration_ms ?? null,
          llm: m.role === "assistant",
          // @ 快捷项展示元数据：回读后让气泡把 @标签 渲染成高亮 chip（刷新后仍保留）
          mentionRaw: m.mention_raw,
          mentions: m.mentions ?? undefined,
        }));
        return add.length ? [welcomeMsg, ...add, ...rest] : prev;
      });
      setPaging((prev) => ({
        ...prev,
        [bucket]: {
          hasMore: resp.has_more ?? false,
          oldestSeq: resp.oldest_seq ?? null,
        },
      }));
      // 钉回原视口：顶部多出来多少像素，scrollTop 就补多少
      requestAnimationFrame(() => {
        const el2 = listRef.current;
        if (el2) el2.scrollTop = prevTop + (el2.scrollHeight - prevHeight);
      });
    } catch {
      // 翻页失败不该打断使用：游标不变，用户可以再点一次
      toast.error("加载更早的消息失败，请稍后重试");
    } finally {
      setLoadingOlder(false);
    }
  }, [agentId, curBucket, paging, loadingOlder, updateMsgs]);

  /** 拉取某会话的历史消息 */
  const loadMessages = useCallback(
    async (sid: string) => {
      try {
        const resp = await apiSessionMessages(agentId, sid);
        const list = resp.messages || [];
        const pending = resp.pending;
        updateMsgs(
          sid,
          () =>
            list.length
              ? [
                welcomeMsg,
                ...list.map((m) => {
                const item: ChatMessage = {
                  id: m.id,
                  role: m.role,
                  text: m.content,
                  created_at: m.created_at ?? null,
                  artifacts: m.artifacts,
                  steps: m.steps ?? [],
                  // 用户上传的附件：后端原样带回，气泡据此渲染缩略图。漏掉则历史里
                  // 只能看到文字（之前就是因为这个，图片始终不进会话记录）。
                  attachments: m.attachments ?? [],
                  // 可观测：历史消息的用量 / 耗时（刷新后仍要能看到）
                  usage: m.usage ?? null,
                  duration_ms: m.duration_ms ?? null,
                  // @ 快捷项展示元数据：刷新后气泡仍渲染成高亮 chip
                  mentionRaw: m.mention_raw,
                  mentions: m.mentions ?? undefined,
                  // 历史消息后端不带 llm 字段（session_messages 也没存这一列），
                  // 会话里的 assistant 回复本就是 agent 生成的，按 role 补上，
                  // 否则 ChatBubble 的「AI 生成」标签不会显示。
                  llm: m.role === "assistant",
                  // 后端合成的「本轮进行中」气泡：整页刷新后靠它还能看到
                  // 「已执行 N 步」，而不是一片空白。
                  streaming: m.streaming,
                  // 后端带来的 run_id：刷新后「停止生成」靠它（见 chat/runs.py）
                  runId: m.in_progress_run_id,
                  // 这条气泡是哪条 user 消息发起的：重连时靠它把 user 消息放进桶
                  inProgressFor: m.in_progress_for,
                };
                  if (
                    pending &&
                    pending.message_id &&
                    m.id === pending.message_id
                  ) {
                    item.waiting = true;
                    item.stepId = pending.step_id;
                    item.options = pending.options ?? undefined;
                  }
                  return item;
                }),
              ]
            : [welcomeMsg]
        );
        // 记下分页游标：接口这次只给了最近一段，前面还有没有、要往哪翻都在这
        setPaging((prev) => ({
          ...prev,
          [sid]: {
            hasMore: resp.has_more ?? false,
            oldestSeq: resp.oldest_seq ?? null,
          },
        }));
      } catch {
        // 拉不到历史就**保留现有 msgs**，绝不整屏清空。
        //
        // 这里原本无条件 ``setMsgs([welcomeMsg])``：一次瞬时失败（后端刚重启 /
        // 网络抖动 / 偶发 500）就把用户眼前所有历史抹成一句欢迎语，表现为
        // 「会话记录全没了」——而真实数据好好躺在库里。切走再切回来时最容易撞上，
        // 因为那一刻往往正好是服务在忙。
        //
        // 拿不到就是拿不到，宁可留着上一次渲染的内容，也不能拿空值覆盖它。
        console.warn("[ChatPage] 加载历史消息失败，保留现有消息不覆盖");
      }
    },
    [agentId, updateMsgs]
  );

  /** 拉取某会话生效的工作空间 */
  const loadWorkspace = useCallback(
    async (sid: string) => {
      try {
        setWorkspace(await apiSessionWorkspace(agentId, sid));
      } catch {
        setWorkspace(null);
      }
    },
    [agentId]
  );

  /**
   * **「有没有开了一轮新的」探测**——让浮球发起的对话也能在本页实时出现。
   *
   * 问题
   * ----
   * 下面那个 3 秒轮询、以及 SSE 重连，都门控在「页面已经知道有这一轮」上：
   * 前者要 ``msgs`` 里已有进行中气泡，后者要 ``recoverRunId``（同样来自
   * ``load_messages`` 合成的那个气泡）。而那个气泡的判据是 ``active_run_id``。
   *
   * 页面自己发起的轮次天然满足，于是两套都能启动。但**浮球发起的那一轮，页面完全
   * 不知情**——两套都不启动，用户只能手动刷新去触发一次 ``load_messages``。
   *
   * 做法
   * ----
   * 每 3 秒问一次 ``/activity``（纯内存查表，几十字节，不打库），**只在
   * ``running`` 或 ``run_id`` 变化时**才 ``loadMessages`` 一次。变化时重新拉消息
   * 会拿到新的用户消息 + 进行中气泡，于是下面那套轮询和重连自然接手。
   *
   * 为什么用「变化才拉」而不是无条件轮询消息：无条件拉一次是 200 条消息的重量，
   * 每 3 秒一次、而绝大多数时候什么都不会变。
   *
   * 只盯**当前打开的会话**：别的会话起了轮次，页面不跳走——用户正在看的东西不该
   * 被别处的事抢走。
   */
  useEffect(() => {
    const sid = activeSessionId;
    if (!sid) return;
    let stop = false;
    let lastKey: string | null = null;

    const tick = async () => {
      while (!stop) {
        await new Promise((r) => setTimeout(r, 3000));
        if (stop) return;
        try {
          const r = await apiSessionActivity(agentId, sid);
          if (stop) return;
          const key = `${r.running ? "1" : "0"}:${r.run_id}`;
          /* ── KEEPER_DIAG ── 每次探测都打（有节流），看内核到底怎么答的 */
          diag("activity", { running: !!r.running, runId: (r.run_id || "").slice(0, 8), key, lastKey });
          // 第一次拿到也当作变化：那正好覆盖"页面打开时那一轮已经在跑"的情况
          if (key !== lastKey) {
            lastKey = key;
            /* 本地流在跑时**不拉消息**：loadMessages 是替换语义，会把服务端的
               user 消息（后端 id）和本地 activeRun.user（前端 id）同时摆上台，
               变成"同一条提问两个气泡"。流式那几秒不值得为它冒这个险——
               过程本来就有流式桶在实时喂。 */
            if (localStreamingRef.current) continue;
            await loadMessages(sid);
            /* 内核**明确说**这个会话没有在跑了：任何"进行中"气泡都是过期的，清掉。
             *
             * 为什么必须在这里再清一次：`loadMessages` 里那套"按 runId 比对"的
             * 清除只在**轮询**里跑，而轮询有两个提前退出的条件——
             *   · activeRun 非空（重连建起了桶）就直接 return；
             *   · 当前没有任何 streaming 气泡也直接 return。
             * 于是残留气泡一旦落到这些路径之外，就再也碰不到清除逻辑，页面会一直
             * 挂着「正在思考」，哪怕内核那一轮早就结束、回答也早已落库。
             *
             * 用内核的 run 注册表当判据，比"服务端这次没合成气泡"硬得多：
             * 后者依赖气泡带着 runId，而它不是每条都有。 */
            if (!r.running) {
              /* ── KEEPER_DIAG ── 清之前先记一笔，看当时到底有没有残留 */
              diag("clear.before", {
                sid: sid.slice(0, 10),
                hadStreamingInMsgs: (msgsRef.current || []).some((m) => m.streaming),
              });
              updateMsgs(sid, (prev) =>
                prev.some((m) => m.streaming) ? prev.filter((m) => !m.streaming) : prev
              );
              /* 连**重连的桶**一起拆。
               *
               * 只清 msgs 是不够的：`visibleMsgs` 优先显示 streamRuns 里的桶，桶
               * 在的话页面照样挂着「正在思考」，而 msgs 那份清得再干净也没用。
               *
               * 只拆 resumed 的桶：本页自己发起的流即便此刻服务端还没登记上 run，
               * 那也是**真的在跑**，一刀切会把用户正在进行的那一轮的本地显示掐掉。
               * 重连的桶则相反——它依赖服务端那一轮还活着，服务端说结束了它就是
               * 过期数据，没有保留的理由。 */
              setStreamRuns((prev) => {
                const run = prev[sid];
                if (!run || !run.resumed) return prev;
                const next = { ...prev };
                delete next[sid];
                return next;
              });
            }
          }
        } catch {
          /* 网络抖动：下一轮自然重试 */
        }
      }
    };
    void tick();
    return () => {
      stop = true;
    };
    // 只依赖当前会话：换会话等于换探测目标。刻意不进 loadMessages——
    // 它每次渲染都是新函数，进依赖会让这个循环每轮自己重启成风暴。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, activeSessionId]);

  /**
   * 「本轮进行中」的轮询：整页刷新 / 重新进页面时用。
   *
   * 背景：客户端断开 SSE 后后端**不再杀 agent**（见 chat/api._sse_pump），它会
   * 继续跑完并落库。但刷新时这一轮还没写 assistant 消息，前端只能靠
   * ``load_messages`` 合成的「进行中」气泡显示已完成的步骤——那个快照是静态的，
   * 不轮询就永远停在刷新那一瞬间。
   *
   * 只在**当前会话**有进行中气泡时轮询，一结束就停（3s 一次，够跟手也不打扰）。
   * 不用 react-query：这是高频推送，走它反而要把缓存策略调得很别扭。
   */
  useEffect(() => {
    const sid = activeSessionId;
    if (!sid) return;
    // 已经有活流在喂这个会话（本地流，或重连成功建起的桶）：再轮询就是白白每 3s
    // 全量查库。这一句同时也是「重连成功后自动停掉轮询」的开关。
    if (activeRun) {
      /* ── KEEPER_DIAG ── 这一句是「残留清不掉」的头号嫌疑人：
         桶在就直接 return，轮询整套不跑，而清除逻辑全在轮询里。 */
      diag("poll.skip.reason", "activeRun");
      return;
    }
    // 只有存在「进行中」的气泡才需要轮询
    if (!msgs.some((m) => m.streaming)) {
      diag("poll.skip.reason", "no-streaming");
      return;
    }
    diag("poll.skip.reason", "run");
    let stop = false;
    const tick = async () => {
      await new Promise((r) => setTimeout(r, 3000));
      if (stop) return;
      try {
        const resp = await apiSessionMessages(agentId, sid);
        if (stop) return;
        const next = resp.messages || [];
        // 轮询只拿「最近这一段」，绝不能整段替换：用户点过「加载更早的消息」之后，
        // 那些更早的历史已经进了这个桶，替换会把它们全冲掉（而且 next 永远只有
        // 最近 200 条，冲掉之后再也回不来）。
        //
        // 所以这里只做两件事：同 id 的用新值覆盖（气泡正文 / steps 会变），真正新增的
        // （这一轮刚写的消息）追加到末尾——最新的消息永远该在最后。
        updateMsgs(sid, (prev) => {
          const hasWelcome = prev[0]?.id === "welcome";
          const rest = hasWelcome ? prev.slice(1) : prev;
          const pos = new Map(rest.map((m, i) => [m.id, i]));
          const added: ChatMessage[] = [];
          for (const m of next) {
            const item: ChatMessage = {
              id: m.id,
              role: m.role,
              text: m.content,
              created_at: m.created_at ?? null,
              artifacts: m.artifacts,
              steps: m.steps ?? [],
              // 用户上传的附件：后端原样带回，气泡据此渲染缩略图。
              attachments: m.attachments ?? [],
              usage: m.usage ?? null,
              duration_ms: m.duration_ms ?? null,
              llm: m.role === "assistant",
              streaming: m.streaming,
              runId: m.in_progress_run_id,
            };
            const at = pos.get(m.id);
            if (at === undefined) added.push(item);
            else rest[at] = item;
          }
          /* 丢掉"服务端已经不再合成"的进行中气泡。
           *
           * 那个气泡的 id 是 `inprogress-<用户消息id>`（见 load_messages），而轮次
           * 跑完后服务端**不再合成它**，取而代之的是一条 id 完全不同的真实回答。
           * 所以上面那段"同 id 覆盖 + 新增"永远碰不到它——它会带着 streaming=true
           * 一直留在页面上，表现为**永久的「正在思考」**。
           *
           * 判据要窄一点：只在服务端对**同一个 runId** 也不再说"在跑"时才丢。
           * 否则会误删本页自己发起、正在实时跑的那一轮——那种情况下服务端可能刚好
           * 还没合成出气泡（第一步都没跑完时就没有），而页面本地正依赖它显示进度。 */
          const serverStreamingRuns = new Set(
            next.filter((m) => m.streaming && m.in_progress_run_id)
              .map((m) => m.in_progress_run_id as string)
          );
          const kept = rest.filter(
            (m) => !(m.streaming && m.runId && !serverStreamingRuns.has(m.runId))
          );
          const merged = [...kept, ...added];
          return hasWelcome ? [welcomeMsg, ...merged] : merged;
        });
        // 还没结束就继续轮
        if (next.some((m) => m.streaming)) tick();
      } catch {
        /* 网络抖动：下一轮自然重试 */
      }
    };
    void tick();
    return () => {
      stop = true;
    };
    // 依赖 activeSessionId 与「是否有进行中气泡」；刻意不进 msgs——
    // 每轮都会改 msgs，进依赖会导致轮询自己把自己重启成风暴。
    // !!activeRun 是布尔，稳定：只用来在「重连成功」后把轮询关掉。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, activeSessionId, msgs.some((m) => m.streaming), !!activeRun]);

  /**
   * **重连**：整页刷新 / 断网后把正在跑的那一轮「接回来」。
   *
   * SSE 是「一条 TCP 上的 HTTP 长连接」，刷新后它被浏览器关掉，协议上不存在把旧
   * 连接接回去的办法。但服务端 `chat/hub.py` 给这一轮留了内存缓冲——用同一
   * run_id 订阅 `chat/stream/resume`：先收到 `catchup`（刷新前已发生的步骤和正文，
   * 页面立刻恢复到该有的样子），再接上实时流，观感与没刷新过一致。
   *
   * 这正是老方案「合成气泡 + 轮询」补不上的一环：它只能看到步骤，正文要等整轮
   * 跑完落库才一次性出现（中间那几分钟正文区是空的）。
   *
   * 收到 `gone`（服务端无从回放：进程重启过 / 缓冲已回收）就拆桶退回轮询——
   * 那是正确降级，不是错误。
   */
  useEffect(() => {
    const sid = activeSessionId;
    if (!sid || !recoverRunId) return;
    const runId = recoverRunId;
    const bucketKey = sid;
    let stopped = false;
    let raf = 0;
    const controller = new AbortController();
    /* ── KEEPER_DIAG ── 重连是否被触发、用的是哪个 run_id */
    diag("resume.enter", { sid: sid.slice(0, 10), runId: (runId || "").slice(0, 8) });

    // 定位这一轮的 user 消息：桶里那条会按 id 覆盖 msgs 里的同名消息，
    // id 拿错的话去重失效，页面上会多出一条空气泡。
    const list = msgsRef.current;
    const target = list.find((m) => m.streaming && m.runId === runId);
    if (!target) return;
    const userForRun =
      list.find((m) => m.id === target.inProgressFor) ||
      list.slice(0, list.indexOf(target)).reverse().find((m) => m.role === "user");

    // 建桶：assistant 直接用恢复态气泡的 id，visibleMsgs 按 id 去重后不会重复
    setStreamRuns((prev) => ({
      ...prev,
      [bucketKey]: {
        user: userForRun || { id: `${runId}-user`, role: "user", text: "" },
        assistant: { ...target },
        runId,
        abort: controller,
        resumed: true,
      },
    }));

    /** 收尾：把权威结果写回该会话的消息列表（换成服务端 message_id）并删桶 */
    const settle = (p: Partial<ChatMessage>) => {
      if (stopped) return;
      stopped = true;
      updateMsgs(bucketKey, (prev) =>
        prev.map((m) => (m.id === target.id ? { ...m, ...p, streaming: false } : m))
      );
      setStreamRuns((prev) => {
        if (!prev[bucketKey]) return prev;
        const next = { ...prev };
        delete next[bucketKey];
        return next;
      });
      qc.invalidateQueries({ queryKey: ["session-usage", sid] });
    };

    let acc = target.text || "";
    const flushDelta = () => {
      raf = 0;
      setStreamRuns((prev) => {
        const run = prev[bucketKey];
        if (!run) return prev;
        return {
          ...prev,
          [bucketKey]: { ...run, assistant: { ...run.assistant, text: acc } },
        };
      });
    };

    void apiResumeChatStream(
      agentId,
      runId,
      {
        onCatchup: (data) => {
          /* ── KEEPER_DIAG ── 服务端快照的关键字段：finished/done/error */
          diag("resume.catchup", {
            finished: !!data.finished,
            done: data.done ? { message_id: String(data.done.message_id || "").slice(0, 22), canceled: !!data.done.canceled } : null,
            error: data.error || null,
            runId: (data.run_id || "").slice(0, 8),
          });
          // 回放：直接覆盖。服务端这份是权威，且比「刷新瞬间查库得到的快照」新
          acc = data.text || "";
          setStreamRuns((prev) => {
            const run = prev[bucketKey];
            if (!run) return prev;
            return {
              ...prev,
              [bucketKey]: {
                ...run,
                assistant: {
                  ...run.assistant,
                  steps: data.steps ?? run.assistant.steps ?? [],
                  text: data.text ?? "",
                },
              },
            };
          });
          if (!data.finished) return;
          // 重连时这一轮已经跑完了：catchup 自带终态
          const d = data.done;
          if (d) {
            settle({
              id: d.message_id || target.id,
              text: d.canceled
                ? d.answer || acc || "（已停止生成）"
                : d.answer || "",
              llm: d.llm,
              steps: d.steps,
              artifacts: d.artifacts,
              waiting: d.waiting_human,
              stepId: d.step_id,
              usage: d.usage ?? null,
              duration_ms: d.duration_ms ?? null,
              options: d.options,
              canceled: d.canceled,
            });
          } else {
            settle({
              text: data.error ? "执行失败。" : acc,
              error: data.error || undefined,
            });
          }
        },
        onStep: (s) =>
          setStreamRuns((prev) => {
            const run = prev[bucketKey];
            if (!run) return prev;
            return {
              ...prev,
              [bucketKey]: {
                ...run,
                assistant: {
                  ...run.assistant,
                  steps: [...(run.assistant.steps ?? []), s],
                },
              },
            };
          }),
        onDelta: (t) => {
          acc += t;
          if (!raf) raf = requestAnimationFrame(flushDelta);
        },
        onDone: (d) => {
          /* ── KEEPER_DIAG ── 走到这里说明轮次收到终态，桶会被拆 */
          diag("resume.done", { message_id: String(d.message_id || "").slice(0, 22), canceled: !!d.canceled });
          return settle({
            id: d.message_id || target.id,
            text: d.canceled ? d.answer || acc || "（已停止生成）" : d.answer || "",
            llm: d.llm,
            steps: d.steps,
            artifacts: d.artifacts,
            waiting: d.waiting_human,
            stepId: d.step_id,
            usage: d.usage ?? null,
            duration_ms: d.duration_ms ?? null,
            options: d.options,
            canceled: d.canceled,
          });
        },
        onError: (msg) => {
          diag("resume.error", String(msg || "").slice(0, 120));
          return settle({ text: "执行失败。", error: msg });
        },
        onGone: () => {
          /* ── KEEPER_DIAG ── **重点嫌疑**：gone 时只拆桶、不 settle，
             于是 msgs 里那条合成气泡仍带 streaming=true，而 recoverRunId 正是从它
             取的 —— 这会反复触发 resume，形成「永久正在思考」。 */
          diag("resume.gone", { sid: bucketKey.slice(0, 10), runId: (runId || "").slice(0, 8) });
          // 服务端无从回放：拆桶退回轮询。**不要 settle**——那一轮还在跑，
          // 只是我们看不到过程而已；标成终态会让用户以为它结束了。
          stopped = true;
          setStreamRuns((prev) => {
            if (!prev[bucketKey]) return prev;
            const next = { ...prev };
            delete next[bucketKey];
            return next;
          });
        },
      },
      controller.signal
    );

    return () => {
      stopped = true;
      if (raf) cancelAnimationFrame(raf);
      try {
        controller.abort();
      } catch {
        /* ignore */
      }
    };
  }, [agentId, activeSessionId, recoverRunId, qc, updateMsgs]);

  useEffect(() => {
    const sid = localStorage.getItem(sessionKey(agentId, effKind));
    if (sid) {
      loadMessages(sid);
      loadWorkspace(sid);
    }
  }, [agentId, loadMessages, loadWorkspace]);

  /**
   * 实际渲染的消息列表 = 已落库的 msgs + 当前会话正在流式的那两条。
   *
   * 去重是必要的：流式期间用户消息**同时**存在于 msgs 和桶里（前者是本地先插的、
   * 后者保证被 loadMessages 抹掉后还能补回），按 id 去重后只留一份。
   * 用桶里的版本覆盖同名消息，保证内容最新。
   */
  const visibleMsgs = activeRun
    ? [
        ...msgs.filter(
          (m) => m.id !== activeRun.user.id && m.id !== activeRun.assistant.id
        ),
        activeRun.user,
        activeRun.assistant,
      ]
    : msgs;

  // 消息更新、发送中，或在会话 / 任务视图间切换回来时，都停在最新一条。
  // 依赖 activeRun：流式期间步骤挂在它上面，只盯 msgs 的话不会跟着往下滚。
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, sending, mode, activeRun]);

  // 切换会话时清空已打开的文件（该会话工作空间变了）
  useEffect(() => {
    setActiveFilePath(null);
  }, [sessionId]);

  /**
   * 消息头显示的 agent 名。
   *
   * 从已装载列表里查（那个 query 在「概览」页已经拉过，命中缓存不会再发请求）；
   * 查不到就退化成 agentId 前 8 位——总比显示「Agent」强，至少能对上号。
   */
  const agentsQ = useQuery({ queryKey: ["agents"], queryFn: apiListAgents });
  const agentName =
    agentsQ.data?.agents.find((a) => a.id === agentId)?.name?.trim() ||
    agentId.slice(0, 8);

  const handleSelect = (sid: string) => {
    setSessionId(sid);
    setChatSessionId(sid);
    setUserSpaceId("");
    localStorage.setItem(sessionKey(agentId, effKind), sid);
    loadMessages(sid);
    loadWorkspace(sid);
  };

  const handleNew = () => {
    setSessionId(undefined);
    setChatSessionId(undefined);
    setUserSpaceId("");
    setWorkspace(null);
    // 思考待选值也回到出厂（关 + 低），别让上一条会话的选择粘到新会话上
    setThinkingMode(false);
    setReasoningEffort("low");
    setRunMode("normal");
    localStorage.removeItem(sessionKey(agentId, effKind));
    // 清掉「还没拿到真实 session_id 时用的那个桶」：里面可能躺着上一段没绑定到
    // 会话上的对话，新建后不该再冒出来。已落库的历史不受影响（各会话自己的桶照旧）。
    //
    // 必须是**删掉这个键**而不是置成空数组：``msgs ?? EMPTY_WITH_WELCOME`` 只在
    // undefined 时兜底，空数组是有效值，会把欢迎语挡掉——新建会话就变成一片空白。
    setMsgsBySession((all) => {
      if (all[PENDING_BUCKET] === undefined) return all;
      const next = { ...all };
      delete next[PENDING_BUCKET];
      return next;
    });
  };

  /** 任务会话：只作为当前生效 session（任务详情 / 工作空间用），
   *  不写入「会话视图」的选择也不落本地存储——否则切回会话会被任务会话顶掉。 */
  const applyTaskSession = (sid: string) => {
    setSessionId(sid);
    loadWorkspace(sid);
  };

  /**
   * 切换会话 / 任务视图：
   * - 回到会话视图：恢复「会话视图」自己的会话（没有则选列表第一条）；
   * - 进入任务视图：选中任务列表第一条（或保持当前任务），只切生效 session。
   * 列表为空则不动，保持「从顶部新建 / 选择」的空态。
   *
   * 会话视图这一支**必须按当前页面的类别拉列表**，而不是无脑恢复记忆里的 id：
   * 「浏览器」页的 mode 同样是 "chat"（那一页没有任务视图），写成常量会在那里
   * 挑一条普通会话选中，表现为「一个浏览器会话都没有，却显示着普通对话的内容」。
   *
   * 顺带用它**校验记忆里的 id**：localStorage 记的是「上次在这页看到哪条」，而那条
   * 可能已被删除、或属于别的类别（键写错、跨页串了）。不校验就拿着一个外类 id 去拉
   * 历史，页面会渲染出另一类会话的内容——一个没有任何报错的串类。校验不通过就把它
   * 忘掉，否则会一直卡在这个不存在的会话上。
   */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (mode === "chat") {
          // 关键：任务会话不能顶掉会话视图的会话，回来时按 chatSessionId 恢复。
          // 浏览器页有 browser / browser-automation 两个子 tab，各自记在按 effKind
          // 分的 localStorage 桶里——切 tab 互不串，下面优先恢复「当前子类」上次看的。
          const r = await apiSessions(agentId, undefined, 50, 0, effKind);
          if (cancelled) return;
          const ids = new Set((r.sessions || []).map((s) => s.id));
          const remembered = localStorage.getItem(sessionKey(agentId, effKind)) || undefined;
          if (remembered && ids.has(remembered)) {
            setChatSessionId(remembered);
            setSessionId(remembered);
            await loadMessages(remembered);
            loadWorkspace(remembered);
            return;
          }
          if (chatSessionId && ids.has(chatSessionId)) {
            setSessionId(chatSessionId);
            await loadMessages(chatSessionId);
            loadWorkspace(chatSessionId);
            return;
          }
          // 记着的那条已经不属于这一类：清掉，否则刷新一次就被它带进一个空页面
          localStorage.removeItem(sessionKey(agentId, effKind));
          setChatSessionId(undefined);
          const first = r.sessions?.[0];
          if (first?.id) handleSelect(first.id);
        } else {
          const r = await apiListTasks(agentId);
          const tasks = r.tasks ?? [];
          if (!tasks.length) return; // 无任务：保持空态
          // 当前 session 已对应某个任务则保持不变
          if (sessionId && tasks.some((t) => t.session_id === sessionId)) return;
          const first = tasks[0];
          if (!cancelled && first.session_id) applyTaskSession(first.session_id);
        }
      } catch {
        // 列表拉取失败不打断主流程，退回手动选择
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    // sessionKind 一并列入：这个 effect 会按它拉不同的列表，不列的话 prop 变了
    // 不会重跑（目前靠路由切换时组件整体卸载掩盖住了，但那是个隐式约定）。
  }, [mode, agentId, sessionKind, browserTab]);

  /**
   * 重新生成：把上一条用户消息重发一遍。
   *
   * 实现上没有走"让后端重放"的路径，而是直接复用 send —— 这轮的 prompt 由
   * 服务端从历史重算，所以重发同一句话得到的就是重新生成的效果。代价是会在
   * 历史里多留一条用户消息，但比起再加一个"只重跑不回话"的接口，这个取舍更划算。
   */
  const regenerate = useCallback(
    (messageId: string) => {
      const list = visibleMsgs;
      const idx = list.findIndex((m) => m.id === messageId);
      if (idx < 0) return;
      // 往上找最近的一条用户消息
      for (let i = idx - 1; i >= 0; i--) {
        if (list[i].role === "user" && list[i].text) {
          void send(list[i].text);
          return;
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [input, sending]
  );

  /**
   * 选文件 → 上传 → 在输入框留个占位。
   *
   * 两步都做，因为它们服务不同的人：上传是为了拿到 file_id（模型要看），
   * 输入框占位是为了让用户**看见自己加了什么**——后者不能省，否则用户加完
   * 图不知道加没加上，而这一轮可能跑很久才发现模型没看到。
   *
   * 新对话（还没有会话）时后端会当场建一条（session_id 传 "new"），返回的 id
   * 这里立刻接过来：否则附件挂在一个前端不知道的会话上，用户一发消息又会另建。
   */
  const addFiles = async (files: FileList | null) => {
    if (!files || !files.length || uploading) return;
    // 拖放/粘贴会绕过文件选择器的 accept 限制，这里按同一白名单兜底拦截，
    // 否则用户拖个 .pdf 进来，后端不支持时才会报错，体验差一截。
    const ACCEPTED = ["image/png", "image/jpeg", "image/gif", "image/webp"];
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
        if (!ACCEPTED.includes(f.type)) {
          toast.error(`仅支持图片（png/jpg/gif/webp）：${f.name}`);
          continue;
        }
        const { attachment, session_id } = await apiUploadAttachment(
          agentId,
          activeSessionId || "new",
          f
        );
        // 后端新建了会话：接过来，别让附件和消息落到两条不同的会话上
        if (session_id && session_id !== activeSessionId) {
          setSessionId(session_id);
          setChatSessionId(session_id);
          localStorage.setItem(sessionKey(agentId, effKind), session_id);
          loadWorkspace(session_id);
        }
        setPendingFiles((prev) => [...prev, attachment]);
        // 不再往输入框塞「【文件：name】」文本——那会让退格逐字删文件名。
        // 附件统一用下方高亮 chip 展示，点 ✕ 整块删。
      }
    } catch (e: any) {
      // 用 toast 而不是就地红字：上传失败的原因（模型不支持 / 超限 / 类型不对）
      // 是**一次性说明**，占着一块常驻区域反而碍眼。
      toast.error(e?.message || "上传失败");
    } finally {
      setUploading(false);
    }
  };

  /**
   * 移除一个待发送附件：输入框里那段占位一起删掉，两边别各留一半。
   *
   * 按**索引**删而不是按名字：同名文件可能出现多次，按名字删会把第一段删掉，
   * 于是删 B 结果 A 的占位消失了——而 A 还在待发送列表里，用户完全看不出问题。
   * 索引删则占位的第几段与列表的第几条严格对应。
   */
  // 整块聊天面板都是文件投放区：拖拽悬停时高亮，松手即添加附件。
  const dragHasFiles = (e: DragEvent) =>
    Array.from(e.dataTransfer.types).includes("Files");

  const onPanelDragEnter = (e: DragEvent) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragCounterRef.current += 1;
    setIsDragging(true);
  };
  const onPanelDragOver = (e: DragEvent) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };
  const onPanelDragLeave = (e: DragEvent) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current <= 0) {
      dragCounterRef.current = 0;
      setIsDragging(false);
    }
  };
  const onPanelDrop = (e: DragEvent) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragCounterRef.current = 0;
    setIsDragging(false);
    const dropped = e.dataTransfer.files;
    if (dropped && dropped.length) void addFiles(dropped);
  };

  const removeFile = (index: number) => {
    // 占位文本已不进输入框，附件只存在于 pendingFiles；整块删即可，无需再清文本。
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const send = async (raw?: string, stepId?: string) => {
    const rawInput = raw ?? input;
    const q = expandMentions(rawInput).trim();
    // 引用片段：拼成「文件的绝对路径: …\n选中内容」，追加到本轮正文
    const quoteBlock = pendingQuotes.length
      ? pendingQuotes
          .map((qq) => `文件的绝对路径: ${qq.path}\n${qq.text}`)
          .join("\n\n")
      : "";
    const sendText = (q + (q ? "\n\n" : "") + quoteBlock).trim();
    // 气泡展示用：把 @标签 连同它解析出的内容（如绝对路径）一起存下来，供 ChatBubble 渲染成 chip
    const mentionRanges = computeMentions(rawInput);
    const userSpacePath = mySpacePath || workspace?.user_space?.path || "";
    const sessionPath =
      workspace?.agent_space || mentionCfg.root + (activeSessionId || "");
    const mentions = mentionRanges.map((r) => ({
      label: r.item.label,
      resolved: resolveSnippet(r.item.snippet, { sessionPath, userSpacePath }),
    }));
    const mentionRaw = mentionRanges.length ? rawInput : undefined;
    // 仅发附件（不打字）时也允许：去掉了输入框里的「【文件：name】」占位后，
    // 这种消息 q 为空，但 pendingFiles 非空，应当照常发出。
    if ((!q && pendingFiles.length === 0 && pendingQuotes.length === 0) || sending) return;
    // 标记本地正在流式：让那条 3 秒一探的 effect 跳过 loadMessages，
    // 否则它会把刚发上去、还在流式中的这一轮又拉一遍，和服务端回灌的 user 重复显示。
    localStreamingRef.current = true;

    /**
     * 这一轮要发到**哪个会话**：视图当前所在的会话。
     *
     * 这里原先用的是 ``sessionId``，而气泡所在的位置用的是 ``activeSessionId``
     * （= chatSessionId || sessionId）。两者在「会话视图和任务视图各自选过不同会话」
     * 时会分叉——``applyTaskSession`` 只改 sessionId、不动 chatSessionId——于是消息
     * 按 sessionId 落到 A、UI 却按 activeSessionId 显示 B，同一条消息连同它的回答
     * 就同时出现在两个会话里。发消息的目标和气泡的归属必须是同一个。
     */
    const targetSid = activeSessionId;

    setInput("");

    // 本次运行的标识 + 可中断的 fetch：点「停止」时前端立即断流、后端尽快收尾
    const runId = newRunId();
    const controller = new AbortController();
    runIdRef.current = runId;
    abortRef.current = controller;

    // 用户消息也放进桶里：它同样会被 loadMessages 抹掉，而用户发完消息切走再回来
    // 「我刚发的那句话没了」是同一个 bug 的另一半。
    const userMsg: ChatMessage = {
      id: newId(),
      role: "user",
      // 气泡显示用「干净原文」（@标签 由 MentionChips 渲染成 chip，不展开绝对路径）；
      // 发给模型、带路径展开的文本走 apiChatStream 的第二个参数 q，二者分开。
      text: rawInput,
      created_at: new Date().toISOString(),
      mentionRaw,
      mentions: mentions.length ? mentions : undefined,
    };
    const placeholderId = newId();
    placeholderRef.current = placeholderId;
    const assistantMsg: ChatMessage = {
      id: placeholderId,
      role: "assistant",
      text: "",
      steps: [],
      created_at: new Date().toISOString(),
      streaming: true,
    };

    // 桶 key：用**视图当前显示的**会话（activeSessionId），这样切会话后气泡跟着
    // 视图走；与 stopSend / visibleMsgs 取的是同一个 key，三处不会分叉。
    // 新会话还没有 id，先落 PENDING_BUCKET，等流里第一帧带真实 session_id 回来再搬。
    let bucket = activeSessionId || PENDING_BUCKET;
    setStreamRuns((prev) => ({
      ...prev,
      [bucket]: { user: userMsg, assistant: assistantMsg, runId, abort: controller },
    }));
    updateMsgs(bucket, (prev) => [...prev, userMsg]);

    const patch = (p: Partial<ChatMessage>) =>
      setStreamRuns((prev) => {
        const run = prev[bucket];
        if (!run) return prev;
        return { ...prev, [bucket]: { ...run, assistant: { ...run.assistant, ...p } } };
      });
    /**
     * 把桶从 PENDING_BUCKET 搬到真实 session_id 上。
     *
     * **消息列表和流式气泡必须一起搬**：新会话在拿到 id 之前，用户的提问先落在
     * PENDING_BUCKET 上。只搬气泡的话，视图切到真实会话后，那条提问就成了孤儿
     * （躺在没人看的桶里）——用户会看到「我刚发的那句话不见了」。
     */
    const rekey = (realSid: string) => {
      if (!realSid || realSid === bucket) return;
      const from = bucket;
      bucket = realSid;
      setMsgsBySession((all) => {
        if (all[from] === undefined) return all;
        const next = { ...all, [realSid]: all[from] };
        delete next[from];
        return next;
      });
      setStreamRuns((prev) => {
        const run = prev[from];
        if (!run) return prev;
        const next = { ...prev, [realSid]: run };
        delete next[from];
        return next;
      });
    };
    // 逐字累积 + 按帧刷新：token 很密，一帧一次 setState 就够跟手了
    let acc = "";
    let raf = 0;
    const flushDelta = () => {
      raf = 0;
      patch({ text: acc });
    };

    try {
      await apiChatStream(
        agentId,
        sendText,
        {
          sessionId: targetSid,
          stepId,
          // 仅新建会话时传待选用户空间；已有会话忽略（沿用库里绑定）
          userSpaceId: targetSid ? undefined : userSpaceId,
          // 思考模式/强度：同理，仅新建会话传；已有会话沿用库里绑定（后端读行）
          thinking: targetSid ? undefined : thinkingMode,
          reasoningEffort: targetSid ? undefined : reasoningEffort,
          // 运行模式：同理，仅新建会话传；已有会话沿用库里绑定（后端据此选 Process）
          mode: targetSid ? undefined : runMode,
          // 同样只对**新建**的那一次生效，但必须传：会话是发第一条消息才建的，
          // 这里就是浏览器页那条会话落库时唯一的类别来源。
          kind: sessionKind,
          runId,
          // 本轮附件：随消息一起发（只带 file_id 等元信息，文件内容已在上面传完）
          attachments: pendingFiles,
          // @ 快捷项展示元数据：后端原样存，刷新后气泡仍渲染成高亮 chip（不参与推理）
          mentionRaw,
          mentions: mentions.length ? mentions : undefined,
          signal: controller.signal,
        },
        {
          // 会话 id 一确定就立刻持久化。新会话第一句尤其关键：用户在这一轮还没
          // 跑完就刷新时，前端靠 localStorage 里的 id 才能回到同一个会话，进而
          // 把这一轮接回来（否则连「去看哪个会话」都不知道，历史整片消失）。
          onSession: (d) => {
            const sid = d?.session_id;
            if (!sid) return;
            rekey(sid);
            setSessionId(sid);
            localStorage.setItem(sessionKey(agentId, effKind), sid);
            loadWorkspace(sid);
          },
          // 每完成一步：追加到占位消息的思考过程里
          onStep: (s) =>
            setStreamRuns((prev) => {
              const run = prev[bucket];
              if (!run) return prev;
              return {
                ...prev,
                [bucket]: {
                  ...run,
                  assistant: {
                    ...run.assistant,
                    steps: [...(run.assistant.steps ?? []), s],
                  },
                },
              };
            }),
          // 逐字输出：累积到下一帧再统一刷新，避免每个 token 都整页重渲染
          onDelta: (t) => {
            acc += t;
            if (!raf) raf = requestAnimationFrame(flushDelta);
          },
          onDone: (resp) => {
            if (resp.session_id && resp.session_id !== targetSid) {
              rekey(resp.session_id);
              setSessionId(resp.session_id);
              localStorage.setItem(sessionKey(agentId, effKind), resp.session_id);
              loadWorkspace(resp.session_id); // 会话已建，拉取生效工作空间
            }
            // 收尾：把权威内容写进**这一轮所属会话**的消息列表（用服务端的 message_id），
            // 随后 finally 删桶。这里写的是 bucket 而不是「当前视图」，所以用户中途
            // 切去别的会话也只会写进正确的那个桶——串会话在结构上就不可能发生了。
            const resolvedId = resp.message_id || placeholderId;
            updateMsgs(bucket, (prev) => [
              // 双键去重：占位 id（本地流式气泡）与服务端 message_id（loadMessages
              // 偶发抢先把这一轮拉回来时带的那个）都要排掉，否则会留下两条相同助手回复。
              ...prev.filter((m) => m.id !== placeholderId && m.id !== resolvedId),
              {
                ...assistantMsg,
                id: resolvedId,
                // 被用户叫停时：保留已生成的那部分（后端答案可能为空，用前端累积的补上）
                text: resp.canceled
                  ? resp.answer || acc || "（已停止生成）"
                  : resp.answer || "没有拿到回答，请换个问法或稍后重试。",
                llm: resp.llm,
                steps: resp.steps,
                artifacts: resp.artifacts,
                waiting: resp.waiting_human,
                stepId: resp.step_id,
                usage: resp.usage ?? null,
                duration_ms: resp.duration_ms ?? null,
                options: resp.options,
                canceled: resp.canceled,
                streaming: false,
              },
            ]);
          },
          onError: (msg) => {
            patch({ text: "执行失败。", error: msg, streaming: false });
            updateMsgs(bucket, (prev) => [
              ...prev.filter((m) => m.id !== placeholderId),
              { ...assistantMsg, text: "执行失败。", error: msg, streaming: false },
            ]);
          },
        }
      );
    } catch (e: any) {
      // 流式不可用（后端未重启 / 网络问题）：回退到一次性接口，语义不变
      try {
        const resp =
          (await apiChat(
            agentId,
            q,
            targetSid,
            stepId,
            targetSid ? undefined : userSpaceId
          )) || { answer: "", llm: false };
        if (resp.session_id && resp.session_id !== targetSid) {
          rekey(resp.session_id);
          setSessionId(resp.session_id);
          localStorage.setItem(sessionKey(agentId, effKind), resp.session_id);
          loadWorkspace(resp.session_id);
        }
        const resolvedId = resp.message_id || placeholderId;
        updateMsgs(bucket, (prev) => [
            ...prev.filter((m) => m.id !== placeholderId && m.id !== resolvedId),
            {
              ...assistantMsg,
              id: resolvedId,
              text: resp.answer || "没有拿到回答，请换个问法或稍后重试。",
              llm: resp.llm,
              steps: resp.steps,
              artifacts: resp.artifacts,
              waiting: resp.waiting_human,
              stepId: resp.step_id,
              usage: resp.usage ?? null,
              duration_ms: resp.duration_ms ?? null,
              options: resp.options,
              streaming: false,
            },
          ]);
      } catch (e2: any) {
        const errText = "查询失败，请确认 keeper 服务已启动。";
        patch({ text: errText, error: e2?.message || String(e2), streaming: false });
        updateMsgs(bucket, (prev) => [
          ...prev.filter((m) => m.id !== placeholderId),
          {
            ...assistantMsg,
            text: errText,
            error: e2?.message || String(e2),
            streaming: false,
          },
        ]);
      }
    } finally {
      if (raf) cancelAnimationFrame(raf);
      abortRef.current = null;
      runIdRef.current = "";
      placeholderRef.current = "";
      // 删桶：终态消息已由 onDone / catch 写进 msgs，这里只收掉流式副本。
      // 若这轮被 loadMessages 抹掉了用户消息（切走过），补回——否则用户会看到
      // 自己刚发的那句话凭空消失。
      setStreamRuns((prev) => {
        if (!prev[bucket]) return prev;
        const next = { ...prev };
        delete next[bucket];
        return next;
      });
      // 补回用户消息：这一轮被 loadMessages 刷掉过（切走过）就得重新放回去，否则用户会
      // 看到自己刚发的那句话凭空消失。写的是 bucket，不会跑到别的会话去。
      //
      // 按「同内容 user」判断而非前端临时 id：loadMessages 已用**后端 id** 把同一条 user
      // 拉回来时，若只比对前端 id 会判成「缺失」又插一份前端 user，于是同一条提问出现
      // 两个气泡。按内容判则后端已回灌的那条会被认出来，不再重复插入。
      updateMsgs(bucket, (prev) => {
        const hasUser = prev.some(
          (m) =>
            m.role === "user" &&
            (m.id === userMsg.id ||
              (m.text ?? "") === (userMsg.text ?? ""))
        );
        return hasUser ? prev : [...prev, userMsg];
      });
      // 附件跟着清：它们已经随这一轮发出去了，留着会被下一轮误带上。
      // （失败路径也清——附件是独立上传的，file_id 已经拿到，重发时重新选一次
      //  比让一个来路不明的旧附件混进下一轮更可预期。）
      setPendingFiles([]);
      setPendingQuotes([]);
      // 本轮产生了新用量：刷新整会话累计
      qc.invalidateQueries({ queryKey: ["session-usage", activeSessionId] });
      // 侧栏会话列表也要刷：**首条用户消息落库后后端会自动派生出标题**，
      // 不刷的话列表里这条会一直显示「新对话」直到手动刷新页面。
      qc.invalidateQueries({ queryKey: ["sessions"] });
      // 流式彻底收尾：放开探测 effect，让后续（含下一轮）的 loadMessages 正常生效。
      localStreamingRef.current = false;
    }
  };

  /**
   * 停止生成：先通知后端**优雅停止**（apiAbortChat → _STOP_RUNS → should_stop 在
   * 下一个 chunk/step 检查点生效，后端落库部分内容并标记 canceled，SSE 自然发
   * done(canceled) 帧收尾）。
   *
   * 不要再先 abort 断 SSE——那会触发后端 gen() 的 task.cancel() 硬取消，取消点时序
   * 不可控：若落在「落库助手回复」之后，完整答案已写入数据库却收不到 done 帧，前端
   * 虽立即显示「已停止」，但刷新/切换会话重载 session_messages 又会读到这条完整答案
   * （即「点停止后过一会结果还是出来了」的现象）。
   *
   * 兜底：若后端迟迟不回 canceled 帧（卡在不可取消的同步操作），5s 后硬断流。
   *
   * 两种来源的 run_id
   * -----------------
   * - **本地流**（本页发起的）：run_id 在 send() 的闭包里，最常见；
   * - **恢复态**（整页刷新后重建的「进行中」气泡）：本地内存全空，靠后端
   *   ``chat/runs.py`` 的注册表把 run_id 带回来，否则停不掉正在跑的任务。
   *
   * 两者都取不到时**什么都不做**——不能猜一个 run_id 去停，那可能停掉别的会话。
   */
  const stopSend = () => {
    // 句柄从**当前视图对应的桶**里取，而不是全局 ref：不同会话可以并发跑，
    // 用单个 ref 会导致「停 A 却把 B 也停了」。
    const run = activeRun;
    const pid = run?.assistant.id || "";
    // run_id 优先取本地流；本地没有（**整页刷新后重建**的「进行中」气泡）就取
    // 后端注册表带回来的那个（见 chat/runs.py）。取不到就放弃——宁可按钮无效，
    // 也不能拿一个错的 run_id 去停别人的任务。
    const rid = run?.runId || recoveredRun?.runId || "";
    const bucketKey = activeSessionId || PENDING_BUCKET;
    // 1. 通知后端优雅停止（should_stop 在下一个检查点生效）
    if (rid) {
      void apiAbortChat(agentId, rid).catch(() => {});
    }
    // 2. UI 立即反馈「已停止」，但保留占位消息等 done 帧补内容/标记
    setStreamRuns((prev) => {
      const cur = prev[bucketKey];
      if (!cur) return prev;
      return {
        ...prev,
        [bucketKey]: {
          ...cur,
          assistant: { ...cur.assistant, streaming: false, canceled: true },
        },
      };
    });
    // 2-b 恢复态的气泡直接在**它自己那个会话**的列表里标停：它没有对应的本地流式桶
    if (recoveredRun) {
      updateMsgs(curBucket, (prev) =>
        prev.map((m) =>
          m.id === recoveredRun.id ? { ...m, streaming: false, canceled: true } : m
        )
      );
    }
    // 3. 兜底：超时仍未结束则硬断流（仅当后端可能被不可取消操作卡住）
    const ctrl = run?.abort;
    if (ctrl) {
      setTimeout(() => {
        try {
          ctrl.abort();
        } catch {
          /* ignore */
        }
        if (runIdRef.current === rid) runIdRef.current = "";
        if (placeholderRef.current === pid) placeholderRef.current = "";
        abortRef.current = null;
      }, 5000);
    }
  };

  /** 切换已有会话绑定的用户空间（对话结束后可换） */
  const switchWorkspace = async (val: string) => {
    if (!sessionId) return;
    try {
      await apiUpdateSessionWorkspace(agentId, sessionId, val || null);
      await loadWorkspace(sessionId);
      // 工作空间变了，关闭当前打开的文件并刷新编码工作台文件树
      setActiveFilePath(null);
      setExplorerRefresh((n) => n + 1);
      if (val) toast.success("已切换工作空间");
      else toast.success("已回落到 agent 默认目录");
    } catch (e: any) {
      toast.error(e?.message || "切换失败");
    }
  };

  // 思考模式/强度：已有会话改一下即写库（不影响工作空间，后端 exclude_unset 区分）；
  // 新会话只更新本地 pending 值（发第一条消息时才落库）。
  const applyThinking = async (enabled: boolean, effort: ReasoningEffort) => {
    if (!sessionId) {
      setThinkingMode(enabled);
      setReasoningEffort(effort);
      return;
    }
    try {
      await apiUpdateSessionWorkspace(agentId, sessionId, undefined, {
        enabled,
        reasoning_effort: effort,
      });
      await loadWorkspace(sessionId);
    } catch (e: any) {
      toast.error(e?.message || "思考模式切换失败");
    }
  };

  // 运行模式：仅在「新会话」（无 sessionId）时可选——选中即存本地 pending，发第一条
  // 消息时落库；会话一旦创建，模式即固定，选择器置灰不可改（见下方 ModePicker 的
  // disabled={!!sessionId}）。后端 PUT 的 mode 能力保留，但前端不再暴露切换入口。

  // 原生 select 的 option 只能放纯文本，所以把「只读 / 可写」并进文案
  const usOptions = userSpaces.map((u) => ({
    value: u.id,
    label: `${u.name}（${u.read_only ? "只读" : "可写"}）`,
  }));

  /** 输入框高度（可拖把手放大，向上拖变大） */
  const [inputH, setInputH] = useState(64);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);

  useEffect(() => {
    const move = (e: MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      // 向上拖动 → clientY 变小 → 高度变大
      const next = d.startH + (d.startY - e.clientY);
      setInputH(Math.max(64, Math.min(320, next)));
    };
    const up = () => {
      if (!dragRef.current) return;
      dragRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  // 聊天列：消息流 + 输入框（两种模式共用，外层容器不同；工作空间已移到顶栏）
  const chatInner = (
    <div
      className="relative flex min-h-0 flex-1 flex-col"
      onDragEnter={onPanelDragEnter}
      onDragOver={onPanelDragOver}
      onDragLeave={onPanelDragLeave}
      onDrop={onPanelDrop}
    >
      {/*
        消息流：纯白底 + 居中单列。

        这里原来挂的是 .kp-canvas（顶部带一道品牌色光晕）。光晕在"有顶栏压着"
        的时候还有层次感，顶栏一去掉，它就变成画面上唯一一块没来由的蓝雾——
        所以内容页直接退回干净的白，让消息本身成为唯一的视觉主体。
      */}
      <div ref={listRef} className="relative min-h-0 flex-1 overflow-auto bg-panel px-4 py-5">
        <div className="mx-auto w-full max-w-[46rem]">
        {/*
          「加载更早的消息」：会话可能很长，接口单次只给最近一段。显式按钮而不是
          无限滚动——气泡里常带着一长串思考步骤，自动加载既容易让用户失去位置感，
          一次拉太多也会明显卡。
        */}
        {paging[curBucket]?.hasMore && (
          <div className="flex justify-center pb-3.5">
            <button
              type="button"
              disabled={loadingOlder}
              onClick={loadOlder}
              className="flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[13px] text-ink-muted transition-colors hover:bg-surface hover:text-ink disabled:opacity-50"
            >
              {loadingOlder && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              加载更早的消息
            </button>
          </div>
        )}
        {visibleMsgs.map((m) => (
          <div key={m.id}>
            <ChatBubble
              msg={m}
              agentId={agentId}
              agentName={agentName}
              sessionId={activeSessionId}
              isLocal={isLocal}
              onUsageDetail={openUsageDetail}
              onUsageDump={
                dumpEnabled && m.role === "assistant"
                  ? openUsageDump
                  : undefined
              }
              onRegenerate={sending ? undefined : regenerate}
            />
            {m.waiting && m.stepId && (
              <AskBox
                options={m.options}
                disabled={sending}
                onSubmit={(t) => send(t, m.stepId)}
              />
            )}
          </div>
        ))}
          {sending && (
            /* 呼吸点比转圈更克制：流式正文本身就在动，再加一个旋转图标
               会把注意力从内容上抢走。 */
            <div className="flex items-center justify-center gap-2 py-2.5 text-[12.5px] text-ink-muted">
              <span className="kp-pulse-dot h-1.5 w-1.5 rounded-full bg-brand" />
              正在生成…
            </div>
          )}
        </div>
      </div>

      {/* 配额 / 累计用量：跟输入框同列宽，不要贴着窗口边缘 */}
      <div className="mx-auto w-full max-w-[46rem] px-4">
        {budget && budget.limit > 0 && (
          <div className="mt-3">
            <AlertHint
              tone={budget.over ? "danger" : budget.warning ? "warn" : "info"}
              title={`本会话配额：${fmtTokens(budget.used)} / ${fmtTokens(
                budget.limit
              )}（${(budget.rate * 100).toFixed(0)}%）`}
            >
              {budget.over
                ? "已超出配额上限，后续执行会被拦住。"
                : budget.warning
                ? "已用掉 80% 以上，快到上限了。"
                : undefined}
            </AlertHint>
          </div>
        )}
        {sessionUsage && sessionUsage.calls > 0 && (
          <div className="mt-1.5 text-right text-[12px] text-ink-muted">
            本会话累计：<UsageTag usage={sessionUsage} />
          </div>
        )}
      </div>

      <UsageDetailModal
        messageId={detailMessageId}
        open={!!detailMessageId}
        onClose={() => setDetailMessageId(null)}
      />

      <PromptDumpModal
        messageId={dumpMessageId}
        open={!!dumpMessageId}
        onClose={() => setDumpMessageId(null)}
      />

      {/*
        输入区：整块做成一个「容器」，聚焦时整块描边高亮（现代对话产品的通行做法），
        比「裸输入框 + 旁边一个按钮」更像一个整体。
      */}
      {/*
        输入区（截图里最标志性的元素）：一个**大圆角白色浮块**，带明显阴影，
        悬在页面底部。它不是一个"贴着窗口边的输入条"——浮起来这一点很关键，
        让输入框在视觉上独立于内容流。
        结构自上而下：正文输入 → 一条工具行（左：+ / 权限 / 计划；右：模型 / 发送）。
      */}
      <div className="shrink-0 bg-panel px-4 pb-3.5 pt-2">
        <div className="mx-auto w-full max-w-[46rem]">
          {/* 浮块投影给足两层（近处贴地一层、远处散开一层），并让它"浮"在内容
              之上——输入区是整个页面唯一需要离开纸面感的东西，投影太弱就变成
              一个贴着边的多行文本框。聚焦时再加一圈品牌色描边，光标落点一眼可见。 */}
          <div
            className={cn(
              "relative rounded-[18px] border border-line-soft bg-panel px-3.5 py-2.5",
              "shadow-[0_8px_28px_-8px_rgba(0,0,0,0.16),0_2px_6px_-2px_rgba(0,0,0,0.06)]",
              "transition-all duration-200 ease-apple",
              "focus-within:border-brand/45 focus-within:shadow-[0_10px_32px_-8px_rgba(0,0,0,0.20),0_0_0_3px_rgba(66,133,244,0.4)]"
            )}
          >
            {/* 待发送的附件：一排小卡片，缩略图 + 名称 + 删除。
                放在输入框**里面**（浮块内最上一行）而不是外面：它属于这一轮的
                输入，跟文字挨着才看得出"我要发的是这些"。放外面会变成另一块
                常驻 UI，和输入框的关系就断了。 */}
            {pendingFiles.length > 0 && (
              <div className="mb-1.5 flex flex-wrap gap-1.5">
                {pendingFiles.map((a, i) => (
                  <div
                    key={a.id}
                    className="group flex max-w-[200px] items-center gap-1.5 rounded-full border border-brand/20 bg-brand/10 py-1 pl-1.5 pr-1 text-[12px] text-ink"
                    title={`${a.name}（${(a.size / 1024).toFixed(0)} KB）`}
                  >
                    {/* 缩略图直接读本地缓存（GET /files/attachment/...）：
                        上传时已落一份，这里不用再问厂商要——file_id 那条路是
                        给模型走的，前端取内容走缓存，两条路各司其职。 */}
                    <img
                      src={attachmentUrl(agentId, activeSessionId || "", a.id)}
                      alt={a.name}
                      className="h-4 w-4 shrink-0 rounded object-cover"
                    />
                    <span className="truncate">{a.name}</span>
                    <button
                      type="button"
                      title="移除"
                      onClick={() => removeFile(i)}
                      className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-black/10 hover:text-ink dark:hover:bg-white/20"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {pendingQuotes.length > 0 && (
              <div className="mb-1.5 flex flex-wrap gap-1.5">
                {pendingQuotes.map((qq, i) => (
                  <div
                    key={i}
                    className="group flex max-w-[300px] items-center gap-1.5 rounded-full border border-brand/20 bg-brand/10 py-1 pl-1.5 pr-1 text-[12px] text-ink"
                    title={`${qq.path}\n\n${qq.text}`}
                  >
                    <FileText className="h-3.5 w-3.5 shrink-0 text-brand" />
                    <span className="truncate">
                      {qq.path.split(/[\\/]/).pop()} · 引用 {qq.text.length} 字
                    </span>
                    <button
                      type="button"
                      title="移除"
                      onClick={() =>
                        setPendingQuotes((prev) => prev.filter((_, j) => j !== i))
                      }
                      className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-black/10 hover:text-ink dark:hover:bg-white/20"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {/* 正文输入：无边无底色，融进浮块 */}
            {/* @ 快捷项弹出：出现在输入框上方（bottom-full），不挡住正在输入的光标 */}
            {mention && filteredMentions.length > 0 && (
              <div className="absolute bottom-full left-4 right-4 z-50 mb-2">
                <MentionPicker
                  items={filteredMentions}
                  activeIndex={activeMention}
                  onSelect={selectMention}
                  onHover={setActiveMention}
                />
              </div>
            )}

            {/* 输入框分两层：透明 textarea（接收输入 / 光标）+ 背后镜像层（把 @标签 渲染成高亮 chip）。
                两层字号 / 行高 / 内边距完全一致，所以光标与高亮始终对齐；textarea 文字透明，只露光标。 */}
            <div className="relative">
              <div
                ref={backdropRef}
                aria-hidden
                className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-1 pt-0.5 text-[13.5px] leading-6 text-ink"
                style={{ height: inputH }}
              >
                {renderBackdrop(input)}
                {"\n"}
              </div>
              <textarea
                ref={textareaRef}
                value={input}
                onChange={onInputChange}
                placeholder="今天帮你做什么？@ 添加上下文，/ 调用技能与指令"
                style={{ height: inputH }}
                className="relative w-full resize-none break-words bg-transparent px-1 pt-0.5 text-[13.5px] leading-6 text-transparent caret-ink outline-none placeholder:text-ink-faint [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
                onKeyDown={onInputKeyDown}
                onScroll={(e) => {
                  if (backdropRef.current)
                    backdropRef.current.scrollTop = (
                      e.target as HTMLTextAreaElement
                    ).scrollTop;
                }}
                onPaste={(e) => {
                  // 粘贴文件 / 截图：拦截掉"图片被塞成 base64 文本"，转为附件上传。
                  // 纯文本粘贴不受影响，照常走 onChange。
                  const pasted = e.clipboardData?.files;
                  if (pasted && pasted.length) {
                    e.preventDefault();
                    void addFiles(pasted);
                  }
                }}
              />
            </div>

            {/* 工具行：左边低频动作、右边模型与发送。整行高度固定 30px */}
            <div className="mt-1 flex items-center gap-1">
              {/* 附件 / 添加上下文 */}
              <button
                type="button"
                title="上传附件（图片）"
                disabled={uploading}
                onClick={() => fileInputRef.current?.click()}
                className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full text-ink-muted transition-colors hover:bg-black/[0.05] hover:text-ink disabled:opacity-50 dark:hover:bg-white/10"
              >
                {uploading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Plus className="h-4 w-4" />
                )}
              </button>
              {/* 隐藏的真文件选择器：点了上面那个按钮才触发。
                  不自己画一个是因为原生的有系统级交互（拖文件进来、键盘可达、
                  多选），自己实现这些是在重复浏览器已经做好的事。 */}
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/gif,image/webp"
                multiple
                hidden
                onChange={(e) => {
                  void addFiles(e.target.files);
                  // 清掉 value：否则连续选同一个文件不会触发 change，
                  // 用户会以为按钮坏了
                  e.target.value = "";
                }}
              />

              {/* 权限模式：显示当前模式，点击切换（这里简化为提示） */}
              <span className="hidden items-center gap-1 rounded-full px-1.5 py-0.5 text-[11.5px] text-ink-muted sm:flex">
                <ShieldCheck className="h-3 w-3" />
                默认权限
              </span>

              {/* 模式标签：当前处于哪种工作视图。这是输入区里唯一的彩色元素，
                  给它品牌淡底，输入框就不会因为"全是灰控件"显得寡淡。 */}
              <span className="flex items-center gap-1 rounded-full bg-brand/10 py-[3px] pl-1.5 pr-2 text-[11.5px] font-medium text-brand">
                <ListChecks className="h-3 w-3" />
                {browserOnly ? "浏览器" : mode === "task" ? "任务" : "对话"}
              </span>

              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                {/* 工作台 / 工作空间 / 模型：三个都在回答同一个问题——这次在哪儿、
                    用哪个模型干活，所以并排放在输入框右侧，顺序也按这个来：
                    工作台（用哪种工作台）→ 工作空间（在哪个目录）→ 模型（用谁思考）。

                    这三个原先散在两处：工作台和工作空间在**会话抽屉头部**，模型在输入
                    框里。抽屉是"选哪个会话"的地方，把干活的控件塞进去等于让它们离真正
                    的使用现场很远——打开抽屉才够得着，而工作空间恰恰是**每轮都可能要换**
                    的那个。

                    工作台本身也从"开关按钮"改成了下拉：右侧那块区域不止文件浏览一种
                    用法，现在是 关 / 编码工作台 / Agent 浏览器 三选一。 */}
                <WorkbenchPicker
                  value={workbenchType}
                  onChange={setWorkbenchType}
                />

                {/* 工作空间选择：会话 / 任务模式共用。
                    宽度给死（原 flex-1 会占满整行，把模型选择挤走），
                    并保留 truncate：空间名可能很长。
                    **最窄的一档整体隐藏**（md 以下）：这一行右侧同时挂着工作台、
                    模型选择和发送钮，全展开时没有 900px 就一定会挤。空间切换在
                    窄屏下仍可从「Agent 配置」里改，功能没有丢。 */}
                {(() => {
                  const dflt =
                    userSpaces.find((u) => u.name === "default")?.id ?? "";
                  const cur = sessionId
                    ? workspace?.user_space?.id || dflt
                    : userSpaceId || dflt;
                  // 会话指向的空间被删、或数据异常时 cur 不在列表里，补一个兜底项，
                  // 否则触发器显示空白，用户看不出当前究竟在哪个目录
                  const opts = [
                    ...usOptions,
                    ...(cur && !usOptions.some((o) => o.value === cur)
                      ? [{ value: cur, label: cur }]
                      : []),
                  ];
                  // 保留文件小图标作入口，点它从上方弹出工作区列表（见 WorkspacePicker）。
                  return (
                    <WorkspacePicker
                      value={cur}
                      options={opts}
                      onChange={(v) =>
                        sessionId ? switchWorkspace(v) : setUserSpaceId(v)
                      }
                    />
                  );
                })()}

                {/*
                  模型选择：改的是**这个 agent 的绑定**，不是逐会话的。
                  所以在这里换了模型，对该 agent 的所有会话都生效（核心 agent
                  更是如此——所有会话都在它身上）。服务端改完会重装配，让改动
                  当场生效；装配失败时这里会显示原因，而不是静默不变。

                  选「未绑定」= 解绑，该 agent 进入纯索引模式（不调模型）。
                */}
                <ModelPicker agentId={agentId} />

                {/* 高度把手：向上拖放大输入框 */}
                <div
                  className="kp-grip hidden sm:flex"
                  title="向上拖动放大输入框"
                  onMouseDown={(e) => {
                    dragRef.current = { startY: e.clientY, startH: inputH };
                    document.body.style.cursor = "row-resize";
                    document.body.style.userSelect = "none";
                  }}
                >
                  <i />
                </div>

                {sending ? (
                  <button
                    type="button"
                    onClick={stopSend}
                    title="停止生成"
                    className="flex h-[30px] w-[30px] items-center justify-center rounded-full bg-ink-strong text-panel shadow-[0_2px_8px_rgba(0,0,0,0.25)] transition-all duration-150 hover:opacity-90 active:scale-95"
                  >
                    <Square className="h-2.5 w-2.5" fill="currentColor" />
                  </button>
                ) : (
                  /* 发送键：品牌渐变 + 同色投影。按下缩 5%，比纯变色更有物理感。 */
                  <button
                    type="button"
                    onClick={() => send()}
                    disabled={!expandMentions(input).trim() && pendingQuotes.length === 0}
                    title="发送（Enter）"
                    className={cn(
                      "flex h-[30px] w-[30px] items-center justify-center rounded-full transition-all duration-150 ease-apple active:scale-95",
                      input.trim()
                        ? "bg-brand-gradient text-white shadow-brand hover:brightness-110"
                        : "cursor-not-allowed bg-black/[0.06] text-ink-faint dark:bg-white/[0.08]"
                    )}
                  >
                    <ArrowUp className="h-4 w-4" strokeWidth={2.5} />
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* 思考模式选择器：输入框下方。开 → 显示强度（低/高/最高）；
              新会话用本地 pending 值，已有会话改即写库（applyThinking）。 */}
          <div className="mt-2 flex items-center gap-2 px-1">
            <ModePicker
              disabled={!!sessionId}
              value={
                ((sessionId ? workspace?.mode : runMode) as RunMode) || "normal"
              }
              onChange={setRunMode}
            />
            <ThinkingPicker
              thinking={sessionId ? !!workspace?.thinking : thinkingMode}
              effort={
                ((sessionId
                  ? workspace?.reasoning_effort
                  : reasoningEffort) as ReasoningEffort) || "low"
              }
              onChange={(thinking, effort) => void applyThinking(thinking, effort)}
            />
          </div>

          {/* 底部免责声明：截图里是居中的极小灰字 */}
          <p className="mt-2 text-center text-[10.5px] text-ink-faint">
            内容由 AI 生成，请核实重要信息
          </p>
        </div>
      </div>
      {/* 拖放文件时的全屏高亮提示：覆盖整块聊天面板 */}
      {isDragging && (
        <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-[18px] border-2 border-dashed border-brand/60 bg-brand/5">
          <div className="flex items-center gap-2 rounded-full bg-panel/90 px-4 py-2 text-[13px] font-medium text-brand shadow-[0_4px_16px_rgba(0,0,0,0.12)]">
            <UploadCloud className="h-4 w-4" />
            松开以添加文件
          </div>
        </div>
      )}
    </div>
  );

  return (
    <div
      /* group/chat：内容区 hover 时唤出左上角那个「会话」回退入口 */
      className="group/chat"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        position: "relative",
      }}
    >
      {/*
        原来这里有一条 44px 顶栏（列表入口 / 视图切换 / 工作台 / 工作空间 / 配置）。
        已整体移除：那条带子把「内容」往下压了一行，而它承载的全部是**导航类**
        操作——它们本就属于列表所在的那一侧。
        这些控件没有丢，全部搬进了左侧「会话」抽屉的头部（见下方 drawer），
        抽屉打开时才出现，正好和"选哪个会话"是同一件事。

        代价是丢掉了「打开列表」的常驻入口（顶栏那个列表按钮），所以这里补一个
        浮在内容区左上角的小按钮顶上。

        它**常驻显示**：会话列表整体搬进抽屉之后，这个按钮就是"回到会话列表"的
        唯一入口了——藏到 hover 才现形，等于让用户先在内容区里扫一圈才能发现
        它。唯一隐藏的情形是抽屉已打开：那时它压在抽屉上，显出来只会挡住内容。

        这里原先还有一颗「返回」按钮，已撤掉：返回是导航动作，归宿是侧栏那两项。 */}
      {/* 左上角悬浮控件组：现在只剩「列表入口」一颗。 */}
      <div
        className={cn(
          "absolute left-3 top-3 z-10 flex items-center gap-1.5",
          "transition-opacity duration-200 ease-apple",
          /* 抽屉打开时让位：这时它压在抽屉上，显出来只会挡住内容 */
          drawerOpen && "pointer-events-none opacity-0"
        )}
      >
        <button
          type="button"
          onClick={() => setDrawerOpen(true)}
          title={browserOnly ? "浏览器会话列表" : mode === "task" ? "任务列表" : "会话列表"}
          className="flex h-8 items-center gap-1.5 rounded-[9px] border border-line-soft bg-panel px-2.5 text-[12.5px] font-medium text-ink-muted shadow-card transition-colors duration-150 hover:border-brand/40 hover:text-brand"
        >
          <List className="h-3.5 w-3.5" />
          {browserOnly ? "浏览器会话" : mode === "task" ? "任务" : "会话"}
        </button>
      </div>

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* 工作台区域：叠加在工作模式左侧。三种形态（关 / 编码 / Agent 浏览器）共用同一套
            宽度和拖拽把手——换形态时这块区域的位置不该跟着跳一下。 */}
        {workbenchType !== "off" && (
          <Suspense
            fallback={
              <div
                className="flex shrink-0 items-center justify-center border-r border-line-soft text-[13px] text-ink-muted"
                style={{ width: workbenchWidth }}
              >
                加载工作台…
              </div>
            }
          >
            {workbenchType === "coding" ? (
              <>
                <div
                  style={{
                    width: workbenchWidth,
                    flex: "0 0 auto",
                    display: "flex",
                    minHeight: 0,
                    overflow: "hidden",
                    borderRight: "1px solid var(--kp-border-soft)",
                  }}
                >
                  <div
                    style={{
                      width: explorerWidth,
                      borderRight: "1px solid var(--kp-border-soft)",
                      display: "flex",
                      flexDirection: "column",
                      minHeight: 0,
                      flex: "0 0 auto",
                    }}
                  >
                    <FileExplorer
                      agentId={agentId}
                      sessionId={sessionId}
                      onOpenFile={setActiveFilePath}
                      onDeleted={(p) => {
                        if (p === activeFilePath) setActiveFilePath(null);
                      }}
                      onRenamed={(oldPath, newPath) => {
                        if (oldPath === activeFilePath) setActiveFilePath(newPath);
                      }}
                      refreshSignal={explorerRefresh}
                      rootName={workspace?.user_space?.name}
                    />
                  </div>
                  <Resizer
                    onResize={(dx) =>
                      setExplorerWidth((w) =>
                        Math.max(160, Math.min(520, w + dx))
                      )
                    }
                  />
                  <div
                    style={{
                      flex: 1,
                      minHeight: 0,
                      display: "flex",
                      flexDirection: "column",
                    }}
                  >
                    <CodeView
                      agentId={agentId}
                      sessionId={sessionId}
                      path={activeFilePath}
                      readOnly={workspace?.read_only ?? true}
                      onSaved={() => setExplorerRefresh((n) => n + 1)}
                    />
                  </div>
                </div>
                <Resizer onResize={resizeWorkbench} />
              </>
            ) : (
              /* 浏览器工作台（用户可见那一份 WebContentsView）：结构比编码工作台简单得多
                 （没有内部分栏），但外框的宽度、边框、把手三处必须一致，否则形态互切时
                 右侧边界会来回跳。 */
              <>
                <div
                  style={{
                    width: workbenchWidth,
                    flex: "0 0 auto",
                    display: "flex",
                    minHeight: 0,
                    overflow: "hidden",
                    borderRight: "1px solid var(--kp-border-soft)",
                  }}
                >
                  {/* 抽屉打开时把原生浏览器视图藏起来：WebContentsView 不在 DOM 里，
                      z-index 对它无效，不显式藏会被整个压在抽屉下面 */}
                  <BrowserPanel
                    className="min-w-0 flex-1"
                    overlayOpen={drawerOpen || configOpen}
                  />
                </div>
                <Resizer onResize={resizeWorkbench} />
              </>
            )}
          </Suspense>
        )}

        {/* 工作模式区域：会话视图(对话) 或 任务视图(对话+计划) */}
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            minWidth: 0,
            // 内边距交给 chatInner 自己（里面是居中的单列容器），外层再加会翻倍
            padding: 0,
            boxSizing: "border-box",
          }}
        >
          {/* browserOnly 时恒走 chatInner：那一页没有"任务视图"，切过去会把浏览器
              会话的界面换成任务面板，而那条任务根本不在本页的列表里。
              写成 browserOnly || 而不是靠 mode 恒为 "chat"——那是个需要读者
              相信的隐式约定，忘了就会渲染出一个能点却不该存在的页面。 */}
          {browserOnly || mode === "chat" ? (
            chatInner
          ) : (
            <Suspense
              fallback={
                <div className="flex h-full items-center justify-center text-[13px] text-ink-muted">
                  加载任务视图…
                </div>
              }
            >
              <TaskPage agentId={agentId} sessionId={sessionId} />
            </Suspense>
          )}
        </div>
      </div>

      {/* Agent 配置抽屉（入口在会话抽屉头部 / 没有会话时悬浮在左上角） */}
      <AgentInfoDrawer
        open={configOpen}
        onClose={() => setConfigOpen(false)}
        agentId={agentId}
      />

      {/* 会话浮层动画关键帧（入场/退场用 CSS 动画，挂载时可靠播放） */}
      <style>{`
        @keyframes kp-slide-in { from { transform: translateX(-100%); } to { transform: translateX(0); } }
        @keyframes kp-slide-out { from { transform: translateX(0); } to { transform: translateX(-100%); } }
        @keyframes kp-fade-in { from { opacity: 0; } to { opacity: 1; } }
        @keyframes kp-fade-out { from { opacity: 1; } to { opacity: 0; } }
      `}</style>

      {/* 会话列表：从内容区左侧（菜单右侧）滑出的浮层，不覆盖全局菜单 */}
      {drawerMounted && (
        <>
          <div
            onClick={closeDrawer}
            style={{
              position: "absolute",
              inset: 0,
              background: "rgba(0,0,0,0.04)",
              zIndex: 10,
              animation: drawerClosing
                ? "kp-fade-out 0.3s ease both"
                : "kp-fade-in 0.3s ease both",
            }}
          />
          <div
            style={{
              position: "absolute",
              top: 0,
              bottom: 0,
              left: 0,
              width: 264,
              // 用主题变量而不是写死 #fff：否则深色主题下这块面板会亮成一块
              background: "var(--kp-bg)",
              borderRight: "1px solid var(--kp-border-soft)",
              boxShadow: "16px 0 48px -20px rgba(0,0,0,0.28)",
              zIndex: 11,
              display: "flex",
              flexDirection: "column",
              animation: drawerClosing
                ? "kp-slide-out 0.34s cubic-bezier(0.22, 1, 0.36, 1) both"
                : "kp-slide-in 0.34s cubic-bezier(0.22, 1, 0.36, 1) both",
            }}
          >
            {/*
              抽屉头部承接了原顶栏的控件：
                第一行「会话 / 任务」切换 + 关闭；第二行只剩「Agent 配置」。
              「选哪个会话」和「配置 agent」放在这里合理；而「工作台」「工作空间」
              已经搬到输入框右侧，和模型选择并排（见那里的说明）。
            */}
            <div className="shrink-0 border-b border-line-soft px-3 pb-2.5 pt-2.5">
              <div className="flex items-center gap-2">
                <span className="text-[13px] font-semibold tracking-[-0.01em] text-ink-strong">
                  会话
                </span>
                {/* 类别切换：对话页在 chat/task 间切；浏览器页在 browser（手动提问）/
                    browser-automation（网页 Agent 自动操作）两个子类别间切。两种都是同一
                    款 SegmentedControl，样式与「对话」页一致。 */}
                {browserOnly ? (
                  <SegmentedControl
                    value={browserTab}
                    onChange={(v) =>
                      setBrowserTab(v as "browser" | "browser-automation")
                    }
                    options={[
                      { label: "浏览器会话", value: "browser" },
                      { label: "网页 Agent", value: "browser-automation" },
                    ]}
                  />
                ) : (
                  <SegmentedControl
                    value={mode}
                    onChange={(v) => setMode(v as "chat" | "task")}
                    options={[
                      { label: "会话", value: "chat" },
                      { label: "任务", value: "task" },
                    ]}
                  />
                )}
                <button
                  type="button"
                  title="关闭"
                  onClick={closeDrawer}
                  className="ml-auto flex h-7 w-7 items-center justify-center rounded-[7px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              {/* 第二行现在只剩「Agent 配置」。
                  原先这行是「工作台」「工作空间」「配置」三个 —— 前两个已搬到
                  输入框右侧（与模型选择并排），因为它们决定"在哪儿干活"，是**每轮
                  都可能改**的东西，不该藏在"选会话"的抽屉里翻。抽屉只保留"选会话"
                  这件事本身。 */}
              <div className="mt-2.5 flex items-center justify-end">
                <button
                  type="button"
                  onClick={() => setConfigOpen(true)}
                  title="Agent 配置"
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[7px] text-ink-muted transition-colors hover:bg-surface hover:text-ink"
                >
                  <Settings2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
              <SessionList
                agentId={agentId}
                activeId={sessionId}
                /* 类别：对话页按 tab 给 chat/task，浏览器页按子 tab 给
                   browser / browser-automation。必须是显式枚举而不是"非 task 即普通"
                   ——那样浏览器会话会混进「会话」列表，而它在那条会话的消息并不属于这里。 */
                kind={effKind}
                hideCreate={browserOnly && browserTab === "browser-automation"}
                onSelect={(sid) => {
                  // 任务视图里选的是任务会话，不能顶掉会话视图的选择
                  if (mode === "task") applyTaskSession(sid);
                  else handleSelect(sid);
                  closeDrawer();
                }}
                onNew={() => {
                  handleNew();
                  closeDrawer();
                }}
                onNewTask={(sid) => {
                  applyTaskSession(sid);
                  closeDrawer();
                }}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
