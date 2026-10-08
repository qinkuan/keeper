import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Empty,
  Input,
  Modal,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import {
  CaretDownOutlined,
  CaretRightOutlined,
  CheckCircleFilled,
  CheckCircleOutlined,
  CloseCircleOutlined,
  PlusOutlined,
  RedoOutlined,
} from "@ant-design/icons";
import ChatBubble from "../components/ChatBubble";
import {
  apiAnswerTask,
  apiAnswerTaskStream,
  apiExecuteTask,
  apiExecuteTaskStream,
  apiPauseTask,
  apiGetSettings,
  apiGetTask,
  apiListTasks,
  apiPatchTaskItems,
  apiPlanApprove,
  apiPlanGenerate,
  apiPlanReplan,
  apiReviewTask,
  apiSessionMessages,
  apiStartTask,
  apiUpdateTask,
  apiTaskMetrics,
  apiToolStats,
} from "../api/client";
import type { StreamHandlers } from "../api/client";
import ArtifactCard from "../components/ArtifactCard";
import PromptDumpModal from "../components/PromptDumpModal";
import ToolStatsTable from "../components/ToolStatsTable";
import UsageDetailModal from "../components/UsageDetailModal";
import { UsageTag } from "../components/UsageTag";
import type {
  Artifact,
  UsageSummary,
  ToolStat,
  ChatMessage,
  SessionMessage,
  Task,
  TaskDetailResp,
  TaskItem,
  TaskItemStatus,
  TaskReviewResp,
  TaskStatus,
} from "../api/types";

const statusColor: Record<TaskStatus, string> = {
  draft: "default",
  planning: "processing",
  plan_review: "warning",
  executing: "processing",
  waiting_input: "warning",
  waiting_review: "gold",
  done: "success",
  failed: "error",
};
const statusLabel: Record<TaskStatus, string> = {
  draft: "草稿",
  planning: "规划中",
  plan_review: "待审计划",
  executing: "执行中",
  waiting_input: "待补充输入",
  waiting_review: "待验收",
  done: "已完成",
  failed: "已失败",
};
const itemColor: Record<TaskItemStatus, string> = {
  pending: "default",
  doing: "processing",
  done: "success",
  skipped: "default",
};
const itemLabel: Record<TaskItemStatus, string> = {
  pending: "待办",
  doing: "进行中",
  done: "已完成",
  skipped: "已跳过",
};

interface FeedbackModal {
  title: string;
  placeholder: string;
  required?: boolean;
  submit: (text: string) => Promise<void>;
}

export default function TaskPage({ agentId, sessionId }: { agentId: string; sessionId?: string | null }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selId, setSelId] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  /** 可观测：本任务的累计用量（token / 缓存命中 / 成本 / 耗时） */
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  /** 可观测：本任务的工具维度统计（按耗时排序，定位最慢 / 返回最大的工具） */
  const [toolStats, setToolStats] = useState<ToolStat[]>([]);
  const [items, setItems] = useState<TaskItem[]>([]);
  /** 任务绑定会话的真实消息（与「会话模式」同源，统一从这里渲染对话） */
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{
    stepId?: string;
    options?: string[];
    question?: string | null;
  } | null>(null);
  /** 计划 / 计划点 / 产物等任务内容是否收起（默认收起，把空间留给对话） */
  const [contentCollapsed, setContentCollapsed] = useState(true);
  /** 工具统计弹窗的开关（默认关）：它按耗时排序，是「排查慢在哪」时才翻的诊断表。
   *  曾试过在标题行里就地展开，但那会挤占计划点的展示空间、还要重排标题布局，
   *  索性走弹窗——与消息里的「查看详情 / 查看 dump」同一套交互。 */
  const [statsOpen, setStatsOpen] = useState(false);

  /** 护栏：同一点连续这么多轮仍未调用 task.mark_item_done 登记完成，就停下来问用户。
   *  不设上限的话，「一次性推进整个计划」会一直重跑到后端 MAX_ROUNDS_PER_ITEM=30，
   *  把同一件事做十几遍、白烧大量 token（实测 agent 偶尔会用自然语言宣布
   *  「第 N 步已完成」而忘了登记，框架便以为还没做完）。 */
  const STALL_ROUNDS = 3;
  /** 触发护栏的计划点；null = 未触发 */
  const [stalled, setStalled] = useState<TaskItem | null>(null);
  /** 用户已选择「再试一轮」的次数：每让一次，下一次提示的门槛按 STALL_ROUNDS
   *  逐次放宽，避免刚点「再试」就被同一个弹窗立刻再拦一次。 */
  const stallGraceRef = useRef<Record<string, number>>({});
  const [fb, setFb] = useState<FeedbackModal | null>(null);
  const [fbText, setFbText] = useState("");

  const [newItem, setNewItem] = useState("");
  const [editOpen, setEditOpen] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editDesc, setEditDesc] = useState("");

  /** 整计划执行：是否正在跑整个计划；pausedRef 为暂停开关（循环里读取，避免重渲染竞态） */
  const pausedRef = useRef(false);
  const [executing, setExecuting] = useState(false);

  /** 流式执行时的临时气泡：实时展示步骤与逐字回答，结束后由服务端消息接管 */
  const [streamMsg, setStreamMsg] = useState<ChatMessage | null>(null);
  /** 当前流式的中止控制器（停止生成） */
  const abortRef = useRef<AbortController | null>(null);

  /** 可观测：单条消息的「查看详情」与「查看 dump」入口。
   *  任务视图的对话与「会话模式」同源（都渲染 ChatBubble），这两个入口本该同样
   *  可用；此前本页既没把 usage 映射进 msg、也没接回调，于是任务会话里连用量标签
   *  都不渲染。会话模式那两个 modal 由 ChatPage 持有，本页复用同一对组件。 */
  const [detailMessageId, setDetailMessageId] = useState<string | null>(null);
  const [dumpMessageId, setDumpMessageId] = useState<string | null>(null);
  /** 落盘开关：与设置页 / 会话模式共用同一 queryKey，命中缓存不额外请求。
   *  没开就不显示「查看 dump」入口（否则点了只会拿到空）。 */
  const settingsQ = useQuery({ queryKey: ["settings"], queryFn: apiGetSettings });
  const dumpEnabled = !!settingsQ.data?.prompt_dump?.enabled;
  // 稳定引用：这两个会传给 memo 化的 ChatBubble，写成内联箭头函数会让 memo 失效
  const openUsageDetail = useCallback((mid: string) => setDetailMessageId(mid), []);
  const openUsageDump = useCallback((mid: string) => setDumpMessageId(mid), []);

  /** 对话区容器：切换任务 / 新消息到达后都滚到最底部（与刚进入时一致） */
  const convRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = convRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy, task?.id, streamMsg]);

  /** 拉取任务绑定会话的真实消息，并据服务端挂起信息驱动「待回答」输入区 */
  const loadMessages = useCallback(
    async (sessionId: string) => {
      if (!sessionId) {
        setMessages([]);
        setPending(null);
        return;
      }
      try {
        const resp = await apiSessionMessages(agentId, sessionId);
        setMessages(resp.messages || []);
        // 服务端 pending 是「待回答」的权威来源（waiting_input 时才有）
        setPending(
          resp.pending
            ? {
                stepId: resp.pending.step_id,
                options: resp.pending.options ?? undefined,
                question: resp.pending.question,
              }
            : null
        );
      } catch {
        setMessages([]);
      }
    },
    [agentId]
  );

  const refreshList = useCallback(async () => {
    try {
      const r = await apiListTasks(agentId);
      setTasks(r.tasks || []);
    } catch {
      /* ignore */
    }
  }, [agentId]);

  const loadDetail = useCallback(
    async (id: string) => {
      const r = await apiGetTask(agentId, id);
      setTask(r.task);
      setItems(r.items || []);
      // 用量加载失败不影响任务详情本身（未配单价等情况下成本为 null）
      setUsage(await apiTaskMetrics(id).catch(() => null));
      setToolStats(await apiToolStats({ task_id: id }).catch(() => []));
      await loadMessages(r.task.session_id ?? "");
      return r;
    },
    [agentId, loadMessages]
  );

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  // 外部（顶部「任务」抽屉）选中某个任务会话时，按 session_id 定位并打开对应任务
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await apiListTasks(agentId);
        if (cancelled) return;
        const tasks = r.tasks || [];
        setTasks(tasks);
        const match = tasks.find((t) => t.session_id === sessionId);
        if (match) {
          setSelId(match.id);
          await loadDetail(match.id);
        }
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, agentId, loadDetail]);

  const selectTask = async (id: string) => {
    setSelId(id);
    await loadDetail(id);
  };

  /** 统一的动作收尾：刷新任务/计划点，并重新拉取会话消息（权威对话源） */
  const afterAction = (_r: TaskDetailResp, _userText?: string) => {
    const r = _r;
    setTask(r.task);
    setItems(r.items || []);
    refreshList();
    void loadMessages(r.task.session_id ?? "");
  };

  const run = async (fn: () => Promise<TaskDetailResp>, _userText?: string) => {
    setBusy(true);
    try {
      const r = await fn();
      afterAction(r);
    } catch (e: any) {
      message.error(e?.message || "操作失败");
    } finally {
      setBusy(false);
    }
  };

  /**
   * 流式跑一次任务动作（执行 / 补充输入）：执行期间用临时气泡实时展示步骤与
   * 逐字回答，结束后清掉临时气泡、以服务端权威结果为准；被停止时返回 null。
   */
  const runTaskStream = async (
    start: (
      runId: string,
      handlers: StreamHandlers,
      signal: AbortSignal
    ) => Promise<TaskDetailResp | null>
  ) => {
    const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const id = `stream-${runId}`;
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    let acc = "";
    let raf = 0;
    const patch = (p: Partial<ChatMessage>) =>
      setStreamMsg((m) => (m && m.id === id ? { ...m, ...p } : m));
    const flush = () => {
      raf = 0;
      patch({ text: acc });
    };
    const finish = () => {
      if (raf) cancelAnimationFrame(raf);
      abortRef.current = null;
      setStreamMsg(null);
    };

    setStreamMsg({
      id,
      role: "assistant",
      text: "",
      steps: [],
      created_at: new Date().toISOString(),
      streaming: true,
    });

    try {
      return await start(
        runId,
        {
          onStep: (s) =>
            setStreamMsg((m) =>
              m && m.id === id ? { ...m, steps: [...(m.steps ?? []), s] } : m
            ),
          // 逐字很密：累积到下一帧再统一刷新，避免每个 token 都整页重渲染
          onDelta: (t) => {
            acc += t;
            if (!raf) raf = requestAnimationFrame(flush);
          },
          onAbort: () => {
            // 已通知后端停止：重新拉消息，用库里的权威内容替换临时气泡
            void loadMessages(task?.session_id ?? "");
          },
        },
        ctrl.signal
      );
    } finally {
      finish();
    }
  };

  /** 流式推进一轮执行（含任务 / 计划点的状态刷新） */
  const executeOnceStream = async (taskId: string) => {
    let r: TaskDetailResp | null;
    try {
      r = await runTaskStream((runId, handlers, signal) =>
        apiExecuteTaskStream(agentId, taskId, 1, runId, handlers, signal)
      );
    } catch {
      // 流式不可用（后端未重启 / 网络问题）：回退到一次性接口，语义不变
      r = (await apiExecuteTask(agentId, taskId, 1)) as TaskDetailResp;
    }
    if (r) {
      setTask(r.task);
      setItems(r.items || []);
      await loadMessages(r.task.session_id ?? "");
    }
    return r;
  };

  /** 流式回答 agent 的追问（与「执行」共用同一套实时展示） */
  const answerStream = async (text: string) => {
    if (!task) return;
    setBusy(true);
    try {
      let r: TaskDetailResp | null;
      try {
        r = await runTaskStream((runId, handlers, signal) =>
          apiAnswerTaskStream(agentId, task.id, text, runId, handlers, signal)
        );
      } catch {
        // 同上：流式不可用时回退到一次性接口
        r = (await apiAnswerTask(agentId, task.id, text)) as TaskDetailResp;
      }
      if (r) afterAction(r);
    } catch (e: any) {
      message.error(e?.message || "回答失败");
    } finally {
      setBusy(false);
    }
  };

  /** 停止生成：立刻断流，后端在下一个检查点收尾（已产出的内容保留） */
  const stopStream = () => {
    abortRef.current?.abort();
  };

  /** 一次性推进整个计划：循环调用 execute，直到任务离开执行态、等人工回答，
   *  或用户点击「暂停」。每次调用最多跑 10 轮（与后端上限一致），暂停在两次
   *  调用之间生效——所以「执行 → 暂停 → 执行」可以无缝续跑。 */
  const runWholePlan = async () => {
    if (!selId) return;
    pausedRef.current = false;
    setExecuting(true);
    setBusy(true);
    try {
      let r: TaskDetailResp | TaskReviewResp = await apiGetTask(agentId, selId);
      setTask(r.task);
      setItems(r.items || []);
      await loadMessages(r.task.session_id ?? "");
      while (!pausedRef.current) {
        const st = r.task.status;
        // 等人工回答 / 已到终态（待验收、完成、失败）就停
        if (st !== "executing") break;
        // 流式推进：这一轮的步骤 / 回答实时显示，返回 null 表示被用户停止
        const rr = await executeOnceStream(selId);
        if (!rr) break;
        r = rr;
        // 后端按 step 暂停：跑完当前这一步就停了，已完成的步骤都已落库。
        // result 由 execute / review 返回（TaskReviewResp），联合类型需断言。
        if ((r as TaskReviewResp).result === "paused") {
          pausedRef.current = true;
          break;
        }
        // 护栏：这一点跑了 STALL_ROUNDS×(已让步次数+1) 轮仍未登记完成，停下来让用户
        // 拍板。result === "running" 就是「本轮跑完了，但这点还没登记」的信号——
        // 不拦住它就会静默重跑同样的活儿。
        if ((r as TaskReviewResp).result === "running") {
          const cur = (r.items || []).find(
            (i) => i.status === "pending" || i.status === "doing"
          );
          if (cur) {
            const grace = stallGraceRef.current[cur.id] ?? 0;
            if (cur.rounds >= STALL_ROUNDS * (grace + 1)) {
              setStalled(cur);
              break;
            }
          }
        }
      }
      refreshList();
      if (pausedRef.current) message.info("已暂停，可继续点击「执行」从断点续跑");
    } catch (e: any) {
      message.error(e?.message || "执行失败");
    } finally {
      setExecuting(false);
      setBusy(false);
      pausedRef.current = false;
    }
  };

  /** 护栏弹窗的三个出口：再试一轮 / 标记已完成 / 跳过这一步。
   *
   *  「再试一轮」**不重置 rounds**（patch 接口不改这个字段，硬改就得让后端加
   *  「重置轮数」语义），而是把该点的提示门槛按 STALL_ROUNDS 逐次放宽：让过
   *  一次就再容忍 3 轮。既不会刚点「再试」又被同一个弹窗立刻拦下，也不至于
   *  无限放行。
   */
  const resolveStall = async (action: "retry" | "done" | "skip") => {
    const it = stalled;
    if (!it) return;
    setStalled(null);
    if (action === "retry") {
      stallGraceRef.current[it.id] = (stallGraceRef.current[it.id] ?? 0) + 1;
      void runWholePlan();
      return;
    }
    await patchItemStatus(it, action === "done" ? "done" : "skipped");
  };

  /** 暂停：置后端 tasks.paused，ReAct 每跑完一步都会检查它，命中就停在下一步之前 */
  const pauseRun = async () => {
    if (!selId) return;
    pausedRef.current = true; // 阻止循环再发下一次 execute
    try {
      await apiPauseTask(agentId, selId, true);
      message.info("正在暂停…当前这一步跑完即停");
    } catch (e: any) {
      message.error(e?.message || "暂停失败");
    }
  };

  const openFb = (m: Omit<FeedbackModal, "submit"> & { submit: (t: string) => Promise<void> }) =>
    setFb(m);

  /** 产出计划：草稿先 start（draft→planning），再 generate（planning→plan_review）。
   *  打回后回到 planning，此时直接 generate 即可重出同版本计划。 */
  const generatePlan = async () => {
    if (!task) return;
    setBusy(true);
    try {
      if (task.status === "draft") await apiStartTask(agentId, task.id);
      const r = await apiPlanGenerate(agentId, task.id);
      afterAction(r);
    } catch (e: any) {
      message.error(e?.message || "产出计划失败");
    } finally {
      setBusy(false);
    }
  };

  const openEdit = () => {
    if (!task) return;
    setEditTitle(task.title || "");
    setEditDesc(task.description_md || "");
    setEditOpen(true);
  };
  const saveEdit = async () => {
    if (!task) return;
    setBusy(true);
    try {
      const r = await apiUpdateTask(agentId, task.id, {
        title: editTitle.trim() || "未命名任务",
        description_md: editDesc.trim() || undefined,
      });
      setTask(r.task);
      setEditOpen(false);
      await refreshList();
      message.success("已保存");
    } catch (e: any) {
      message.error(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  };

  /** 标记某计划点完成 / 跳过 */
  const patchItemStatus = async (it: TaskItem, status: TaskItemStatus) => {
    if (!selId) return;
    setBusy(true);
    try {
      const r = await apiPatchTaskItems(agentId, selId, [
        { id: it.id, content_md: it.content_md, status, action: "update" },
      ]);
      setTask(r.task);
      setItems(r.items || []);
      await refreshList();
      message.success(status === "done" ? "已标记完成" : "已跳过");
    } catch (e: any) {
      message.error(e?.message || "更新失败");
    } finally {
      setBusy(false);
    }
  };

  const addItem = async () => {
    const text = newItem.trim();
    if (!text || !selId) return;
    setBusy(true);
    try {
      const r = await apiPatchTaskItems(agentId, selId, [
        { content_md: text, action: "add" },
      ]);
      setTask(r.task);
      setItems(r.items || []);
      setNewItem("");
      await refreshList();
    } catch (e: any) {
      message.error(e?.message || "添加失败");
    } finally {
      setBusy(false);
    }
  };

  // -------- 渲染 --------
  const status = task?.status;
  const sortedItems = [...items].sort((a, b) => a.seq - b.seq);

  /** 本次任务会话里产出的文件（agent 用 fs.publish 挂在消息上的那些）。
   *  它们带 messageId，所以能用卡片预览 / 下载；在「任务内容」里集中展示，
   *  方便一眼看到这次任务到底产出了什么（不必去对话里翻）。 */
  const msgArtifacts = messages.flatMap((m) =>
    (m.artifacts || []).map((a) => ({ artifact: a, messageId: m.id }))
  );

  /** 最终**交付**产物：只展示任务级登记过的文件。
   *
   *  同一个文件被反复修改时取最后一次挂出的那个。注意预览走的是
   *  ``artifactUrl(agentId, messageId, artifact.id)``——读的是**工作区当前内容**，
   *  不是历史快照，所以拿到的永远是最新版本，不存在「取到旧版」的问题。
   *
   *  为什么要按 ``task.artifacts`` 过滤：跑一个任务会产出一堆过程产物——临时测试
   *  脚本（check.py / test_step2.js）、补丁脚本（patch_*.js），以及同一文件的多个
   *  中间版本。它们各自挂在消息上（那里才是看过程的地方），不该混进「最终产物」；
   *  而且其中一部分在收尾时已被 agent 清理掉，列出来点了预览直接404。
   *
   *  ``task.artifacts`` 为空（agent 还没登记 / 手工勾完成的旧任务）时退回
   *  「全部按名去重」，否则会把产物整块藏掉。
   */
  const finalArtifacts = (() => {
    const delivered = new Set(
      (task?.artifacts ?? []).map((a) => a.name).filter(Boolean)
    );
    const merged = new Map<string, { artifact: Artifact; messageId: string }>();
    msgArtifacts.forEach((x, i) => {
      // 前端 Artifact 只有 id / name / mime / size（path 是后端内部字段）
      if (delivered.size > 0 && !delivered.has(x.artifact.name)) return;
      const key = x.artifact.name || x.artifact.id;
      merged.set(key || `__anon_${i}`, x);
    });
    return [...merged.values()];
  })();

  /** 任务绑定会话的真实对话（与「会话模式」同源，统一渲染方式） */
  const renderConversation = () => {
    if (messages.length === 0) {
      return (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          暂无对话。执行 / 验收 / 打回后，agent 的回答会按会话真实记录显示在这里（与「会话模式」一致）。
        </Typography.Text>
      );
    }
    return (
      <>
        {messages.map((m) => (
          <ChatBubble
            key={m.id}
            msg={{
              id: m.id,
              role: m.role,
              text: m.content,
              created_at: m.created_at ?? null,
              artifacts: m.artifacts,
              steps: m.steps ?? [],
              // 任务会话里的 assistant 回复同样是 agent 生成的
              llm: m.role === "assistant",
              // 可观测：这两项后端 load_messages 已按 message_id 聚合好。
              // 不映射的话 ChatBubble 收不到 usage，用量标签整块不渲染——任务会话
              // 里连「用了多少 token」都看不到，更没有下钻入口。
              usage: m.usage,
              duration_ms: m.duration_ms,
            }}
            agentId={agentId}
            onUsageDetail={openUsageDetail}
            onUsageDump={
              dumpEnabled && m.role === "assistant" ? openUsageDump : undefined
            }
          />
        ))}
        {/* 流式执行中的临时气泡：结束 / 被停止后由服务端消息接管 */}
        {streamMsg && <ChatBubble msg={streamMsg} agentId={agentId} />}
      </>
    );
  };

  return (
    <div style={{ display: "flex", height: "100%" }}>
      {/* 任务详情（任务由顶部「任务」抽屉选中，占满工作区） */}
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", padding: 20, boxSizing: "border-box" }}>
        {!task ? (
          <Empty
            style={{ marginTop: 80 }}
            description="从顶部选择或新建一个任务"
          />
        ) : (
          <>
            {/* 任务内容（计划 / 计划点 / 产物），可收起；标题行合并展示任务元信息 */}
            <div style={{ border: "1px solid var(--kp-border-soft)", borderRadius: 8, marginBottom: 12, flex: "0 0 auto", maxHeight: "55%", overflow: "auto" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 12px",
                  cursor: "pointer",
                }}
                onClick={() => setContentCollapsed((c) => !c)}
              >
                <Button
                  type="text"
                  size="small"
                  icon={contentCollapsed ? <CaretRightOutlined /> : <CaretDownOutlined />}
                />
                <Typography.Text
                  strong
                  style={{ fontSize: 15, color: "var(--kp-text-strong)" }}
                >
                  {task.title || "未命名任务"}
                </Typography.Text>
                <Tag color={statusColor[task.status]}>{statusLabel[task.status]}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  计划版本 v{task.plan_version ?? 1} · 计划打回 {task.plan_reject_count ?? 0} · 验收打回{" "}
                  {task.review_reject_count ?? 0}
                </Typography.Text>
                {usage && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {" · "}
                    <UsageTag usage={usage} />
                  </Typography.Text>
                )}
                {toolStats.length > 0 && (
                  <div style={{ marginTop: 8, width: "100%" }}>
                    <Typography.Link
                      style={{ fontSize: 12 }}
                      onClick={(e) => {
                        // 标题行整体可点（折叠任务内容区），这里只开弹窗
                        e.stopPropagation();
                        setStatsOpen(true);
                      }}
                    >
                      {`工具统计 · ${toolStats.length} 个工具（点击查看）`}
                    </Typography.Link>
                  </div>
                )}
                <span
                  style={{ marginLeft: "auto", display: "flex", gap: 8, cursor: "default" }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <Button size="small" onClick={openEdit}>
                    编辑
                  </Button>
                  <Button
                    size="small"
                    onClick={() => loadDetail(task.id)}
                    loading={busy}
                  >
                    刷新
                  </Button>
                  {(status === "executing" || status === "waiting_input") && (
                    <>
                      <Button
                        type="primary"
                        size="small"
                        loading={executing}
                        disabled={executing || status !== "executing"}
                        onClick={runWholePlan}
                      >
                        {executing ? "执行中…" : "执行"}
                      </Button>
                      <Button
                        size="small"
                        danger
                        disabled={!executing}
                        onClick={pauseRun}
                      >
                        暂停
                      </Button>
                      <Button
                        size="small"
                        disabled={!streamMsg}
                        onClick={stopStream}
                      >
                        停止生成
                      </Button>
                    </>
                  )}
                </span>
              </div>
              {!contentCollapsed && (
              <div style={{ padding: "0 12px 12px" }}>
            {task.description_md && (
              <div
                style={{
                  whiteSpace: "pre-wrap",
                  background: "var(--kp-surface)",
                  border: "1px solid var(--kp-border-soft)",
                  borderRadius: 8,
                  padding: 12,
                  marginBottom: 12,
                  fontSize: 13,
                }}
              >
                {task.description_md}
              </div>
            )}

            {/* 操作条 */}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
              {(status === "draft" || status === "planning") && (
                <Button type="primary" loading={busy} onClick={generatePlan}>
                  {status === "draft" ? "启动并产出计划" : "产出计划"}
                </Button>
              )}

              {status === "plan_review" && (
                <>
                  <Button
                    type="primary"
                    icon={<CheckCircleOutlined />}
                    loading={busy}
                    onClick={() =>
                      openFb({
                        title: "通过计划（可附意见，可选）",
                        placeholder: "选填：通过时附带的修改意见",
                        submit: async (t) => {
                          await run(() =>
                            apiPlanApprove(agentId, task.id, true, t || undefined)
                          );
                        },
                      })
                    }
                  >
                    通过计划
                  </Button>
                  <Button
                    danger
                    loading={busy}
                    onClick={() =>
                      openFb({
                        title: "打回修改（不升版，必填反馈）",
                        placeholder: "请说明哪里需要调整，将回到规划态同版本重出",
                        required: true,
                        submit: async (t) => {
                          await run(() => apiPlanApprove(agentId, task.id, false, t));
                        },
                      })
                    }
                  >
                    打回修改
                  </Button>
                  <Button
                    icon={<RedoOutlined />}
                    loading={busy}
                    onClick={() =>
                      openFb({
                        title: "重新规划（升版，可附方向，可选）",
                        placeholder: "选填：重新规划的方向或约束",
                        submit: async (t) => {
                          await run(() =>
                            apiPlanReplan(agentId, task.id, t || undefined)
                          );
                        },
                      })
                    }
                  >
                    重新规划
                  </Button>
                </>
              )}

              {(status === "executing" || status === "waiting_input") && (
                <Button
                  icon={<RedoOutlined />}
                  loading={busy}
                  onClick={() =>
                    openFb({
                      title: "重新规划（可附反馈，可选）",
                      placeholder: "选填：重新规划的方向或约束",
                      submit: async (t) => {
                        await run(() =>
                          apiPlanReplan(agentId, task.id, t || undefined)
                        );
                      },
                    })
                  }
                >
                  重新规划
                </Button>
              )}

              {status === "waiting_review" && (
                <>
                  <Button
                    type="primary"
                    icon={<CheckCircleOutlined />}
                    loading={busy}
                    onClick={() =>
                      openFb({
                        title: "验收通过（可附备注，可选）",
                        placeholder: "选填：验收备注",
                        submit: async (t) => {
                          await run(() =>
                            apiReviewTask(agentId, task.id, "approve", undefined, t || undefined)
                          );
                        },
                      })
                    }
                  >
                    验收通过
                  </Button>
                  <Button
                    danger
                    icon={<CloseCircleOutlined />}
                    loading={busy}
                    onClick={() =>
                      openFb({
                        title: "验收打回（必填反馈）",
                        placeholder: "请说明验收不通过的原因，agent 会在原会话继续修改",
                        required: true,
                        submit: async (t) => {
                          await run(() =>
                            apiReviewTask(agentId, task.id, "reject", undefined, t)
                          );
                        },
                      })
                    }
                  >
                    验收打回
                  </Button>
                </>
              )}

              {(status === "done" || status === "failed") && (
                <Typography.Text type={status === "failed" ? "danger" : "success"}>
                  {status === "done" ? "任务已完成" : "任务已失败"}
                </Typography.Text>
              )}
            </div>

            {/* 验收/计划反馈展示 */}
            {task.plan_feedback && (
              <div style={{ marginBottom: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  计划反馈：
                </Typography.Text>
                <div
                  style={{
                    whiteSpace: "pre-wrap",
                    background: "#fdf6e8",
                    border: "1px solid #f2dfae",
                    borderRadius: 6,
                    padding: 8,
                    fontSize: 13,
                  }}
                >
                  {task.plan_feedback}
                </div>
              </div>
            )}
            {task.review_feedback && (
              <div style={{ marginBottom: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  验收反馈：
                </Typography.Text>
                <div
                  style={{
                    whiteSpace: "pre-wrap",
                    background: "#fdf2f1",
                    border: "1px solid #f3c9c5",
                    borderRadius: 6,
                    padding: 8,
                    fontSize: 13,
                  }}
                >
                  {task.review_feedback}
                </div>
              </div>
            )}

            {/* 计划 */}
            {task.plan_md && (
              <div style={{ marginBottom: 16 }}>
                <Typography.Text strong>计划（plan_md）</Typography.Text>
                <div
                  style={{
                    background: "var(--kp-surface)",
                    border: "1px solid var(--kp-border-soft)",
                    borderRadius: 8,
                    padding: 12,
                    marginTop: 6,
                    fontSize: 13,
                  }}
                >
                  {/* plan_md 里完成项是标准 markdown 的 `- [x]`（后端事实与解析都依赖
                      这个写法），这里只在显示层换成勾：实心绿勾=已完成，灰色空心=
                      待做，灰色叉+删除线=已作废 */}
                  {task.plan_md.split("\n").map((line, i) => {
                    const isItem = /^-\s*\[[ xX]\]/.test(line);
                    if (!isItem) {
                      return (
                        <div key={i} style={{ whiteSpace: "pre-wrap" }}>
                          {line}
                        </div>
                      );
                    }
                    const isDone = /^-\s*\[[xX]\]/.test(line);
                    const isSkipped = line.includes("~~");
                    const body = line
                      .replace(/^-\s*\[[ xX]\]\s*/, "")
                      .replace(/~~/g, "")
                      .replace(/（已作废）/g, "");
                    return (
                      <div
                        key={i}
                        style={{
                          display: "flex",
                          gap: 6,
                          alignItems: "flex-start",
                          marginBottom: 2,
                        }}
                      >
                        {isDone ? (
                          <CheckCircleFilled
                            style={{ color: "#3fa46a", marginTop: 4, fontSize: 13 }}
                          />
                        ) : isSkipped ? (
                          <CloseCircleOutlined
                            style={{ color: "var(--kp-text-muted)", marginTop: 4, fontSize: 13 }}
                          />
                        ) : (
                          <CheckCircleOutlined
                            style={{ color: "var(--kp-border)", marginTop: 4, fontSize: 13 }}
                          />
                        )}
                        <span
                          style={{
                            textDecoration: isSkipped ? "line-through" : undefined,
                            color: isSkipped ? "var(--kp-text-muted)" : "var(--kp-text)",
                          }}
                        >
                          {body}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 计划点列表 */}
            <div style={{ marginBottom: 16 }}>
              <Typography.Text strong>计划点（{sortedItems.length}）</Typography.Text>
              <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
                {sortedItems.length === 0 && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    尚无计划点。通过「产出计划」生成，或在下方手动添加。
                  </Typography.Text>
                )}
                {sortedItems.map((it) => (
                  <div
                    key={it.id}
                    style={{
                      border: "1px solid var(--kp-border-soft)",
                      borderRadius: 8,
                      padding: 10,
                      background: "#fff",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      {it.status === "done" && (
                        <CheckCircleFilled style={{ color: "#3fa46a", fontSize: 14 }} />
                      )}
                      <Typography.Text strong style={{ fontSize: 13 }}>
                        #{it.seq}
                      </Typography.Text>
                      <Tag color={itemColor[it.status]}>{itemLabel[it.status]}</Tag>
                      {it.rounds > 0 && (
                        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                          {it.rounds} 轮
                        </Typography.Text>
                      )}
                      {(it.status === "pending" || it.status === "doing") && (
                        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                          <Button
                            size="small"
                            type="link"
                            onClick={() => patchItemStatus(it, "done")}
                          >
                            标记完成
                          </Button>
                          <Button
                            size="small"
                            type="link"
                            danger
                            onClick={() => patchItemStatus(it, "skipped")}
                          >
                            跳过
                          </Button>
                        </span>
                      )}
                    </div>
                    <div
                      style={{
                        whiteSpace: "pre-wrap",
                        fontSize: 13,
                        marginTop: 4,
                      }}
                    >
                      {it.content_md}
                    </div>
                    {it.conclusion && (
                      <div
                        style={{
                          whiteSpace: "pre-wrap",
                          fontSize: 12,
                          color: "#2f7a4f",
                          marginTop: 6,
                          background: "#f0f9f4",
                          borderRadius: 6,
                          padding: 8,
                        }}
                      >
                        结论：{it.conclusion}
                      </div>
                    )}
                  </div>
                ))}
              </div>

              {/* 手动添加计划点 */}
              {(status === "plan_review" ||
                status === "executing" ||
                status === "waiting_input" ||
                status === "waiting_review") && (
                <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                  <Input
                    placeholder="补充一个计划点（描述要做的事）"
                    value={newItem}
                    onChange={(e) => setNewItem(e.target.value)}
                    onPressEnter={addItem}
                  />
                  <Button icon={<PlusOutlined />} onClick={addItem} loading={busy}>
                    添加
                  </Button>
                </div>
              )}
            </div>

            {/* 任务级产物 / 总结 */}
            {task.result_summary && (
              <div style={{ marginBottom: 16 }}>
                <Typography.Text strong>总结</Typography.Text>
                <div
                  style={{
                    whiteSpace: "pre-wrap",
                    background: "#f0f9f4",
                    border: "1px solid #bfe3cd",
                    borderRadius: 8,
                    padding: 12,
                    marginTop: 6,
                    fontSize: 13,
                  }}
                >
                  {task.result_summary}
                </div>
              </div>
            )}
            {/* 最终交付产物：只呈现任务级登记过的文件；临时测试脚本、中间版本
                这些过程产物仍留在对话消息里可查 */}
            {finalArtifacts.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <Typography.Text strong>
                  最终交付产物（{finalArtifacts.length}）
                </Typography.Text>
                <div style={{ marginTop: 8 }}>
                  {finalArtifacts.map(({ artifact, messageId }, i) => (
                    <ArtifactCard
                      key={`${messageId}-${artifact.id ?? i}`}
                      artifact={artifact}
                      agentId={agentId}
                      messageId={messageId}
                    />
                  ))}
                </div>
              </div>
            )}
              </div>
              )}
            </div>

            {/* 对话延续区：占满剩余高度，独立滚动到底部 */}
            <div style={{ marginTop: 8, flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
              <Typography.Text strong style={{ flex: "0 0 auto" }}>执行过程 / 对话</Typography.Text>
              <div
                ref={convRef}
                style={{
                  marginTop: 8,
                  flex: 1,
                  minHeight: 0,
                  background: "var(--kp-bg)",
                  borderRadius: 8,
                  padding: 12,
                  overflow: "auto",
                }}
              >
                {renderConversation()}
                {busy && (
                  <div style={{ textAlign: "center", padding: 8 }}>
                    <Spin size="small" />
                  </div>
                )}
              </div>

              {/* 待回答区 */}
              {pending && (
                <div style={{ marginTop: 10 }}>
                  {pending.question && (
                    <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
                      模型提问：{pending.question}
                    </Typography.Text>
                  )}
                  {!!pending.options?.length && (
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                      {pending.options.map((o) => (
                        <Button
                          key={o}
                          size="small"
                          disabled={busy}
                          onClick={() => void answerStream(o)}
                        >
                          {o}
                        </Button>
                      ))}
                    </div>
                  )}
                  <div style={{ display: "flex", gap: 8 }}>
                    <Input
                      placeholder="回答上面的问题，Enter 发送"
                      value={fbText}
                      disabled={busy}
                      onChange={(e) => setFbText(e.target.value)}
                      onPressEnter={() => {
                        const t = fbText.trim();
                        if (!t) return;
                        setFbText("");
                        void answerStream(t);
                      }}
                    />
                    <Button
                      type="primary"
                      disabled={busy || !fbText.trim()}
                      onClick={() => {
                        const t = fbText.trim();
                        setFbText("");
                        void answerStream(t);
                      }}
                    >
                      回答
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* 反馈弹窗（通过计划 / 打回 / 验收共用） */}
      <Modal
        title={fb?.title}
        open={!!fb}
        confirmLoading={busy}
        onOk={async () => {
          const t = fbText.trim();
          if (fb?.required && !t) {
            message.warning("请填写反馈");
            return;
          }
          setBusy(true);
          try {
            await fb?.submit(t);
            setFb(null);
            setFbText("");
          } catch (e: any) {
            message.error(e?.message || "操作失败");
          } finally {
            setBusy(false);
          }
        }}
        onCancel={() => {
          setFb(null);
          setFbText("");
        }}
        okButtonProps={{ disabled: !!fb?.required && !fbText.trim() }}
      >
        <Input.TextArea
          rows={4}
          value={fbText}
          onChange={(e) => setFbText(e.target.value)}
          placeholder={fb?.placeholder}
        />
      </Modal>

      {/* 编辑任务弹窗 */}
      <Modal
        title="编辑任务"
        open={editOpen}
        confirmLoading={busy}
        onOk={saveEdit}
        onCancel={() => setEditOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div>
            <Typography.Text>标题</Typography.Text>
            <Input
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
            />
          </div>
          <div>
            <Typography.Text>描述</Typography.Text>
            <Input.TextArea
              rows={4}
              value={editDesc}
              onChange={(e) => setEditDesc(e.target.value)}
            />
          </div>
        </div>
      </Modal>

      {/* 护栏：同一点反复不登记完成，让用户拍板，而不是白烧 token 静默重跑 */}
      <Modal
        open={!!stalled}
        title={
          stalled ? `第 ${stalled.seq} 步连续 ${stalled.rounds} 轮未登记完成` : ""
        }
        onCancel={() => setStalled(null)}
        footer={[
          <Button key="skip" onClick={() => void resolveStall("skip")}>
            跳过这一步
          </Button>,
          <Button key="done" onClick={() => void resolveStall("done")}>
            标记为已完成
          </Button>,
          <Button
            key="retry"
            type="primary"
            onClick={() => void resolveStall("retry")}
          >
            再试一轮
          </Button>,
        ]}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          连续多轮执行后，agent 始终没有调用 <code>task.mark_item_done</code>{" "}
          登记这一步完成，框架因此把它当作「还没做完」继续重跑—— 既是重复劳动，
          也白烧 token。请选一个出口：
        </Typography.Paragraph>
        <Typography.Paragraph style={{ fontSize: 13, marginBottom: 4 }}>
          {stalled?.content_md}
        </Typography.Paragraph>
        {stalled?.conclusion ? (
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            最近一次结论：{stalled.conclusion}
          </Typography.Paragraph>
        ) : null}
      </Modal>

      {/* 工具统计：按需弹窗。放在弹窗里而不是就地展开，是因为就地展开会挤占
          计划点的展示空间、还得重排标题行布局。 */}
      <Modal
        title={`工具统计 · ${toolStats.length} 个工具`}
        open={statsOpen}
        onCancel={() => setStatsOpen(false)}
        footer={null}
        width={720}
      >
        <ToolStatsTable stats={toolStats} />
      </Modal>

      {/* 可观测：与「会话模式」共用同一对组件，只是 open 状态由本页各自持有 */}
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
    </div>
  );
}
