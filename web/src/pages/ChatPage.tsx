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

export default function ChatPage({ agentId }: { agentId: string }) {
  const [msgs, setMsgs] = useState<ChatMessage[]>([welcomeMsg]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
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

  /** 拉取某会话的历史消息 */
  const loadMessages = useCallback(
    async (sid: string) => {
      try {
        const resp = await apiSessionMessages(agentId, sid);
        const list = resp.messages || [];
        const pending = resp.pending;
        setMsgs(
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
      } catch {
        setMsgs([welcomeMsg]);
      }
    },
    [agentId]
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

  useEffect(() => {
    const sid = localStorage.getItem(sessionKey(agentId));
    if (sid) {
      loadMessages(sid);
      loadWorkspace(sid);
    }
  }, [agentId, loadMessages, loadWorkspace]);

  // 消息更新、发送中，或在会话 / 任务视图间切换回来时，都停在最新一条
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs, sending, mode]);

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
    setMsgs([welcomeMsg]);
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

    setMsgs((prev) => [
      ...prev,
      { id: newId(), role: "user", text: q, created_at: new Date().toISOString() },
    ]);
    setInput("");
    setSending(true);

    // 本次运行的标识 + 可中断的 fetch：点「停止」时前端立即断流、后端尽快收尾
    const runId = newRunId();
    const controller = new AbortController();
    runIdRef.current = runId;
    abortRef.current = controller;

    // 先放一条占位回复：流式过程中逐步填充步骤，跑完再补最终内容
    const placeholderId = newId();
    placeholderRef.current = placeholderId;
    const patchPlaceholder = (patch: Partial<ChatMessage>) =>
      setMsgs((prev) =>
        prev.map((m) => (m.id === placeholderId ? { ...m, ...patch } : m))
      );
    setMsgs((prev) => [
      ...prev,
      {
        id: placeholderId,
        role: "assistant",
        text: "",
        steps: [],
        created_at: new Date().toISOString(),
        streaming: true,
      },
    ]);

    // 逐字累积 + 按帧刷新：token 很密，一帧一次 setState 就够跟手了
    let acc = "";
    let raf = 0;
    const flushDelta = () => {
      raf = 0;
      patchPlaceholder({ text: acc });
    };

    try {
      await apiChatStream(
        agentId,
        q,
        {
          sessionId,
          stepId,
          // 仅新建会话时传待选用户空间；已有会话忽略（沿用库里绑定）
          userSpaceId: sessionId ? undefined : userSpaceId,
          runId,
          signal: controller.signal,
        },
        {
          // 每完成一步：追加到占位消息的思考过程里
          onStep: (s) =>
            setMsgs((prev) =>
              prev.map((m) =>
                m.id === placeholderId ? { ...m, steps: [...(m.steps ?? []), s] } : m
              )
            ),
          // 逐字输出：累积到下一帧再统一刷新，避免每个 token 都整页重渲染
          onDelta: (t) => {
            acc += t;
            if (!raf) raf = requestAnimationFrame(flushDelta);
          },
          onDone: (resp) => {
            if (resp.session_id && resp.session_id !== sessionId) {
              setSessionId(resp.session_id);
              localStorage.setItem(sessionKey(agentId), resp.session_id);
              loadWorkspace(resp.session_id); // 会话已建，拉取生效工作空间
            }
            patchPlaceholder({
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
            });
          },
          onError: (msg) =>
            patchPlaceholder({
              text: "执行失败。",
              error: msg,
              streaming: false,
            }),
        }
      );
    } catch (e: any) {
      // 流式不可用（后端未重启 / 网络问题）：回退到一次性接口，语义不变
      try {
        const resp =
          (await apiChat(
            agentId,
            q,
            sessionId,
            stepId,
            sessionId ? undefined : userSpaceId
          )) || { answer: "", llm: false };
        if (resp.session_id && resp.session_id !== sessionId) {
          setSessionId(resp.session_id);
          localStorage.setItem(sessionKey(agentId), resp.session_id);
          loadWorkspace(resp.session_id);
        }
        patchPlaceholder({
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
        });
      } catch (e2: any) {
        patchPlaceholder({
          text: "查询失败，请确认 keeper 服务已启动。",
          error: e2?.message || String(e2),
          streaming: false,
        });
      }
    } finally {
      if (raf) cancelAnimationFrame(raf);
      abortRef.current = null;
      runIdRef.current = "";
      placeholderRef.current = "";
      setSending(false);
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
   */
  const stopSend = () => {
    const pid = placeholderRef.current;
    const rid = runIdRef.current;
    // 1. 通知后端优雅停止（should_stop 在下一个检查点生效）
    if (rid) {
      void apiAbortChat(agentId, rid).catch(() => {});
    }
    // 2. UI 立即反馈「已停止」，但保留占位消息等 done 帧补内容/标记
    setSending(false);
    if (pid) {
      setMsgs((prev) =>
        prev.map((m) =>
          m.id === pid ? { ...m, streaming: false, canceled: true } : m
        )
      );
    }
    // 3. 兜底：超时仍未结束则硬断流（仅当后端可能被不可取消操作卡住）
    const ctrl = abortRef.current;
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
        {msgs.map((m) => (
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
