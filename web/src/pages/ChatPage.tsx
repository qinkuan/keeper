import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  Alert,
  Button,
  Input,
  Select,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import {
  UnorderedListOutlined,
  CloseOutlined,
  SettingOutlined,
  FolderOutlined,
  CodeOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ChatBubble from "../components/ChatBubble";
import SessionList from "../components/SessionList";
import AskBox from "../components/AskBox";
import FileExplorer from "../components/FileExplorer";
import CodeView from "../components/CodeView";
import AgentInfoDrawer from "../components/AgentInfoDrawer";
import UsageDetailModal from "../components/UsageDetailModal";
import PromptDumpModal from "../components/PromptDumpModal";
import { UsageTag, fmtTokens } from "../components/UsageTag";
import type { UsageSummary } from "../api/types";
import TaskPage from "./TaskPage";

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
  apiListTasks,
  apiListUserSpaces,
  apiSessionMessages,
  apiSessions,
  apiSessionWorkspace,
  apiUpdateSessionWorkspace,
  apiSessionMetrics,
  apiBudget,
  apiGetSettings,
} from "../api/client";
import type {
  ChatMessage,
  SessionWorkspace,
  UserSpace,
} from "../api/types";

const newId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

/** 每个 agent 单独记会话 id，避免在不同 agent 间串号 */
const sessionKey = (agentId: string) => `keeper.sessionId.${agentId}`;

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
};

export default function ChatPage({ agentId }: { agentId: string }) {
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
  const [sessionId, setSessionId] = useState<string | undefined>(
    () => localStorage.getItem(sessionKey(agentId)) || undefined
  );
  /** 会话视图选中的会话：与「任务视图」用的任务会话分开保存，避免互相顶掉 */
  const [chatSessionId, setChatSessionId] = useState<string | undefined>(
    () => localStorage.getItem(sessionKey(agentId)) || undefined
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
  const listRef = useRef<HTMLDivElement>(null);

  // 工作模式：会话视图 / 任务视图（合并原「会话模式」「任务模式」）
  const [mode, setMode] = useState<"chat" | "task">("chat");
  // 工作台：关 / 编码工作台（原 Coding 模式，作为可叠加的右侧区域）
  const [workbenchType, setWorkbenchType] = useState<"off" | "coding">("off");
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
  // 编码工作台：文件浏览器宽度（内部可拖拽），及工作台整体宽度（与主区域间可拖拽）
  const [explorerWidth, setExplorerWidth] = useState(248);
  const [workbenchWidth, setWorkbenchWidth] = useState(560);
  // Agent 配置抽屉（入口在顶栏右侧）
  const [configOpen, setConfigOpen] = useState(false);

  // 用户空间列表（选择器用）
  const usQ = useQuery({ queryKey: ["user-spaces"], queryFn: apiListUserSpaces });
  const userSpaces: UserSpace[] = usQ.data?.user_spaces ?? [];

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
          usage: m.usage ?? null,
          duration_ms: m.duration_ms ?? null,
          llm: m.role === "assistant",
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
      message.warning("加载更早的消息失败，请稍后重试");
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
                  // 可观测：历史消息的用量 / 耗时（刷新后仍要能看到）
                  usage: m.usage ?? null,
                  duration_ms: m.duration_ms ?? null,
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
    if (activeRun) return;
    // 只有存在「进行中」的气泡才需要轮询
    if (!msgs.some((m) => m.streaming)) return;
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
          return hasWelcome ? [welcomeMsg, ...rest, ...added] : [...rest, ...added];
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
        onDone: (d) =>
          settle({
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
          }),
        onError: (msg) => settle({ text: "执行失败。", error: msg }),
        onGone: () => {
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
    const sid = localStorage.getItem(sessionKey(agentId));
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

  const handleSelect = (sid: string) => {
    setSessionId(sid);
    setChatSessionId(sid);
    setUserSpaceId("");
    localStorage.setItem(sessionKey(agentId), sid);
    loadMessages(sid);
    loadWorkspace(sid);
  };

  const handleNew = () => {
    setSessionId(undefined);
    setChatSessionId(undefined);
    setUserSpaceId("");
    setWorkspace(null);
    localStorage.removeItem(sessionKey(agentId));
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
   */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (mode === "chat") {
          // 关键：任务会话不能顶掉会话视图的会话，回来时按 chatSessionId 恢复
          if (chatSessionId) {
            if (!cancelled) {
              setSessionId(chatSessionId);
              await loadMessages(chatSessionId);
              loadWorkspace(chatSessionId);
            }
            return;
          }
          const r = await apiSessions(agentId);
          const first = r.sessions?.[0];
          if (!cancelled && first?.id) handleSelect(first.id);
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
  }, [mode, agentId]);

  const send = async (raw?: string, stepId?: string) => {
    const q = (raw ?? input).trim();
    if (!q || sending) return;

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
      text: q,
      created_at: new Date().toISOString(),
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
        q,
        {
          sessionId: targetSid,
          stepId,
          // 仅新建会话时传待选用户空间；已有会话忽略（沿用库里绑定）
          userSpaceId: targetSid ? undefined : userSpaceId,
          runId,
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
            localStorage.setItem(sessionKey(agentId), sid);
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
              localStorage.setItem(sessionKey(agentId), resp.session_id);
              loadWorkspace(resp.session_id); // 会话已建，拉取生效工作空间
            }
            // 收尾：把权威内容写进**这一轮所属会话**的消息列表（用服务端的 message_id），
            // 随后 finally 删桶。这里写的是 bucket 而不是「当前视图」，所以用户中途
            // 切去别的会话也只会写进正确的那个桶——串会话在结构上就不可能发生了。
            updateMsgs(bucket, (prev) => [
              ...prev.filter((m) => m.id !== placeholderId),
              {
                ...assistantMsg,
                id: resp.message_id || placeholderId,
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
          localStorage.setItem(sessionKey(agentId), resp.session_id);
          loadWorkspace(resp.session_id);
        }
        updateMsgs(bucket, (prev) => [
            ...prev.filter((m) => m.id !== placeholderId),
            {
              ...assistantMsg,
              id: resp.message_id || placeholderId,
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
      updateMsgs(bucket, (prev) =>
        prev.some((m) => m.id === userMsg.id) ? prev : [...prev, userMsg]
      );
      // 本轮产生了新用量：刷新整会话累计
      qc.invalidateQueries({ queryKey: ["session-usage", activeSessionId] });
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
      if (val) message.success("已切换工作空间");
      else message.success("已回落到 agent 默认目录");
    } catch (e: any) {
      message.error(e?.message || "切换失败");
    }
  };

  const usOptions = [
    ...userSpaces.map((u) => ({
      value: u.id,
      label: (
        <span>
          <span
            style={{
              display: "inline-block",
              maxWidth: 70,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              verticalAlign: "middle",
            }}
          >
            {u.name}
          </span>
          <Tag color={u.read_only ? "orange" : "green"} style={{ marginLeft: 6 }}>
            {u.read_only ? "只读" : "可写"}
          </Tag>
        </span>
      ),
    })),
  ];

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

  /** 顶栏控件分组：极淡圆角底托住一组控件，形成"分段控件"观感 */
  const toolbarGroup: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 8,
    background: "var(--kp-bg-app)",
    borderRadius: 10,
    padding: 4,
  };

  // 聊天列：消息流 + 输入框（两种模式共用，外层容器不同；工作空间已移到顶栏）
  const chatInner = (
    <>
      <div
        ref={listRef}
        style={{
          flex: 1,
          overflow: "auto",
          background: "var(--kp-bg)",
          borderRadius: 18,
          padding: 20,
          boxShadow: "0 1px 3px rgba(0, 0, 0, 0.04)",
        }}
      >
        {/*
          「加载更早的消息」：会话可能很长，接口单次只给最近一段。显式按钮而不是
          无限滚动——气泡里常带着一长串思考步骤，自动加载既容易让用户失去位置感，
          一次拉太多也会明显卡。
        */}
        {paging[curBucket]?.hasMore && (
          <div
            style={{
              display: "flex",
              justifyContent: "center",
              padding: "0 0 14px",
            }}
          >
            <Button size="small" type="text" loading={loadingOlder} onClick={loadOlder}>
              加载更早的消息
            </Button>
          </div>
        )}
        {visibleMsgs.map((m) => (
          <div key={m.id}>
            <ChatBubble
              msg={m}
              agentId={agentId}
              isLocal={isLocal}
              onUsageDetail={openUsageDetail}
              onUsageDump={
                dumpEnabled && m.role === "assistant"
                  ? openUsageDump
                  : undefined
              }
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
          <div style={{ textAlign: "center", padding: 8 }}>
            <Spin size="small" />{" "}
            <Typography.Text type="secondary">检索中…</Typography.Text>
          </div>
        )}
      </div>

      {budget && budget.limit > 0 && (
        <Alert
          style={{ marginBottom: 8 }}
          type={budget.over ? "error" : budget.warning ? "warning" : "info"}
          showIcon
          message={`本会话配额：${fmtTokens(budget.used)} / ${fmtTokens(
            budget.limit
          )}（${(budget.rate * 100).toFixed(0)}%）`}
          description={
            budget.over
              ? "已超出配额上限，后续执行会被拦住。"
              : budget.warning
              ? "已用掉 80% 以上，快到上限了。"
              : undefined
          }
        />
      )}
      {sessionUsage && sessionUsage.calls > 0 && (
        <div style={{ marginTop: 6, textAlign: "right" }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            本会话累计：<UsageTag usage={sessionUsage} />
          </Typography.Text>
        </div>
      )}

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

      <div
        style={{
          display: "flex",
          alignItems: "stretch",
          gap: 10,
          marginTop: 14,
        }}
      >
        <Input.TextArea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          style={{
            height: inputH,
            resize: "none",
            borderRadius: 12,
            padding: "10px 12px",
            fontSize: 14,
          }}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        {/* 右侧列：把手在发送按钮正上方，向上拖放大输入框（64–320px） */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 8,
          }}
        >
          <div
            className="kp-grip"
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
            // 生成中：按钮变「停止」，点了立刻断流并通知后端收尾
            <Button
              danger
              onClick={stopSend}
              style={{ height: 40, minWidth: 88, marginTop: "auto" }}
            >
              停止
            </Button>
          ) : (
            <Button
              type="primary"
              onClick={() => send()}
              style={{ height: 40, minWidth: 88, marginTop: "auto" }}
            >
              发送
            </Button>
          )}
        </div>
      </div>
    </>
  );

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        position: "relative",
      }}
    >
      {/* 顶栏：分三组（会话视图 / 工作台 / 配置），细竖线分隔 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "10px 20px",
          minHeight: 52,
          // 与上方全局顶栏、下方内容卡片同为白底，只用细线分层：
          // 避免一条灰带夹在两片白之间（原来那样显脏）
          background: "var(--kp-bg)",
          borderBottom: "1px solid var(--kp-border-soft)",
          flexWrap: "wrap",
        }}
      >
        {/* 组1：会话列表 + 视图切换 */}
        <span style={toolbarGroup}>
          <Button
            size="small"
            icon={<UnorderedListOutlined />}
            onClick={() => setDrawerOpen(true)}
          >
            {mode === "task" ? "任务" : "会话"}
          </Button>
          <Select
            size="small"
            style={{ width: 110 }}
            value={mode}
            onChange={(v) => setMode(v as "chat" | "task")}
            options={[
              { label: "会话", value: "chat" },
              { label: "任务", value: "task" },
            ]}
          />
        </span>

        {/* 组2：工作台 */}
        <span style={toolbarGroup}>
          {/* 图标前置，与工作目录组对称 */}
          <CodeOutlined
            style={{ color: "var(--kp-text-muted)", fontSize: 13 }}
          />
          <Select
            size="small"
            style={{ width: 150 }}
            value={workbenchType}
            onChange={(v) => setWorkbenchType(v as "off" | "coding")}
            options={[
              { label: "工作台：关", value: "off" },
              { label: "编码工作台", value: "coding" },
            ]}
          />
        </span>

        {/* 组3：工作目录（与工作台各自成组，避免两个下拉连成一片） */}
        <span style={toolbarGroup}>
          <FolderOutlined
            style={{ color: "var(--kp-text-muted)", fontSize: 13 }}
          />
          {/* 工作空间：会话 / 任务模式共用顶栏一行（合并进下拉框） */}
          {(() => {
            const cur = sessionId ? (workspace?.user_space?.id ?? "") : userSpaceId;
            // 当前处于默认目录（""）且不在真实空间列表中时，作为兜底项显示
            const opts =
              cur && !usOptions.some((o) => o.value === cur)
                ? [...usOptions, { value: cur, label: cur === "" ? "默认" : cur }]
                : usOptions;
            return (
              <Select
                size="small"
                style={{ width: 150 }}
                value={cur}
                options={opts}
                onChange={(v) => (sessionId ? switchWorkspace(v) : setUserSpaceId(v))}
                placeholder="选择工作空间"
              />
            );
          })()}
        </span>

        {/* 组4：Agent 配置（最右，同样托底保持一致） */}
        <span style={{ ...toolbarGroup, marginLeft: "auto" }}>
          <Button
            size="small"
            icon={<SettingOutlined />}
            onClick={() => setConfigOpen(true)}
          >
            Agent 配置
          </Button>
        </span>
      </div>

      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* 工作台区域：编码工作台 = 文件浏览器 + 编辑器（叠加在工作模式左侧） */}
        {workbenchType === "coding" && (
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
                  setExplorerWidth((w) => Math.max(160, Math.min(520, w + dx)))
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
            <Resizer
              onResize={(dx) =>
                setWorkbenchWidth((w) => Math.max(360, Math.min(1100, w + dx)))
              }
            />
          </>
        )}

        {/* 工作模式区域：会话视图(对话) 或 任务视图(对话+计划) */}
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            minWidth: 0,
            padding: mode === "task" ? 0 : 16,
            boxSizing: "border-box",
          }}
        >
          {mode === "chat" ? chatInner : <TaskPage agentId={agentId} sessionId={sessionId} />}
        </div>
      </div>

      {/* Agent 配置抽屉（入口在顶栏右侧） */}
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
              width: 260,
              background: "#fff",
              borderRight: "1px solid var(--kp-border-soft)",
              boxShadow: "2px 0 8px rgba(0,0,0,0.10)",
              zIndex: 11,
              display: "flex",
              flexDirection: "column",
              animation: drawerClosing
                ? "kp-slide-out 0.34s cubic-bezier(0.22, 1, 0.36, 1) both"
                : "kp-slide-in 0.34s cubic-bezier(0.22, 1, 0.36, 1) both",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 12px",
                borderBottom: "1px solid var(--kp-border-soft)",
              }}
            >
              <Typography.Text strong>会话</Typography.Text>
              <Button
                type="text"
                size="small"
                icon={<CloseOutlined />}
                onClick={closeDrawer}
              />
            </div>
            <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
              <SessionList
                agentId={agentId}
                activeId={sessionId}
                taskMode={mode === "task"}
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
