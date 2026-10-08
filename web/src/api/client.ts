import type {
  AgentConfig,
  AgentStep,
  ChatResp,
  Health,
  SessionMessagesResp,
  SessionsResp,
  SkillsResp,
  AgentItem,
  PlatformAgent,
  MarketAgent,
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
  UsageTimeseries,
  SlowCall,
  ErrorBreakdown,
  SettingsResp,
  SettingsInput,
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
  PluginLibraryResp,
  LocalAgentListResp,
  CreateLocalAgentInput,
} from "./types";

// dev（vite dev server）下 /api 经代理转发到后端并剥离前缀；
// 生产（dist 被 keeper 进程同源托管）下前端与后端同域，直接请求 /agents、/manage 等。
const BASE = (import.meta as any).env?.DEV ? "/api" : "";

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
export function apiListAgents() {
  return apiGet<{ agents: AgentItem[] }>("/agents");
}

// ---- 对话（按 agentId 路由到本进程内实例）----
export function apiConfig(agentId: string) {
  return apiGet<AgentConfig>(`/agents/${agentId}/config`);
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
}

/**
 * 通用 SSE 读取：POST 一个 body，按帧解析并分派给回调。
 *
 * 会话流式（chat/stream）与任务流式（execute/stream、answer/stream）共用同一套
 * 帧协议，所以收帧逻辑只写一处。`signal` 用于前端立即断开（停止生成）。
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

    const reader = r.body.getReader();
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
          else if (event === "error")
            handlers.onError?.(data?.message || "流式执行失败");
        } catch {
          /* 单帧解析失败不中断整条流 */
        }
      }
    }
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
    runId?: string;
    signal?: AbortSignal;
  },
  handlers: {
    onStep: (step: AgentStep) => void;
    onDelta: (text: string) => void;
    onDone: (resp: ChatResp) => void;
    onError: (msg: string) => void;
  }
) {
  return apiStream(
    `/agents/${agentId}/chat/stream`,
    {
      q,
      session_id: opts.sessionId || "",
      step_id: opts.stepId || "",
      user_space_id: opts.userSpaceId === "" ? undefined : opts.userSpaceId,
      run_id: opts.runId || "",
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

export function apiSessions(
  agentId: string,
  initiatorId?: number,
  limit = 50
) {
  return apiGet<SessionsResp>(`/agents/${agentId}/sessions`, {
    initiator_id: initiatorId === undefined ? undefined : String(initiatorId),
    limit: String(limit),
  });
}
export function apiSessionMessages(
  agentId: string,
  sessionId: string,
  limit = 200
) {
  return apiGet<SessionMessagesResp>(
    `/agents/${agentId}/sessions/${sessionId}/messages`,
    { limit: String(limit) }
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
/** 装载：从平台拉该 agent 的配置 → 落本地库 → 装配成实例（装载后才能对话）。 */
export function loadAgent(id: string) {
  return apiPost<{ ok: boolean; id: string; name: string }>(
    `/manage/agents/${id}/load`
  );
}
/** 卸载：释放实例（配置缓存保留，可再装载）。 */
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
 * 本地启用 / 停用 —— 决定它跑不跑、进不进概览。平台 agent 和本地 agent 都适用。
 *
 * 写的是 override 表而不是 status，所以**不会被下一次平台同步悄悄撤销**。
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

/** 我已有的全部 agent：本地创建的 + **已装载的**平台 agent。 */
export function apiListAllAgents() {
  return apiGet<LocalAgentListResp>("/manage/agents?origin=all");
}

/**
 * 平台 agent 全量（含已卸载的），市场页用它判断显示「装载」还是「重新装载」。
 *
 * 注意和 apiListAllAgents 的区别：那个是**库存**（已卸载的不算），这个是
 * 「平台那边我添加过的」全量，用来看装载状态。
 */
export function apiListPlatformAgents() {
  return apiGet<LocalAgentListResp>("/manage/agents?origin=platform");
}

// ---- 平台（配置之源）----
// mcp / skill / tool / agent 的定义统一在 platform，keeper 只读不写。
// 平台地址可用 VITE_PLATFORM_URL 覆盖（默认 :9095）。
const PLATFORM_URL =
  (import.meta as any).env?.VITE_PLATFORM_URL ?? "http://localhost:9095";

async function platformGet<T>(path: string): Promise<T> {
  const r = await fetch(`${PLATFORM_URL}${path}`);
  if (!r.ok) throw new Error(`平台请求失败 ${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

async function platformPost<T>(path: string): Promise<T> {
  const r = await fetch(`${PLATFORM_URL}${path}`, { method: "POST" });
  if (!r.ok) throw new Error(`平台请求失败 ${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

/** 平台上的 agent 定义（智能体页只读展示；增删改请到平台）。 */
export function platformListAgents() {
  return platformGet<PlatformAgent[]>("/api/agents");
}

/** 市场：平台全部 agent + 当前用户是否已添加（`added`）。 */
export function platformListMarket() {
  return platformGet<MarketAgent[]>("/api/market/agents");
}

/** 把某个 agent 加进「我已添加」——装载的前提（幂等）。 */
export function platformAddAgent(id: string) {
  return platformPost<{ ok: boolean; agent_id: string }>(
    `/api/market/agents/${id}/add`
  );
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

// ---- 会话工作空间（按会话绑定 + 展示）----
export function apiSessionWorkspace(agentId: string, sessionId: string) {
  return apiGet<SessionWorkspace>(
    `/agents/${agentId}/sessions/${sessionId}/workspace`
  );
}
export function apiUpdateSessionWorkspace(
  agentId: string,
  sessionId: string,
  userSpaceId?: string | null
) {
  return apiPut<{ session_id: string; user_space_id: string | null; ok?: boolean }>(
    `/agents/${agentId}/sessions/${sessionId}`,
    { user_space_id: userSpaceId ?? null }
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
  return `/agents/${agentId}/sessions/${sessionId}/workspace/file/raw?path=${encodeURIComponent(
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
  return apiDelete<{ id: string; removed: boolean }>(`/manage/models/${id}`);
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
export function apiGetAgentModel(agentId: string) {
  return apiGet<AgentModelBinding>(`/manage/agents/${agentId}/model`);
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


