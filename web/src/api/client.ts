/// <reference types="vite/client" />
import type {

  AgentConfig,

  AgentPluginsResp,

  AgentStep,

  ChatResp,

  Health,

  SessionMessagesResp,

  SessionsResp,

  TrashResp,

  SkillsResp,

  AgentItem,

  PeersResp,

  PeerInput,

  EffectiveToolsResp,

  CapabilitiesResp,

  CapabilityToggle,

  AgentEnvResp,

  AgentEnvReq,

  UserSpace,

  UserSpaceInput,

  SessionWorkspace,

  Artifact,

  DirBrowseResp,

  ModelProfile,

  ModelProfileInput,

  AgentModelBinding,

  WorkspaceTree,

  WorkspaceFile,

  MySpaceFile,

  Task,

  TaskItem,

  TaskItemStatus,

  TasksResp,

  TaskDetailResp,

  TaskReviewResp,

  UsageSummary,

  ToolStat,

  MessageTimeline,

  ModelPrice,

  ModelPriceInput,
  ModelProvider,
  PresetPriceCandidate,
  PresetPricesResp,
  ProviderModelsCreateInput,
  ProviderModelsCreateResult,

  UsageTimeseries,

  SlowCall,

  ErrorBreakdown,

  SettingsResp,

  SettingsInput,


  ReasoningEffort,

  RunMode,

  PromptDumpSettings,

  AskRate,

  DuplicateCalls,

  BudgetInfo,

  MessageDump,

  CapabilityStats,

  ReactParseStats,

  EvalReport,

  EvalReportBrief,

  EvalSuite,

  EvalRunStatus,
  SearchStatus,
  SearchStats,
  SearchModelState,
  SearchIndexState,
  SearchIndexStats,
  SearchSyncConfig,
  SearchConfig,
  SearchQueryResp,
  SearchHit,
  SearchDocument,
  SearchDocumentsResp,

  PluginLibraryResp,

  LocalAgentListResp,

  CreateLocalAgentInput,

  Attachment,

  SessionKind,

  DelegateeListResp,
  DelegateeUpdate,
  DelegateeUpdateResp,

} from "./types";


// dev（vite dev server）下 /api 经代理转发到后端并剥离前缀；

// 生产（dist 被 keeper 进程同源托管）下前端与后端同域，直接请求 /agents、/manage 等。

//
// 这里用精确的 `import.meta.env.DEV`（vite 的 define 会把它替换成常量），
// 而不是 `(import.meta as any).env?.DEV`：后者每次都要做一次运行时属性查找，
// 而且类型退化成 any。
//
// **坑**：vite 判断「是不是生产」看的是 `process.env.NODE_ENV`——**不是** mode。
// 所以环境里若带着 `NODE_ENV=production`，连 `vite dev` 都会注入 DEV=false，
// BASE 变空串，所有 API 都打到前端路由（返回 index.html），页面表现为「服务离线」。
// 本地起 dev server 时用 `NODE_ENV=development npx vite`。
const BASE = import.meta.env.DEV ? "/api" : "";


function qs(params?: Record<string, string | undefined>) {

  if (!params) return "";

  const usp = new URLSearchParams();

  Object.entries(params).forEach(([k, v]) => {

    if (v !== undefined && v !== "") usp.append(k, v);

  });

  const s = usp.toString();

  return s ? `?${s}` : "";

}


async function request<T>(

  method: string,

  path: string,

  params?: Record<string, string | undefined>,

  body?: unknown

): Promise<T> {

  const r = await fetch(`${BASE}${path}${qs(params)}`, {

    method,

    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,

    body: body !== undefined ? JSON.stringify(body) : undefined,

  });

  if (!r.ok && r.status !== 204) {

    // 优先带后端 detail，方便定位（如「当前状态 draft 不能产出计划」）

    let detail = "";

    try {

      const d = await r.json();

      detail = d?.detail || d?.message || "";

    } catch {

      /* 非 JSON 响应则忽略 */

    }

    throw new Error(detail ? `${r.status}：${detail}` : `${r.status} ${r.statusText}`);

  }

  if (r.status === 204) return undefined as T;

  return (await r.json()) as T;

}


export function apiGet<T>(path: string, params?: Record<string, string | undefined>) {

  return request<T>("GET", path, params);

}

export function apiPost<T>(path: string, body?: unknown) {

  return request<T>("POST", path, undefined, body);

}

export function apiPatch<T>(path: string, body?: unknown) {

  return request<T>("PATCH", path, undefined, body);

}

export function apiPut<T>(path: string, body?: unknown) {

  return request<T>("PUT", path, undefined, body);

}

export function apiDelete<T>(path: string) {

  return request<T>("DELETE", path);

}


/** POST + query 参数（请求体为空，全部参数走 query） */

export function apiPostQuery<T>(

  path: string,

  params?: Record<string, string | undefined>

) {

  return request<T>("POST", path, params);

}


// ---- 可观测：用量聚合（/metrics）----

// 数据来自 llm_calls 事实表，按 step / 消息 / 会话 / 任务 / agent 任一维度聚合。

export function apiStepMetrics(stepId: string) {

  return apiGet<UsageSummary>(`/metrics/steps/${stepId}`);

}

export function apiMessageMetrics(messageId: string) {

  return apiGet<UsageSummary>(`/metrics/messages/${messageId}`);

}

/** 一轮的逐步时间线：总览 + 每步耗时/token 分解，用于「查看详情」可视化 */

export function apiMessageTimeline(messageId: string) {

  return apiGet<MessageTimeline>(`/metrics/messages/${messageId}/timeline`);

}

export function apiSessionMetrics(sessionId: string) {

  return apiGet<UsageSummary>(`/metrics/sessions/${sessionId}`);

}

export function apiTaskMetrics(taskId: string) {

  return apiGet<UsageSummary>(`/metrics/tasks/${taskId}`);

}

export function apiToolStats(params?: {

  session_id?: string;

  task_id?: string;

  agent_id?: string;

}) {

  // 注意：后端 Query 用的是下划线命名，别写成驼峰（会静默查不到）

  return apiGet<ToolStat[]>(`/metrics/tools`, params);

}

export function apiAgentMetrics(

  agentId: string,

  params?: { since?: string; until?: string }

) {

  return apiGet<UsageSummary>(

    `/metrics/agents/${agentId}`,

    params as Record<string, string | undefined>

  );

}

/** 读取某一轮落盘的完整 prompt（调试用；未开启时返回 enabled=false） */

export function apiMessageDump(messageId: string) {

  return apiGet<MessageDump>(`/metrics/messages/${messageId}/dump`);

}


/** 预算 / 配额：本会话（或任务）累计 token 是否超上限 */

export function apiBudget(params: { session_id?: string; task_id?: string }) {

  return apiGet<BudgetInfo>(

    "/metrics/budget",

    params as Record<string, string | undefined>

  );

}


/** 重复调用 / 空转：同参数反复调同一工具 */

export function apiDuplicateCalls(params: {

  agent_id?: string;

  session_id?: string;

  task_id?: string;

  since?: string;

  until?: string;

  limit?: number;

}) {

  return apiGet<DuplicateCalls>("/metrics/duplicates", {

    ...params,

    limit: params.limit != null ? String(params.limit) : undefined,

  } as Record<string, string | undefined>);

}


/**

 * 能力加载统计：技能正文 / 工具定义各被取进上下文多少次、来源如何。

 *

 * 诊断价值：preload 占比高说明 L1 摘要写得不准（系统总在猜）；

 * auto_disclose 高说明工具折叠太狠或没写组摘要（模型只猜名字）；

 * redundant_loads 是抖动——同一轮把同一能力取两次以上。

 */

export function apiCapabilityStats(params: {

  agent_id?: string;

  days?: number;

  top?: number;

}) {

  return apiGet<CapabilityStats>("/metrics/capabilities", {

    agent_id: params.agent_id,

    days: params.days != null ? String(params.days) : undefined,

    top: params.top != null ? String(params.top) : undefined,

  } as Record<string, string | undefined>);

}


/**

 * 解析结构统计：模型有多想并行、丢了多少动作。

 *

 * 这是「工具并行化」的收益依据：`multi_rate` 高说明模型确实会连写 ACTION；

 * `dropped_total` 大说明现在就在白丢信息（现状是第二个 ACTION 被丢弃）。

 */

export function apiReactParseStats(params: { agent_id?: string; days?: number }) {

  return apiGet<ReactParseStats>("/metrics/react-parse-stats", {

    agent_id: params.agent_id,

    days: params.days != null ? String(params.days) : undefined,

  } as Record<string, string | undefined>);

}


// ---- 效果评估（/evals）----

//

// 评测本体跑在**子进程**里（服务端不能切库，见 keeper/api/evals.py），所以这里

// 只有「读报告」和「触发一次」两类调用，进度靠轮询日志拿。


/** 可选用例集 */

export function apiEvalSuites() {

  return apiGet<EvalSuite[]>("/evals/suites");

}


/** 历史基线列表（时间倒序） */

export function apiEvalReports() {

  return apiGet<{ total: number; items: EvalReportBrief[] }>("/evals/reports");

}


/**

 * 一份报告的完整详情（含逐条用例 + 对比结论）。

 *

 * `compareWith`：不传 = 自动找**早于本报告**的最近一次完整运行（不能直接读

 * latest——刚跑完那刻 latest 就是它自己）；传 run_id = 跟那份指定报告比

 * （看「离起点还差多远」）；传空串 = 不对比。后端会**按这个参数重算**对比。

 */

export function apiEvalReport(runId: string, compareWith?: string) {

  return apiGet<EvalReport>(`/evals/reports/${runId}`, {

    compare_with: compareWith,

  });

}


/** 报告的 Markdown 原文（一键复制到 PR 描述用） */

export function apiEvalReportMarkdown(runId: string) {

  return apiGet<{ run_id: string; markdown: string }>(

    `/evals/reports/${runId}/markdown`

  );

}


/** 触发一次评测（异步，立刻返回 run_id） */

export function apiEvalRun(params?: {

  agent_id?: string;

  /** 多个用例集用逗号分隔 */

  suite?: string;

  case_timeout?: number;

  /**

   * 对比这份历史报告；不传则对比 latest（= 上一次完整运行）。

   * 两者回答不同问题：latest 看「本次改动的净效果」，固定基线看

   * 「离起点还差多远」——后者才拦得住连续小退化。

   */

  baseline?: string;

}) {

  return apiPostQuery<{ run_id: string; status: string; log: string }>(

    "/evals/run",

    {

      agent_id: params?.agent_id,

      suite: params?.suite,

      case_timeout:

        params?.case_timeout != null

          ? String(params.case_timeout)

          : undefined,

      baseline: params?.baseline,

    } as Record<string, string | undefined>

  );

}


/** 某次运行的状态 + 日志尾部（轮询它显示进度） */

export function apiEvalRunStatus(runId: string, tail?: number) {

  return apiGet<EvalRunStatus>(`/evals/run/${runId}`, {

    tail: tail != null ? String(tail) : undefined,

  } as Record<string, string | undefined>);

}


/** 当前正在跑的评测（进页面先问一次，避免重复触发） */

export function apiEvalRunning() {

  return apiGet<{ items: { run_id: string }[] }>("/evals/running");

}


/** 追问率：多少比例的对话被追问打断（效果指标） */

export function apiAskRate(params: {

  agent_id?: string;

  session_id?: string;

  since?: string;

  until?: string;

}) {

  return apiGet<AskRate>(

    "/metrics/ask-rate",

    params as Record<string, string | undefined>

  );

}


// ---- 设置（本地配置，写回 config.yaml）----

export function apiGetSettings() {

  return apiGet<SettingsResp>("/manage/settings");

}

export function apiUpdateSettings(body: SettingsInput) {

  return apiPut<SettingsResp>("/manage/settings", body);

}

/** 手动改了 settings.yaml 后热重载（设置页保存会自动生效，不必点） */

export function apiReloadSettings() {

  return apiPost<SettingsResp>("/manage/settings/reload");

}

/** 读取调试模式开关（settings.yaml 顶层 debug，默认 false） */
export function apiGetDebug() {
  return apiGet<{ debug: boolean }>("/manage/settings/debug");
}

/** 手动解除熔断：不传 peer 表示全部重置 */
export function apiResetBreaker(peer?: string) {
  return apiPost<SettingsResp>("/manage/settings/a2a/breaker/reset", {
    peer: peer ?? null,
  });

}


/** 最慢的 N 次 LLM 调用：定位「这一轮为什么这么久」 */

export function apiSlowestCalls(params: {

  agent_id?: string;

  session_id?: string;

  task_id?: string;

  since?: string;

  until?: string;

  limit?: number;

}) {

  return apiGet<{ items: SlowCall[] }>("/metrics/slowest", {

    ...params,

    limit: params.limit != null ? String(params.limit) : undefined,

  } as Record<string, string | undefined>);

}

/** 错误聚合：LLM / 工具各自失败率 + 按错误信息归类的 Top N */

export function apiErrorBreakdown(params: {

  agent_id?: string;

  session_id?: string;

  task_id?: string;

  since?: string;

  until?: string;

  limit?: number;

}) {

  return apiGet<ErrorBreakdown>("/metrics/errors", {

    ...params,

    limit: params.limit != null ? String(params.limit) : undefined,

  } as Record<string, string | undefined>);

}

/** 用量趋势：按天/小时聚合（趋势图用） */

export function apiUsageTimeseries(params: {

  agent_id?: string;

  session_id?: string;

  task_id?: string;

  since?: string;

  until?: string;

  granularity?: "day" | "hour";

}) {

  return apiGet<UsageTimeseries>("/metrics/timeseries", {

    ...params,

  } as Record<string, string | undefined>);

}


// ---- 服务健康 / agent 列表（landing 用）----

export function apiHealth() {

  return apiGet<Health>("/health");

}

/** 核心 agent（从本地目录构建，不在数据库里） */

export interface CoreAgentResp {

  /** 目录路径 */

  dir: string;

  /** agent.yaml：名字与说明 */

  name: string;

  description: string;

  /** soul.md 全文 */

  soul: string;

  /** 运行时状态；目录还没建好时为 null */

  resolved: { name: string; model: boolean } | null;

  exists: boolean;

  /** 保存时后端自动重新装配的结果（仅写接口返回）。

      装配失败不影响文件已保存，所以这里要能显示"改了但没当场生效"。 */

  reload?: {

    ok: boolean;

    running: boolean;

    name: string | null;

    reason?: string;

  };

}


/* 注意前缀是 /manage/agents 而不是 /agents：后者是会话侧路由（chat/api.py），

   它有一堆 /{agent_id}/... 的路径，请求 /agents/core 会被当成 id="core" 的

   agent 去查，结果就是 404。核心 agent 的管理接口在管理侧路由上。 */

export function apiGetCoreAgent() {

  return apiGet<CoreAgentResp>("/manage/agents/core");

}


export function apiUpdateCoreAgent(body: { name: string; description: string }) {

  return apiPut<CoreAgentResp>("/manage/agents/core", body);

}


export function apiUpdateCoreAgentSoul(body: { soul: string }) {

  return apiPut<CoreAgentResp>("/manage/agents/core/soul", body);

}


/**

 * 新建一个空会话（点「开始对话」时调用，让新会话立刻出现在列表里）。

 *

 * `kind` 决定这条会话属于哪一类：缺省 chat（「对话」页），任务会话传 "task"。

 * 它**只在建会话的那一刻生效**——之后一律沿用库里的值，所以传错了只能删掉重建来改，

 * 和用户空间绑定是同一个性质。

 */

export function apiNewSession(

  agentId: string,

  userSpaceId?: string,

  kind: SessionKind = "chat",

  thinking?: boolean,

  reasoningEffort?: ReasoningEffort,

  mode?: RunMode

) {

  return apiPost<{ session_id: string; kind: SessionKind }>(

    `/agents/${agentId}/sessions`,

    {

      user_space_id: userSpaceId ?? "",

      thinking: thinking ?? false,

      reasoning_effort: reasoningEffort ?? "low",

      mode: mode ?? "normal",

      kind,

    }

  );

}


export function apiListAgents() {

  /* core_agent_id：新建会话的默认落点。前端「开始对话」直接用它，

     于是"会话"可以是一级入口，不必先让用户挑一个智能体。 */

  return apiGet<{ agents: AgentItem[]; core_agent_id?: string | null }>("/agents");

}

/** 同 apiListAgents，但把**核心 agent** 也列进 agents 列表。

    核心 agent 从本地目录构建（~/.keeper/core-agent/），**不在运行时实例表里**，
    所以默认列不出来——而可观测面板要按 agent_id 查用量 / 解析统计，主脑的记录
    全在它名下，不带上它就选不到主脑（实测症状：可观测面板的 agent 下拉里没有
    核心 agent）。

    单独一个函数而不是给 apiListAgents 加参数：它常被直接当 queryFn 传给
    useQuery，加了可选参数会被 React Query 的 context 顶掉（类型不兼容），
    得把所有调用点都改成 () => apiListAgents() 才划算。 */
export function apiListAgentsWithCore() {

  return apiGet<{ agents: AgentItem[]; core_agent_id?: string | null }>(
    "/agents",
    { include_core: "1" },
  );

}


// ---- 对话（按 agentId 路由到本进程内实例）----

export function apiConfig(agentId: string) {

  return apiGet<AgentConfig>(`/agents/${agentId}/config`);

}

/** 该 agent 已绑定的插件（含悬空链接），用于插件 tab 的绑定态展示。 */
export function apiAgentPlugins(agentId: string) {

  return apiGet<AgentPluginsResp>(`/agents/${agentId}/plugins`);

}

export function apiChat(

  agentId: string,

  q: string,

  sessionId?: string,

  stepId?: string,

  userSpaceId?: string

) {

  return apiGet<ChatResp>(`/agents/${agentId}/chat`, {

    q,

    session_id: sessionId,

    step_id: stepId,

    user_space_id: userSpaceId === "" ? undefined : userSpaceId,

  });

}

/** 停止生成：通知后端中止 runId 对应的那次流式运行 */

export function apiAbortChat(agentId: string, runId: string) {

  return apiPost<{ ok: boolean }>(`/agents/${agentId}/chat/abort`, {

    run_id: runId,

  });

}


/** 生成一次运行标识：前端用它精确中止「这一次」流式请求 */

export function newRunId(): string {

  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

}


/** SSE 帧回调：step（一步）/ delta（一小段回答）/ done（权威结果）/ error / abort */

export interface StreamHandlers {

  onStep?: (step: AgentStep) => void;

  onDelta?: (text: string) => void;

  onDone?: (data: any) => void;

  onError?: (msg: string) => void;

  /** 前端主动断开（停止生成） */

  onAbort?: () => void;

  /**

   * 回放快照（仅重连用）：整页刷新后服务端把「已经发生过的」一次补齐。

   * steps / text 直接覆盖本地状态即可，不需要拼接。

   */

  onCatchup?: (data: {

    run_id: string;

    steps: AgentStep[];

    text: string;

    truncated: boolean;

    dropped_steps: number;

    finished: boolean;

    done?: any;

    error?: string;

  }) => void;

  /**

   * 服务端查不到这个 run（进程重启过 / 缓冲已回收 / 前端没带 run_id）。

   * 不是错误，前端应退回轮询。

   */

  onGone?: (data: any) => void;

  /**

   * 会话 id 已确定（流一开始就来）。

   *

   * 刻意不在 done 帧才给：新会话的第一句要立刻持久化 session_id，否则用户在这一

   * 轮没跑完时刷新，前端连「该去看哪个会话」都不知道。

   */

  onSession?: (data: { session_id: string }) => void;

}


/**

 * 按 SSE 帧解析一个响应流并分派给回调。

 *

 * 会话流式（chat/stream）、任务流式（execute/stream、answer/stream）与**重连**

 * （chat/stream/resume）共用同一套帧协议，所以收帧逻辑只写一处。

 */

async function consumeSSE(

  reader: ReadableStreamDefaultReader<Uint8Array>,

  handlers: StreamHandlers

) {

  const decoder = new TextDecoder();

  let buf = "";

  while (true) {

    let done: boolean;

    let value: Uint8Array | undefined;

    try {

      ({ done, value } = await reader.read());

    } catch {

      // 前端 abort（点「停止生成」）：直接收尾，不算错误

      handlers.onAbort?.();

      return;

    }

    if (done) break;

    buf += decoder.decode(value, { stream: true });


    // SSE 以空行分帧

    let idx: number;

    while ((idx = buf.indexOf("\n\n")) !== -1) {

      const frame = buf.slice(0, idx);

      buf = buf.slice(idx + 2);


      let event = "message";

      const dataLines: string[] = [];

      frame.split("\n").forEach((line) => {

        if (line.startsWith("event:")) event = line.slice(6).trim();

        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());

      });

      if (!dataLines.length) continue; // 心跳等无 data 的帧


      try {

        const data = JSON.parse(dataLines.join("\n"));

        if (event === "step") handlers.onStep?.(data as AgentStep);

        else if (event === "delta") handlers.onDelta?.(data?.text || "");

        else if (event === "done") handlers.onDone?.(data);

        // 重连专属两帧：catchup 补回已发生的，gone 表示服务端已无从回放

        else if (event === "catchup") handlers.onCatchup?.(data);

        else if (event === "gone") handlers.onGone?.(data);

        else if (event === "session") handlers.onSession?.(data);

        else if (event === "error")

          handlers.onError?.(data?.message || "流式执行失败");

      } catch {

        /* 单帧解析失败不中断整条流 */

      }

    }

  }

}


/**

 * 通用 SSE 读取：POST 一个 body，按帧解析并分派给回调。

 *

 * `signal` 用于前端立即断开（停止生成）。

 */

export function apiStream(

  path: string,

  body: unknown,

  handlers: StreamHandlers,

  signal?: AbortSignal

) {

  return (async () => {

    const r = await fetch(`${BASE}${path}`, {

      method: "POST",

      headers: { "Content-Type": "application/json" },

      body: JSON.stringify(body),

      signal,

    });

    if (!r.ok || !r.body) {

      handlers.onError?.(`${r.status} ${r.statusText || "请求失败"}`);

      return;

    }

    await consumeSSE(r.body.getReader(), handlers);

  })();

}


/**

 * 重连一条**已断开的**流式运行（GET /agents/{id}/chat/stream/resume）。

 *

 * 与 apiChatStream 的本质区别：**不重新发起执行**——那会让 agent 跑第二遍。

 * 这里只是订阅服务端 chat/hub.py 为这一轮留下的缓冲：先收到 catchup（刷新前

 * 已经发生的步骤 + 累积正文），再接上还没发生的实时流。

 *

 * 收到 gone 表示服务端查不到这个 run（进程重启过 / 缓冲已回收），此时前端应

 * 退回轮询——那是正确降级，不是错误。

 */

export function apiResumeChatStream(

  agentId: string,

  runId: string,

  handlers: StreamHandlers,

  signal?: AbortSignal

) {

  return (async () => {

    const r = await fetch(

      `${BASE}/agents/${agentId}/chat/stream/resume?run_id=${encodeURIComponent(

        runId

      )}`,

      { signal, headers: { Accept: "text/event-stream" } }

    );

    if (!r.ok || !r.body) {

      handlers.onError?.(`${r.status} ${r.statusText || "请求失败"}`);

      return;

    }

    await consumeSSE(r.body.getReader(), handlers);

  })();

}


/**

 * 流式聊天：SSE（POST /agents/{id}/chat/stream）。

 *

 * 逐步推送 ReAct 步骤与最终回答的逐字增量，`done` 带回权威结果

 * （answer / artifacts / 追问信息 / canceled）。

 */

export function apiChatStream(

  agentId: string,

  q: string,

  opts: {

    sessionId?: string;

    stepId?: string;

    userSpaceId?: string;

    /** 思考模式（仅新建会话时生效；已有会话沿用库里绑定，同 userSpaceId） */

    thinking?: boolean;

    /** 思考强度（仅 thinking 开启时有意义） */

    reasoningEffort?: ReasoningEffort;

    /** 运行模式（仅新建会话时生效；已有会话沿用库里绑定） */

    mode?: RunMode;

    /** 新建会话时写进 kind 的类别；已有会话忽略这个值 */

    kind?: SessionKind;

    runId?: string;

    /**
     * 本轮附件：**已上传完成**的附件列表（由 apiUploadAttachment 返回）。
     *
     * 文件内容不走这里——那是 multipart，和 SSE 的长连接混在一起很别扭。
     * 所以是「先上传拿 file_id、后发送带 file_id」两步，这里只传元信息。
     */
    attachments?: Attachment[];

    /** 输入框 @ 快捷项：展示用元数据，后端原样存、回读时用于气泡渲染（不参与推理）。 */
    mentionRaw?: string;

    /** 每条 @标签 的展示内容：label + 已解析文本（如绝对路径）。 */
    mentions?: { label: string; resolved: string }[];

    signal?: AbortSignal;

  },

  handlers: {

    onStep: (step: AgentStep) => void;

    onDelta: (text: string) => void;

    onDone: (resp: ChatResp) => void;

    onError: (msg: string) => void;

    /** 会话 id 已确定（流的第一帧）：新会话要立刻持久化，别等到 done */

    onSession?: (data: { session_id: string }) => void;

  }

) {

  return apiStream(

    `/agents/${agentId}/chat/stream`,

    {

      q,

      session_id: opts.sessionId || "",

      step_id: opts.stepId || "",

      user_space_id: opts.userSpaceId === "" ? undefined : opts.userSpaceId,

      // 思考：仅新会话传（已有会话前端传 undefined，后端沿用库里绑定）

      thinking: opts.thinking,

      reasoning_effort: opts.reasoningEffort,

      // 运行模式：同理，仅新会话传
      mode: opts.mode,

      kind: opts.kind || "chat",

      run_id: opts.runId || "",

      // 附件：空数组时不发这个键，让后端拿到默认值（与「没传」等价）
      attachments: opts.attachments?.length ? opts.attachments : undefined,

      // @ 快捷项展示元数据（不参与推理，仅气泡渲染用）。mention_raw 空串也发，
      // 后端会归一为 NULL；mentions 为空则省略，等价于没传。
      mention_raw: opts.mentionRaw ?? "",
      mentions: opts.mentions,

    },

    { ...handlers, onDone: (d) => handlers.onDone(d as ChatResp) },

    opts.signal

  );

}


/** 流式推进任务执行：done 帧即权威的 task + result；被停止时 resolve(null) */

export function apiExecuteTaskStream(

  agentId: string,

  taskId: string,

  maxRounds: number,

  runId: string,

  handlers: StreamHandlers,

  signal?: AbortSignal

): Promise<TaskDetailResp | null> {

  return new Promise((resolve, reject) => {

    apiStream(

      `/agents/${agentId}/tasks/${taskId}/execute/stream`,

      { max_rounds: maxRounds, run_id: runId },

      {

        ...handlers,

        onDone: (d) => {

          handlers.onDone?.(d);

          resolve(d as TaskDetailResp);

        },

        onError: (m) => {

          handlers.onError?.(m);

          reject(new Error(m));

        },

        onAbort: () => {

          handlers.onAbort?.();

          resolve(null); // 用户停止：不算失败，交给调用方收尾

        },

      },

      signal

    );

  });

}


/** 流式补充输入（回答 agent 追问）：同上，被停止时 resolve(null) */

export function apiAnswerTaskStream(

  agentId: string,

  taskId: string,

  answer: string,

  runId: string,

  handlers: StreamHandlers,

  signal?: AbortSignal

): Promise<TaskDetailResp | null> {

  return new Promise((resolve, reject) => {

    apiStream(

      `/agents/${agentId}/tasks/${taskId}/answer/stream`,

      { answer, run_id: runId },

      {

        ...handlers,

        onDone: (d) => {

          handlers.onDone?.(d);

          resolve(d as TaskDetailResp);

        },

        onError: (m) => {

          handlers.onError?.(m);

          reject(new Error(m));

        },

        onAbort: () => {

          handlers.onAbort?.();

          resolve(null);

        },

      },

      signal

    );

  });

}


/** 重命名会话。空串 = 恢复占位标题。 */

export function apiRenameSession(agentId: string, sessionId: string, title: string) {

  return apiPatch<{ id: string; title: string }>(

    `/agents/${agentId}/sessions/${sessionId}`,

    { title }

  );

}


/** 删除会话。**软删除**：数据仍在库里，见后端 delete_session 的说明。 */

export function apiDeleteSession(agentId: string, sessionId: string) {

  return apiDelete<{ deleted: string; soft_delete: boolean }>(

    `/agents/${agentId}/sessions/${sessionId}`

  );

}


/**

 * 回收站：已删除的会话，按删除时间倒序（跨 agent）。

 *

 * 不带 agentId —— 回收站是管理视图，删掉的会话可能属于任何一个 agent。

 */

export function apiTrashSessions(limit = 50, offset = 0) {

  return apiGet<TrashResp>("/manage/sessions/trash", {

    limit: String(limit),

    offset: String(offset),

  });

}


/** 把一条已删除的会话恢复回列表（409 = 它本来就不在回收站里）。 */

export function apiRestoreSession(sessionId: string) {

  return apiPost<{ id: string; restored: boolean; agent_id?: string | null }>(

    `/manage/sessions/${sessionId}/restore`

  );

}


/**

 * 会话列表。`kind` 收窄类别：不传就是全部，传了只列那一类。

 *

 * 类别过滤**必须交给后端**（查询参数），不能取回来在前端筛：分页是 limit/offset，

 * 前端筛等于「这一页 50 条里挑出属于那一类的几条」，同类会话一多就会把别的

 * 挤出第一页，翻页还会出现空洞。

 */

export function apiSessions(

  agentId: string,

  initiatorId?: number,

  limit = 50,

  offset = 0,

  kind?: SessionKind

) {

  return apiGet<SessionsResp>(`/agents/${agentId}/sessions`, {

    initiator_id: initiatorId === undefined ? undefined : String(initiatorId),

    limit: String(limit),

    offset: String(offset),

    kind,

  });

}


/**

 * 取某会话的一段消息。

 *

 * `beforeSeq` 传入时只取 seq 小于它的那些——「加载更早的消息」用它往前翻页；

 * 不传就是取最近 `limit` 条。响应的 has_more / oldest_seq 是继续翻页用的游标。

 */

export function apiSessionMessages(

  agentId: string,

  sessionId: string,

  limit = 200,

  beforeSeq?: number

) {

  return apiGet<SessionMessagesResp>(

    `/agents/${agentId}/sessions/${sessionId}/messages`,

    {

      limit: String(limit),

      before_seq: beforeSeq === undefined ? undefined : String(beforeSeq),

    }

  );

}

export function apiSkills(agentId: string) {

  return apiGet<SkillsResp>(`/agents/${agentId}/skills`);

}


// ---- 能力开关（内置 / 外部 / MCP / 技能）；切换后服务端会重建实例 ----

export function apiCapabilities(agentId: string) {

  return apiGet<CapabilitiesResp>(`/agents/${agentId}/capabilities`);

}

export function apiToggleCapability(agentId: string, body: CapabilityToggle) {

  return apiPost<{ kind: string; name: string; enabled: boolean; ok?: boolean }>(

    `/agents/${agentId}/capabilities`,

    body

  );

}


// ---- 运行时管理：A2A 对端 + 生效工具（POST/DELETE 会热刷新工具表）----

export function apiPeers(agentId: string) {

  return apiGet<PeersResp>(`/agents/${agentId}/peers`);

}

export function apiAddPeer(agentId: string, body: PeerInput) {

  return apiPost<{ name: string; a2a_url: string; ok?: boolean }>(

    `/agents/${agentId}/peers`,

    body

  );

}

export function apiDeletePeer(agentId: string, name: string) {

  return apiDelete<{ name: string; removed: boolean }>(

    `/agents/${agentId}/peers/${encodeURIComponent(name)}`

  );

}

/** 就地改对端设置（当前只改「是否走系统代理」） */

export function apiPatchPeer(

  agentId: string,

  name: string,

  trustEnv: boolean

) {

  return apiPatch<{ name: string; trust_env: boolean; ok?: boolean }>(

    `/agents/${agentId}/peers/${encodeURIComponent(name)}`,

    { trust_env: trustEnv }

  );

}

/** 运行时实际生效的工具表（区别于 /tools 的静态登记清单） */

export function apiEffectiveTools(agentId: string) {

  return apiGet<EffectiveToolsResp>(`/agents/${agentId}/tools/effective`);

}


// ---- 本地运行时管理（/manage，只保留热重载）----

/** 启动：把库里的配置装配成运行时实例（之后才能对话）。 */
export function startAgent(id: string) {
  return apiPost<{ ok: boolean; id: string; name: string }>(
    `/manage/agents/${id}/start`
  );
}
/** 停止：释放运行时实例（配置与插件绑定保留，可再启动）。 */

export function unloadAgent(id: string) {

  return apiPost<{ ok: boolean; id: string; unloaded: boolean }>(

    `/manage/agents/${id}/unload`

  );

}


// ---- 插件库（只读）----

/**

 * 读插件库。**只读**：插件库是一个目录，加插件就是往里放文件，所以没有

 * 安装 / 卸载接口。也不缓存——用户随时可能往目录里丢文件，缓存只会让人

 * 以为「我放了但它没出现」。

 */

export function apiListPlugins() {

  return apiGet<PluginLibraryResp>("/manage/plugins");

}

/** 库里有、未 link 给该 agent、且支持动态加载(bin 类)的插件——对话里 `/` 选择器数据源。 */
export function apiAvailablePlugins(agentId: string) {
  return apiGet<{ plugins: PluginLibraryResp["plugins"] }>(
    `/manage/plugins/available?agent_id=${encodeURIComponent(agentId)}`
  );
}

/** 本会话当前激活的插件名列表（输入框上方 chip 用它渲染）。 */
export function apiActivatedPlugins(sessionId: string) {
  return apiGet<{ plugins: string[] }>(
    `/manage/plugins/sessions/${sessionId}/activated`
  );
}

/** 激活一个插件到会话，返回激活后的名单。 */
export function apiActivatePlugin(sessionId: string, name: string) {
  return apiPost<{ plugins: string[] }>(
    `/manage/plugins/sessions/${sessionId}/activated`,
    { name }
  );
}

/** 从会话停用一个插件，返回剩余名单。 */
export function apiDeactivatePlugin(sessionId: string, name: string) {
  return apiDelete<{ plugins: string[] }>(
    `/manage/plugins/sessions/${sessionId}/activated/${encodeURIComponent(name)}`
  );
}


/** 创建本地 agent：落库 + 落插件绑定 + 建插件链接；环境不满足会被后端拒绝（409）。 */

export function apiCreateLocalAgent(body: CreateLocalAgentInput) {

  return apiPost<{ ok: boolean; id: string; name: string; plugins: string[] }>(

    "/manage/agents",

    body

  );

}

/** 整体替换某 agent 的插件勾选（勾上→建链接，取消→删链接）。 */

export function apiSetLocalAgentPlugins(id: string, plugins: string[]) {

  return apiPatch<{

    ok: boolean;

    agent_id: string;

    plugins: string[];

    /** 原来在跑的话是否已重建实例（插件是装配期读的，不重建不生效） */

    restarted: boolean;

  }>(`/manage/agents/${id}/plugins`, { plugins });

}

/** 删除本地 agent：卸实例 + 删行/绑定 + 清资源目录（含插件链接）。 */

export function apiDeleteLocalAgent(id: string) {

  return apiDelete<{ ok: boolean; id: string; removed_dir: boolean }>(

    `/manage/agents/${id}`

  );

}


/**

 * 本地启用 / 停用 —— 决定它跑不跑、进不进概览。

 *

 * 写的是 override 表而不是 status，所以**不会因为别处的状态变更悄悄失效**。

 * 缺省是启用的。

 */

export function apiSetAgentEnabled(id: string, enabled: boolean) {

  return apiPatch<{

    ok: boolean;

    id: string;

    enabled: boolean;

    running: boolean;

  }>(`/manage/agents/${id}/enable`, { enabled });

}


/** 本机所有 agent（库里有的就是本机的）。 */
export function apiListAllAgents() {
  return apiGet<LocalAgentListResp>("/manage/agents");
}


// ---- framework 环境：检测本机 + 按 agent 指定解释器（不做安装）----

/** 读某 agent 的环境配置：需求说明 + 本机检测到的解释器 + 当前选择。 */

export function apiAgentEnv(agentId: string) {

  return apiGet<AgentEnvResp>(`/framework/agents/${agentId}/env`);

}

/** 保存某 agent 的解释器选择（写到 config.yaml 的 agent_overrides）。 */

export function apiUpdateAgentEnv(agentId: string, body: AgentEnvReq) {

  return apiPut<AgentEnvResp>(`/framework/agents/${agentId}/env`, body);

}


// ---- 用户空间（用户自管的命名文件路径）----

export function apiListUserSpaces() {

  return apiGet<{ user_spaces: UserSpace[] }>("/user-spaces");

}

export function apiCreateUserSpace(body: UserSpaceInput) {

  return apiPost<UserSpace>("/user-spaces", body);

}

export function apiUpdateUserSpace(id: string, body: UserSpaceInput) {

  return apiPut<UserSpace>(`/user-spaces/${id}`, body);

}

export function apiDeleteUserSpace(id: string) {

  return apiDelete<{ id: string; removed: boolean }>(`/user-spaces/${id}`);

}

/** 浏览服务器本机目录（只读列目录），path 为空时从用户 home 开始。 */

export function apiBrowseDir(path?: string) {

  return apiGet<DirBrowseResp>("/user-spaces/browse", { path: path ?? "" });

}

/** 「我的空间」：单一用户空间，路径存 settings.yaml。GET 读当前路径（空=未配置）。 */
export function apiGetMySpace() {
  return apiGet<{ path: string | null }>("/user-spaces/mine");
}

/** 「我的空间」：把选中的文件夹绝对路径写入 settings.yaml，立即生效。 */
export function apiSetMySpace(path: string) {
  return apiPut<{ path: string }>("/user-spaces/mine", { path });
}

/** 「我的空间」：读取空间内某文件的受限内容（越界会被后端拒绝）。 */
export function apiMySpaceFile(path: string) {
  return apiGet<MySpaceFile>("/user-spaces/mine/file", { path });
}

/** 「我的空间」：在系统文件管理器里打开（定位到）本空间 / 某文件（等同 PyCharm 的 Reveal）。 */
export function apiOpenMySpace(path: string, allowDir: boolean) {
  return apiPost<{ ok: boolean }>("/user-spaces/mine/open", {
    path,
    allow_dir: allowDir,
  });
}

/** 「我的空间」：把文件写入空间（拷入）。dir 为相对空间根的子目录，缺省为根。 */
export function apiUploadMySpace(file: File, dir?: string) {
  const form = new FormData();
  form.append("file", file);
  if (dir) form.append("dir", dir);
  return fetch(`${BASE}/user-spaces/mine/upload`, {
    method: "POST",
    body: form,
  }).then(async (r) => {
    if (!r.ok) {
      let detail = `上传失败（${r.status}）`;
      try {
        const b = await r.json();
        if (b && typeof b.detail === "string") detail = b.detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return r.json();
  });
}

/** 「我的空间」：下载空间内某文件（拷出），用带鉴权的 fetch 拿 blob 触发浏览器下载。 */
export async function apiDownloadMySpace(path: string) {
  const r = await fetch(
    `${BASE}/user-spaces/mine/download?path=${encodeURIComponent(path)}`
  );
  if (!r.ok) throw new Error(`下载失败（${r.status}）`);
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = path.split("/").pop() || "download";
  a.click();
  URL.revokeObjectURL(url);
}

/** 「我的空间」：文件原始字节 URL，直接塞进 img / video / iframe。 */
export function apiMySpaceFileRawUrl(path: string, dl = 0): string {
  return `${BASE}/user-spaces/mine/file/raw?path=${encodeURIComponent(path)}${dl ? "&dl=1" : ""}`;
}

/** 「我的空间」：重命名文件或目录。 */
export function apiRenameMySpace(path: string, name: string) {
  return apiPost<{ ok: boolean; path: string; name: string }>(
    "/user-spaces/mine/rename",
    { path, name }
  );
}

/** 「我的空间」：在指定父目录下新建子文件夹。 */
export function apiMkdirMySpace(path: string, name: string) {
  return apiPost<{ ok: boolean; path: string; name: string }>(
    "/user-spaces/mine/mkdir",
    { path, name }
  );
}

/** 「我的空间」：把空间内的文件/目录复制到另一个目录（递归，目标同名自动改名）。 */
export function apiCopyInMySpace(src: string, dst: string) {
  return apiPost<{ ok: boolean; path: string; name: string }>(
    "/user-spaces/mine/copy",
    { src, dst }
  );
}

/** 「我的空间」：把本机文件/目录复制到空间内某目录（源不限位置，目标必须位于空间内）。 */
export function apiCopyFromLocal(src: string, dst: string) {
  return apiPost<{ ok: boolean; path: string; name: string }>(
    "/user-spaces/mine/import",
    { src, dst }
  );
}

/** 「我的空间」：删除文件或目录。 */
export function apiDeleteMySpaceFile(path: string) {
  return apiDelete<{ ok: boolean }>(
    `/user-spaces/mine/file?path=${encodeURIComponent(path)}`
  );
}


// ---- 会话工作空间（按会话绑定 + 展示）----

export function apiSessionWorkspace(agentId: string, sessionId: string) {

  return apiGet<SessionWorkspace>(

    `/agents/${agentId}/sessions/${sessionId}/workspace`

  );

}

export function apiUpdateSessionWorkspace(

  agentId: string,

  sessionId: string,

  userSpaceId?: string | null,

  thinking?: { enabled: boolean; reasoning_effort?: ReasoningEffort },

  mode?: RunMode

) {

  return apiPut<{

    session_id: string;

    user_space_id: string | null;

    thinking?: boolean;

    reasoning_effort?: ReasoningEffort;

    mode?: RunMode;

    ok?: boolean;

  }>(

    `/agents/${agentId}/sessions/${sessionId}`,

    {

      // 只带显式给的字段：思考/模式可单独改而不动工作空间（后端用 exclude_unset 区分）

      ...(userSpaceId !== undefined ? { user_space_id: userSpaceId ?? null } : {}),

      ...(thinking

        ? { thinking: thinking.enabled, reasoning_effort: thinking.reasoning_effort }

        : {}),

      ...(mode !== undefined ? { mode } : {}),

    }

  );

}


// ---- 产物（artifact）：agent 本轮产出的文件 ----

/** 产物文件 URL：直接塞进 img / iframe / a 标签（dev 下带 /api 前缀走代理）。 */

export function artifactUrl(

  agentId: string,

  messageId: string,

  artifactId: string,

  dl = false

) {

  return `${BASE}/agents/${agentId}/files/${messageId}/${artifactId}${dl ? "?dl=1" : ""}`;

}


/** 产物相对 git HEAD 的行级改动（新增/删除标注）。 */

export function artifactDiffUrl(

  agentId: string,

  messageId: string,

  artifactId: string,

): string {

  return `${BASE}/agents/${agentId}/files/${messageId}/${artifactId}/diff`;

}

/**
 * 用户上传附件的 URL。
 *
 * 第二个参数是 **sessionId 而不是 messageId**：附件在发出去之前就要能预览
 * （输入框上方那排缩略图），那一刻它还没挂到任何消息上。按 messageId 查会
 * 直接 404——而那正是用户第一眼看到附图的时机。
 *
 * 与 artifactUrl 分开而不是复用那个：产物取的是「agent 产出的文件」（工作区里的
 * 真实文件），附件取的是「用户传进来的原件」（会话私有临时区的缓存）。两者数据
 * 来源与安全边界不同，端点也是两个。
 */
export function attachmentUrl(

  agentId: string,

  sessionId: string,

  attachmentId: string,

  dl = false

) {

  return `${BASE}/agents/${agentId}/files/attachment/${sessionId}/${attachmentId}${dl ? "?dl=1" : ""}`;

}

/**
 * 上传一个附件：落本地缓存 + 传 LLM 厂商拿 file_id。
 *
 * 用 FormData 而不是 JSON：文件是二进制，JSON 装不下（base64 会让体积涨 1/3）。
 * 返回的附件字典随后作为 attachments 字段随消息一起发出——**文件内容不走消息**，
 * 那是「先上传、后发送」两步，好处是 SSE 那条长连接不用背 multipart。
 */
export async function apiUploadAttachment(

  agentId: string,

  sessionId: string,

  file: File

) {

  const form = new FormData();

  form.append("file", file);

  const res = await fetch(

    `${BASE}/agents/${agentId}/sessions/${sessionId}/attachments`,

    {

      method: "POST",

      body: form,

    }

  );

  if (!res.ok) {

    let detail = `上传失败（${res.status}）`;

    try {

      const body = await res.json();

      if (body && typeof body.detail === "string") detail = body.detail;

    } catch {

      /* 响应不是 JSON 就用默认文案 */

    }

    throw new Error(detail);

  }

  const data = await res.json();

  return {

    attachment: data.attachment as {

      id: string;

      name: string;

      mime: string;

      size: number;

      provider: string;

      model: string;

      file_id: string;

      expires_at: number | null;

      cache_path: string;

    },

    /** 附件归属的会话。传 "new" 时由后端当场建一条，前端据此切过去。 */

    session_id: data.session_id as string,

  };

}

/** 本机部署：在文件管理器打开产物所在目录。 */

export function apiRevealArtifact(

  agentId: string,

  messageId: string,

  artifactId: string

) {

  return apiPost<{ ok: boolean; folder: string }>(

    `/agents/${agentId}/files/${messageId}/${artifactId}/reveal`

  );

}

/** 部署形态（本机 / 远程），前端据此决定显示「打开文件夹」还是只「下载」。 */

export function apiDeployInfo(agentId: string) {

  return apiGet<{ local: boolean }>(`/agents/${agentId}/deploy-info`);

}


// ---- coding 模式：工作空间文件浏览 / 编辑 ----

/** 列工作空间目录（限制在有效根内）+ 每文件 git 状态。 */

export function apiWorkspaceTree(

  agentId: string,

  sessionId: string,

  path = ""

) {

  return apiGet<WorkspaceTree>(

    `/agents/${agentId}/sessions/${sessionId}/workspace/tree`,

    { path }

  );

}

/** 读取工作空间内任意文件内容 + git 改动。 */

export function apiWorkspaceFile(

  agentId: string,

  sessionId: string,

  path: string

) {

  return apiGet<WorkspaceFile>(

    `/agents/${agentId}/sessions/${sessionId}/workspace/file`,

    { path }

  );

}

/** 保存（覆盖写）工作空间内文件；只读空间后端会拒绝。 */

export function apiSaveWorkspaceFile(

  agentId: string,

  sessionId: string,

  path: string,

  content: string

) {

  return apiPut<{ ok: boolean; git: { status: string; original: string; modified: string } }>(

    `/agents/${agentId}/sessions/${sessionId}/workspace/file`,

    { path, content }

  );

}

/**

 * 工作台文件的**原始内容**地址（图片 / PDF 内联预览、二进制下载用）。

 *

 * 与 apiWorkspaceFile 的区别：那个返回 JSON（文本已按 errors="replace" 解码，

 * 二进制会损坏），这个是原始字节。展示器类型在前端判定（components/viewers.ts）。

 */

export function workspaceFileRawUrl(

  agentId: string,

  sessionId: string,

  path: string,

  dl = false

) {

  // 必须带 BASE：dev 模式下 vite 只把 /api 反代到 keeper，裸 /agents/... 会被

  // vite 自己的 SPA 回退页接管并以 200 + text/html 返回——iframe 会套出一整个

  // keeper 应用，看起来像「HTML 预览坏了」，且没有任何报错。

  return `${BASE}/agents/${agentId}/sessions/${sessionId}/workspace/file/raw?path=${encodeURIComponent(

    path

  )}${dl ? "&dl=1" : ""}`;

}


/** 删除工作空间内文件或目录；只读空间后端会拒绝。 */

export function apiDeleteWorkspaceFile(

  agentId: string,

  sessionId: string,

  path: string

) {

  return apiDelete<{ ok: boolean }>(

    `/agents/${agentId}/sessions/${sessionId}/workspace/file?path=${encodeURIComponent(

      path

    )}`

  );

}

/** 新建目录（自动建父级）。 */

export function apiCreateWorkspaceFolder(

  agentId: string,

  sessionId: string,

  path: string

) {

  return apiPost<{ ok: boolean }>(

    `/agents/${agentId}/sessions/${sessionId}/workspace/folder`,

    { path }

  );

}

/** 重命名（同目录内改基名），返回新相对路径。 */

export function apiRenameWorkspacePath(

  agentId: string,

  sessionId: string,

  path: string,

  newName: string

) {

  return apiPost<{ ok: boolean; path: string }>(

    `/agents/${agentId}/sessions/${sessionId}/workspace/rename`,

    { path, new_name: newName }

  );

}


// ---- 模型管理（本地维护的模型预设；agent 通过绑定引用）----

export function apiListModels() {

  return apiGet<ModelProfile[]>("/manage/models");

}

export function apiCreateModel(body: ModelProfileInput) {

  return apiPost<ModelProfile>("/manage/models", body);

}

export function apiUpdateModel(id: string, body: ModelProfileInput) {

  return apiPut<ModelProfile>(`/manage/models/${id}`, body);

}

export function apiDeleteModel(id: string) {
  return apiDelete<{
    id: string;
    removed: boolean;
    /** 连带解绑了几个 agent（模型没了，绑定不能留成悬空的） */
    unbound_agents: number;
    reloaded_agents?: { id: string; name: string; ok: boolean; reason?: string }[];
  }>(`/manage/models/${id}`);
}

export function apiTestModel(id: string) {

  return apiPost<{ ok: boolean; reply?: string }>(`/manage/models/${id}/test`, {});

}

/** 价格段列表（按生效时间倒序，第一条即当前最新价） */

export function apiListPrices(modelId: string) {

  return apiGet<ModelPrice[]>(`/manage/models/${modelId}/prices`);

}

/** 新增一段价格：改价 = 新增段而非改旧段，历史成本不会被重算 */

export function apiCreatePrice(modelId: string, body: ModelPriceInput) {

  return apiPost<ModelPrice>(`/manage/models/${modelId}/prices`, body);

}

export function apiDeletePrice(modelId: string, priceId: string) {

  return apiDelete<null>(`/manage/models/${modelId}/prices/${priceId}`);

}

/**
 * 预设包里的服务商及其支持的模型（新建模型向导的两级选择用）。
 *
 * 一次返回全部：选服务商 → 勾它支持的模型（含上下文、价格），不用逐个请求。
 */
export function apiListProviders() {

  return apiGet<ModelProvider[]>("/manage/models/providers");
}

/**
 * 查某个模型的出厂预设价格（按 provider + model_name 匹配）。
 *
 * 「单价」弹窗里的「从预设添加」用它：匹配上了就把预设配好的价格段（可能含
 * 高峰/低峰错峰）列出来，点一下就能加进自己的价段列表。
 */
export function apiListPresetPrices(provider: string, modelName: string) {

  const q = new URLSearchParams({ provider, model_name: modelName });
  return apiGet<PresetPricesResp>(`/manage/models/preset-prices?${q.toString()}`);
}

/**
 * 按服务商批量创建模型预设：同一个 Key 一次填好，勾几个模型就建几个条目。
 *
 * 每个模型建一行 `llm_profiles`（名字默认等于模型名），并把预设里的价格段
 * 一起写进去。
 */
export function apiCreateModelsFromPreset(body: ProviderModelsCreateInput) {

  return apiPost<ProviderModelsCreateResult>("/manage/models/from-preset", body);
}

// ---- 子 agent（delegatee）配置：每个子 agent 独立模型 + 插件 ----

export function apiListDelegatees() {
  return apiGet<DelegateeListResp>("/manage/delegatees");
}

export function apiUpdateDelegatee(name: string, body: DelegateeUpdate) {
  return apiPut<DelegateeUpdateResp>(`/manage/delegatees/${name}`, body);
}

export function apiGetAgentModel(agentId: string) {

  return apiGet<AgentModelBinding>(`/manage/agents/${agentId}/model`);

}


/** 设定 / 清除 agent 的默认工作区（引用 user_space；传 null = 无工作区）。

 *

 *  保存后服务端会重装配该 agent（工作区是 build 期装配的），并回报是否成功。

 */

export function apiSetAgentWorkspace(agentId: string, userSpaceId: string | null) {

  return apiPut<{

    ok: boolean;

    user_space_id: string | null;

    workspace: string;

    reloaded: boolean;

    reason: string;

  }>(`/manage/agents/${agentId}/workspace`, { user_space_id: userSpaceId });

}


export function apiSetAgentModel(agentId: string, llmProfileId: string | null) {

  return apiPost<{

    ok: boolean;

    agent_id: string;

    llm_profile_id: string | null;

    /** 重装配后是否已经在跑（绑完模型应该立刻就绪） */

    running: boolean;

    /** 没跑起来的原因，比如「还没有配置任何模型」 */

    reason?: string;

  }>(`/manage/agents/${agentId}/model`, {

    llm_profile_id: llmProfileId,

  });

}


// ---- 任务系统 ----

export function apiListTasks(agentId: string) {

  return apiGet<TasksResp>(`/agents/${agentId}/tasks`);

}

export function apiCreateTask(agentId: string, title: string, description_md?: string) {

  return apiPost<TaskDetailResp>(`/agents/${agentId}/tasks`, { title, description_md });

}

export function apiGetTask(agentId: string, taskId: string) {

  return apiGet<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}`);

}

export function apiUpdateTask(

  agentId: string,

  taskId: string,

  body: { title?: string; description_md?: string }

) {

  return apiPatch<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}`, body);

}

export function apiStartTask(agentId: string, taskId: string) {

  return apiPost<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}/start`);

}

export function apiPlanGenerate(agentId: string, taskId: string, instruction?: string) {

  return apiPost<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}/plan/generate`, {

    instruction,

  });

}

export function apiPlanApprove(

  agentId: string,

  taskId: string,

  approved: boolean,

  feedback?: string

) {

  return apiPost<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}/plan/approve`, {

    approved,

    feedback,

  });

}

export function apiPlanReplan(agentId: string, taskId: string, instruction?: string) {

  return apiPost<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}/plan/replan`, {

    instruction,

  });

}

export function apiExecuteTask(agentId: string, taskId: string, max_rounds?: number) {

  return apiPost<TaskReviewResp>(`/agents/${agentId}/tasks/${taskId}/execute`, {

    max_rounds,

  });

}

/** 按 step 暂停：置位后 ReAct 跑到下一步之前停下，已完成的步骤都保留 */

export function apiPauseTask(agentId: string, taskId: string, paused = true) {

  return apiPost<TaskReviewResp>(`/agents/${agentId}/tasks/${taskId}/pause`, {

    paused,

  });

}

export function apiAnswerTask(

  agentId: string,

  taskId: string,

  text: string,

  step_id?: string

) {

  return apiPost<TaskReviewResp>(`/agents/${agentId}/tasks/${taskId}/answer`, {

    text,

    step_id,

  });

}

export interface TaskItemInput {

  id?: string;

  content_md: string;

  status?: TaskItemStatus;

  action?: "add" | "update" | "delete";

}

export function apiPatchTaskItems(

  agentId: string,

  taskId: string,

  items: TaskItemInput[]

) {

  return apiPatch<TaskDetailResp>(`/agents/${agentId}/tasks/${taskId}/items`, {

    items,

  });

}

export function apiReviewTask(

  agentId: string,

  taskId: string,

  action: "approve" | "reject",

  result_summary?: string,

  feedback?: string

) {

  return apiPost<TaskReviewResp>(`/agents/${agentId}/tasks/${taskId}/review`, {

    action,

    result_summary,

    feedback,

  });

}




// ---- 检索（/search）----
//
// 检索层从独立项目 k-search 迁入。端点是**同步**的（后端阻塞调用跑在它自己的
// 线程池里），所以前端不用特殊处理，但**建索引是后台任务**——它立即返回，
// 真正的进度要轮询 /search/status 看。

/** 首屏一次性拉全：模型状态 + 索引统计 + 索引进度 + 参数摘要。 */
export function apiSearchStatus() {
  return apiGet<SearchStatus>("/search/status");
}

/** 索引统计。 */
export function apiSearchStats() {
  return apiGet<SearchStats>("/search/stats");
}

/** embedding 模型状态（是否就绪 / 下载中 / 失败）。 */
export function apiSearchModelStatus() {
  return apiGet<SearchModelState>("/search/model/status");
}

/** 后台下载 embedding 模型（几百 MB，不能等它下完）。 */
export function apiSearchModelDownload(source: "auto" | "hf" | "modelscope" = "auto") {
  return apiPost<{ status: string; source: string }>("/search/model/download", {
    source,
  });
}

/**
 * 建索引（**后台跑**）。
 *
 * `roots` 省略 = 索引「我的空间」；`force` = 忽略内容哈希强制重嵌；
 * `paths` = 只索引这几个文件（桌面端文件监听用）。
 *
 * 返回的 `status` 可能是 `already_running`——自动同步正在跑时不必重复触发。
 */
export function apiSearchIndex(body?: { roots?: string[]; force?: boolean; paths?: string[] }) {
  return apiPost<{ status: string }>("/search/index", body ?? {});
}

/**
 * 读索引同步配置（当前生效值）。
 */
export function apiSearchConfig() {
  return apiGet<SearchSyncConfig>("/search/config");
}

/**
 * 保存索引同步配置，**立即生效**（内核会立刻调整后台扫描，不用重启）。
 */
export function apiSearchSaveConfig(body: {
  sync_enabled?: boolean;
  sync_interval?: number;
  sync_full_interval?: number;
}) {
  return apiPost<SearchSyncConfig>("/search/config", body);
}

/**
 * 检索。mode: hybrid（默认）/ vector / keyword。
 *
 * 融合方式与向量权重**不开放给前端**：由服务端 config 定死（见 search/config.py），
 * 这类调参项对普通用户没有意义，却很容易被调坏（分数不再是 0~1 的置信度，
 * 门槛就失效了）。需要调试请直接改服务端配置。
 */
export function apiSearchQuery(body: {
  query: string;
  top_k?: number;
  mode?: "hybrid" | "vector" | "keyword";
  neighbor_span?: number;
  path_prefix?: string;
  title?: string;
}) {
  return apiPost<SearchQueryResp>("/search/query", body);
}

/** 已索引文档列表。 */
export function apiSearchDocuments(limit = 200) {
  return apiGet<SearchDocumentsResp>("/search/documents", { limit: String(limit) });
}

/** 删除某文档及其全部片段（只删索引，不动磁盘上的文件）。 */
export function apiSearchDeleteDocument(docId: number) {
  return apiDelete<{ status: string; doc_id: number }>(`/search/documents/${docId}`);
}

/** 只删该文档的片段，保留文件记录（标记 pending，待重新索引）。 */
export function apiSearchDeleteDocumentChunks(docId: number) {
  return apiDelete<{ status: string; doc_id: number }>(
    `/search/documents/${docId}/chunks`
  );
}

/**
 * 清空索引。
 *
 * `dropDocuments=false` 时只清片段、保留文件清单（之后重新索引即可恢复，
 * 不用重新扫描目录）。
 */
export function apiSearchClearIndex(dropDocuments = true) {
  return apiPost<{ status: string; drop_documents: boolean; chunks: number; keywords: number }>(
    "/search/cache/clear",
    { drop_documents: dropDocuments }
  );
}
