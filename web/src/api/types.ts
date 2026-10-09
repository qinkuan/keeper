// keeper 服务的响应类型（对应 niubi_platform/keeper/keeper.py 的 FastAPI 端点）

export interface Health {
  ok: boolean;
  agents: { id: string; name: string; ready: boolean }[];
}

/** GET /agents 返回的已装载 agent 概要（landing 页枚举用） */
export interface AgentItem {
  id: string;
  name: string;
  /**
   * 来源：platform=平台同步下来的（改了要回平台改），local=本机创建的。
   * 两类混在同一张表里，没有这个字段就分不清卡片上这个 agent 该去哪改配置。
   */
  origin?: "platform" | "local";
  /** 简介（来自平台配置的本地缓存），概览卡片展示用 */
  description?: string | null;
  ready: boolean;
  mcp_connected: boolean;
  /** 该 agent 自己的 A2A 入站地址（服务端生成），添加为对端时直接填即可 */
  a2a_url?: string | null;
}

// ---- 平台（配置之源）----
// mcp / skill / tool / agent 的定义统一由 platform 管理，keeper 只负责运行。
// 智能体页直接读平台，因此这里只保留"读"所需的字段。
/**
 * 平台侧的 agent 画像。
 *
 * 注意：**不含** llm / workspace_kind / workspace_path / workspace_read_only——
 * 那几项已划归客户端（keeper）本地配置，平台不再下发（见 platform 的
 * ``_drop_agent_local_columns``）。这里要与平台 ``AgentOut`` 保持一致。
 */
export interface PlatformAgent {
  id: string;
  name: string;
  description?: string | null;
  persona: string;
  status: string;
  owner_id?: string | null;
  bindings: { id: string; agent_id: string; kind: string; ref_name: string }[];
  created_at: string;
  updated_at: string;
}

/**
 * 市场里的一条 agent：平台上全部定义 + **当前用户是否已添加**。
 *
 * 只有「已添加」的才会出现在 `/api/me/agents` 里，也就是本地能装载的范围。
 * 因此在平台上"创建了 agent"≠"可以添加载"，这一列专门用来区分二者。
 */
export interface MarketAgent {
  id: string;
  name: string;
  description?: string | null;
  status: string;
  added: boolean;
  env_dependencies: EnvDependency[];
  created_at: string;
}

/** agent 运行所需的环境依赖：解释器种类 + 最低版本（可选最高版本，前端暂不填）。 */
export interface EnvDependency {
  kind: string; // "python" | "node"
  min_version: string; // 最低版本，如 "3.11"
  max_version?: string | null; // 服务端预留的最高版本约束口子
}

export interface SkillsResp {
  skills: string[];
}

/** GET /config（只读、已脱敏） */
export interface LlmInfo {
  provider?: string | null;
  model_name?: string | null;
  temperature?: number | null;
  has_api_key?: boolean;
}

export interface AgentConfig {
  name: string;
  /** 绑定的本地工作区目录（workspace.kind=none 时为 null） */
  workspace?: string | null;
  mcp_connected: boolean;
  mcp_servers: string[];
  skills?: string[];
  llm?: LlmInfo | null;
}

// ---- 聊天消息（前端本地状态）----
/** 一条产物引用：agent 本轮通过 fs.write_file 产出的文件。 */
export interface Artifact {
  id: string;
  /** 文件名 */
  name: string;
  /** MIME，用于决定预览 / 下载 / 展示方式 */
  mime: string;
  size?: number | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** 消息时间（ISO 串）：历史消息由后端给出，本地新消息在前端补当前时间 */
  created_at?: string | null;
  llm?: boolean;
  error?: string;
  /** processor 自主规划的步骤（思考 / 工具调用 / 观察） */
  steps?: AgentStep[];
  /** 可观测：本轮汇总用量（token / 缓存命中 / 成本 / LLM 耗时）。
   *  实时回答由后端直接带上；历史消息在 load_messages 时按 message_id 聚合补上。 */
  usage?: UsageSummary | null;
  /** 可观测：本轮端到端墙钟耗时（毫秒），与 usage.duration_ms（仅 LLM）是两个口径 */
  duration_ms?: number | null;
  /** 流式生成中（前端本地状态，后端不返回）：打字机效果期间隐藏部分操作 */
  streaming?: boolean;
  /** 模型在等用户回答，该消息下要渲染追问框 */
  waiting?: boolean;
  /** 挂起的 step id，回答时回传 */
  stepId?: string;
  /** 可选项 */
  options?: string[];
  /** 本轮产出的文件（agent 写的 HTML / 图片等），供展示 / 下载 */
  artifacts?: Artifact[];
  /** 生成途中被用户停止：内容是已产出的部分 */
  canceled?: boolean;
}

/** processor 自主规划的一步 */
/** 用量汇总（可观测）：由 llm_calls 事实表聚合而来 */
export interface UsageSummary {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 思维链 token：completion_tokens 的子集（仅 reasoning 模型非 0） */
  reasoning_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  /** 缓存写入 token（写比读贵，也是成本项） */
  cache_write_tokens: number;
  /** false = provider 未上报缓存，此时 cached_tokens 的 0 不能当「没命中」看 */
  cached_reported: boolean;
  cache_hit_rate: number;
  /** LLM 耗时之和（不含工具执行与等待） */
  duration_ms: number;
  /** 成本；未配单价时为 null，应展示「—」而不是 0 */
  cost: number | null;
  cost_currency: string | null;
  /** 单次调用的最大输入 token（上下文水位用，累加无意义） */
  max_prompt_tokens?: number;
  context_limit?: number | null;
  context_usage_rate?: number | null;
  /** 水位 ≥80%：ReAct 多轮很容易顶满，逼近时提前示警 */
  context_warning?: boolean;
  /** 失败调用数（超时 / 报错）与错误率 */
  errors?: number;
  error_rate?: number;
}

/** 工具维度统计（一行 = 一个工具） */
/**
 * 能力加载的聚合（技能正文 / 工具定义进入上下文的次数）。
 *
 * 三条来源要一起看：
 * - `preload` 高 → L1 摘要写得不准，系统总在猜；
 * - `model_load` 高 → 摘要够用，模型自己在取（好事）；
 * - `auto_disclose` 高 → 工具折叠太狠或没写组摘要，模型只猜名字。
 */
export interface CapabilityKeyStat {
  key: string;
  kind: string;
  loads: number;
  preload: number;
  model_load: number;
  auto_disclose: number;
  chars: number;
  /** 每次注入的平均字符数（比 token 更直观：它就是实际占掉的 prompt 体积） */
  avg_chars: number;
}

export interface CapabilityStats {
  total: number;
  window_days: number;
  by_key: CapabilityKeyStat[];
  by_source: Record<string, number>;
  /** 抖动：同一轮把同一个能力取两次以上 */
  redundant_loads: number;
  top: number;
}

/**
 * 解析结构统计：模型有多想并行、又丢了多少动作。
 *
 * 判读：`multi_rate` 明显 > 0 → 并行化有真实收益；
 * `dropped_total` 大 → 现在就在白丢信息（现状第二个 ACTION 被丢弃）。
 */
export interface ReactParseStats {
  window_days: number;
  total: number;
  multi: number;
  multi_rate: number;
  dropped_total: number;
  dropped_ratio: number;
  by_kind: Record<string, number>;
}

export interface ToolStat {
  tool: string;
  calls: number;
  errors: number;
  error_rate: number;
  total_duration_ms: number;
  avg_duration_ms: number;
  max_duration_ms: number;
  /** 返回大小：大的要重点看，它的输出会原样进下一轮 prompt */
  avg_output_size: number;
  max_output_size: number;
  /** 被 observation_limit 换引用/截断的次数：高说明返回臃肿 */
  truncated: number;
}

// ---- 效果评估（/evals）----
//
// 评估的价值在于「能变红」：改了 prompt / 换模型 / 加 skill 之后跑一遍，
// 看哪条用例掉了。`fingerprint` 必须在展示时一并看到——否则换了模型导致
// token 变化，会被误读成代码变好或变坏。

/** 一次用例跑出来的客观指标（口径与可观测面板一致） */
export interface EvalCaseMetrics {
  steps: number;
  tool_calls: number;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost: number | null;
  /** 端到端墙钟（含工具执行） */
  duration_ms: number;
  /** 纯 LLM 算力耗时 */
  llm_duration_ms: number;
  /** 是否有向用户追问 */
  asked: boolean;
  /** 是否因预算触顶提前收尾 */
  budget_stopped: boolean;
  capability_loads?: number;
  tool_errors?: number;
  /** 只读用例里为真 = 隔离失效，这条用例直接判失败 */
  workspace_mutated?: boolean;
  changed_files?: string[];
}

/** 单条失败明细：assert 是断言类型，detail 是给人看的说明 */
export interface EvalFailure {
  assert: string;
  detail: string;
}

export interface EvalCaseResult {
  case_id: string;
  title: string;
  tags: string[];
  /** 人读的一句话：这条用例在测什么 */
  summary: string;
  input: string;
  skipped: boolean;
  skip_reason: string;
  passed: boolean;
  failures: EvalFailure[];
  /** 运行级异常（agent 没就绪 / 超时），与「答错了」要分开看 */
  error: string | null;
  metrics: EvalCaseMetrics;
  tool_calls: string[];
  answer_preview: string;
}

export interface EvalSummary {
  total: number;
  skipped: number;
  passed: number;
  failed: number;
  pass_rate: number;
  avg_steps: number;
  avg_tokens: number;
  avg_duration_ms: number;
  total_tokens: number;
  total_cost: number | null;
  /** 跑挂了的条数（不是答错） */
  error_cases: number;
  asked_cases: number;
  budget_stopped_cases: number;
  capability_loads: number;
  tool_calls: number;
}

/** 被测对象指纹：配置变了，指标 delta 就不能归因给代码改动 */
export interface EvalFingerprint {
  agent_id: string;
  model?: string | null;
  provider?: string | null;
  profile_id?: string | null;
  tools: string[];
  skills: string[];
  tool_count: number;
  skill_count: number;
  persona_chars: number;
}

/** 一份报告 */
export interface EvalReport {
  schema: number;
  run_id: string;
  agent_id: string;
  suites: string[];
  /** 本次跑的范围。老报告没这个字段，按全量处理 */
  selection?: EvalSelection;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  fingerprint: EvalFingerprint;
  summary: EvalSummary;
  cases: EvalCaseResult[];
  compare?: EvalCompare;
}

/** 历史列表里的一行（不带逐条明细，省带宽） */
export interface EvalReportBrief {
  run_id: string;
  agent_id: string;
  finished_at: string;
  total: number | null;
  passed: number | null;
  failed: number | null;
  pass_rate: number | null;
  avg_steps: number | null;
  avg_tokens: number | null;
  avg_duration_ms: number | null;
  model?: string | null;
}

export interface EvalDelta {
  from: number;
  to: number;
  delta: number;
}

/** 单条用例相对基线的状态：broken=回归（这条最该看） */
export type EvalCaseStatus = "broken" | "fixed" | "kept" | "new" | "skipped";

export interface EvalCompareCase {
  case_id: string;
  title: string;
  status: EvalCaseStatus;
  failures: EvalFailure[];
  tokens: number | null;
  tokens_delta: EvalDelta | null;
  steps: number | null;
}

export interface EvalCompare {
  has_baseline: boolean;
  baseline_run_id: string | null;
  /** 被测配置的变化项；非空 = 指标 delta 不能直接归因给代码 */
  fingerprint_changed: string[];
  summary: Record<string, EvalDelta | null>;
  cases: EvalCompareCase[];
  removed: string[];
  /** 一句话结论，直接可执行 */
  verdict: string;
}

/** 可选用例集 */
export interface EvalSuite {
  name: string;
  cases: number;
  skipped: number;
  tags: string[];
  error: string | null;
}

/**
 * 本次跑的范围。
 *
 * 必须能区分「全量跑完」和「只跑了 --case 指定的几条」——选集运行的通过率
 * 只针对跑过的那几条，把它当全量数字读会得出错误结论。
 */
export interface EvalSelection {
  /** all = 跑完整个用例集；subset = 只跑了指定的用例 */
  mode: "all" | "subset";
  case_ids: string[];
  suites: string[];
  total: number;
}

export interface EvalRunStatus {
  run_id: string;
  status: "running" | "done" | "failed";
  in_memory: boolean;
  agent_id?: string | null;
  suites?: string[];
  started_at?: string;
  returncode?: number | null;
  has_report?: boolean;
  log_tail?: string;
}

/** 单个 ReAct 步骤的用量 */
export interface StepUsage {
  step_id?: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 思维链 token：completion_tokens 的子集（仅 reasoning 模型非 0） */
  reasoning_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  cached_reported: boolean;
  cache_hit_rate: number;
  /** 仅 LLM 思考耗时 */
  llm_duration_ms: number;
  /** 整步耗时（含工具执行） */
  duration_ms?: number | null;
}

/** 时间线里的一步：耗时分解（LLM / 工具）+ token 分解 */
export interface TimelineStep {
  /** 步骤序号；上下文压缩节点不属于任何步骤，为 null */
  seq: number | null;
  /** "compact" = 上下文压缩（不是 ReAct 步骤，但是一次真实的 LLM 调用） */
  virtual?: string | null;
  step_id: string | null;
  /** 步骤类型：工具步即工具名，无工具时为 think / ask / pause */
  kind: string;
  is_tool: boolean;
  status: string;
  /** 整步耗时（含工具执行） */
  duration_ms: number | null;
  llm_duration_ms: number;
  tool_duration_ms: number;
  tool_calls: number;
  /** 截断后真正进入上下文的长度（影响 token 的是它） */
  output_size: number;
  raw_output_size: number;
  /** 原始返回 > 实际进上下文：超内联上限，被换引用或截断了 */
  truncated: boolean;
  created_at: string | null;
  /** 本步的思考（+ 工具入参）；过长会被截断 */
  input_text?: string | null;
  /** 工具返回的结果（观测，会进下一轮 prompt）；过长会被截断 */
  output_text?: string | null;
  /** 原始字符数（与截断后的文本对照） */
  input_chars?: number;
  output_chars?: number;
  /** 空转定位：本步是「同工具 + 同入参」的第 nth 次调用，首次在第 first_seq 步 */
  dup?: { tool: string; nth: number; first_seq?: number | null } | null;
  /** 追回抖动：本步 read 的块之前已展开过（取回 → 被压 → 又取回） */
  read_repeat?: boolean;
  usage: StepUsage | null;
}

/** 一次 LLM 调用的明细（最慢调用列表用） */
export interface SlowCall {
  id: string;
  created_at: string | null;
  model: string | null;
  kind: string;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  duration_ms: number | null;
  /** 首字延迟 */
  ttft_ms: number | null;
  is_stream: boolean;
  ok: boolean;
  error?: string | null;
  message_id?: string | null;
  step_seq?: number | null;
  session_id?: string | null;
}

/** 按错误信息归并后的一组 */
export interface ErrorGroup {
  key: string;
  count: number;
  last_at: string | null;
}

export interface ErrorBucket {
  total: number;
  failed: number;
  error_rate: number;
  groups: ErrorGroup[];
}

/** 错误聚合：LLM 与工具分别统计 */
export interface ErrorBreakdown {
  llm: ErrorBucket;
  tool: ErrorBucket;
}

/** A2A 出站（本端 → 对端）的超时与熔断 */
export interface A2ASettings {
  /** 单次 HTTP/RPC 超时（秒） */
  request_timeout: number;
  /** 等一个 task 走到终态的总时长（秒）；0 = 不限 */
  task_timeout: number;
  /** 轮询间隔（秒） */
  poll_interval: number;
  /** 超时后主动取消对端任务 */
  cancel_on_timeout: boolean;
  /** 连续失败达到阈值后熔断 */
  breaker_enabled: boolean;
  breaker_threshold: number;
  /** 熔断后的冷却时长（秒） */
  breaker_cooldown: number;
}

/** 某个对端当前的熔断状态（运行时，内存里，不落配置） */
export interface BreakerState {
  key: string;
  name: string;
  /** 连续失败次数 */
  failures: number;
  /** 是否正在熔断（冷却期内直接快速失败） */
  open: boolean;
  /** 冷却剩余秒数 */
  remaining: number;
  last_error?: string | null;
}

/** 上下文治理：外部化 + 自动压缩（见 doc/context-design.md） */
export interface ContextSettings {
  enabled: boolean;
  /** 退出保活窗口后，超过这么多字符才换成引用（0 = 不外部化） */
  externalize_min_chars: number;
  /** 豁免名单：这些工具连退出窗口也不压，逗号分隔，支持 task.* 前缀 */
  never_externalize: string;
  /** 外部化块保留天数，过期自动清理（0 = 不清理） */
  retain_days: number;
  /** 留在上下文里的开头字符数 */
  preview_head: number;
  /** 结尾字符数 */
  preview_tail: number;
  /** 外部化块落盘目录 */
  dir: string;
  compact_enabled: boolean;
  /** 模型窗口（token），按它算水位 */
  model_context_limit: number;
  /**
   * 单次回复的输出上限（token）。0 = 不限制，交给模型 / provider 默认。
   * 默认 16384（不少 provider 自己只给 4096）；撞上上限时输出会被**静默截断**
   * （参数 JSON 半截、工具莫名失败），所以提示词会带着这个数字让模型分段输出。
   */
  max_output_tokens: number;
  /** 输入用到窗口的这个比例就压缩 */
  compact_ratio: number;
  compact_min_steps: number;
  /** 步数触发的水位下限：不到这个比例，步数再多也不压 */
  compact_min_ratio: number;
  /** 两次压缩至少隔几步 */
  compact_min_interval: number;
  /** 最近这几步永远保留原文 */
  keep_recent_steps: number;
  compact_max_chars: number;
  /**
   * 单条输出内联上限：超过它先外部化成「预览 + block_id」，外部化不可用时才硬截断。
   * 键名沿用 observation_limit 是历史原因，语义已改，别再当「截断上限」理解。
   */
  observation_limit: number;
  /** 一轮 ReAct 最多跑多少步 */
  max_steps: number;
  /** read 展开多块的总字符预算 */
  read_budget: number;
  /** recall 返回「本轮外部化块」的上限 */
  max_ctx_recall: number;
  /** token 粗估系数：中文多调小，代码/英文多调大 */
  chars_per_token: number;
  /** 追回（read）回来的内容保活几条 */
  pin_recent_reads: number;
  /**
   * 技能按需加载：每轮开头系统要不要替模型把技能正文取来、怎么挑。
   * off=不预加载（概要常驻，正文等模型自己按名字读）；keyword=关键词匹配（零成本）；
   * llm=一次判读挑名字；auto=技能少就全取，多了先关键词、没命中再判读。
   */
  capability_preload: string;
  /** 「技能少」的判定线：按需技能正文 token 粗估合计低于它就全部取来，连判读都不做 */
  cap_small_limit: number;
  /**
   * 工具清单折叠门槛：同一组（插件 / MCP server）里的工具达到几个就折成一行，
   * 只列工具名、不列参数说明（真正调用时自动把完整定义补进上下文）。1 = 从不折叠。
   */
  tool_group_min: number;
  /** 工具并行（P1-1）：0=关闭（默认）；1=并行只读；2=只读 + mutating:false 的 */
  tool_parallel: number;
  /** 单轮最多并发几个工具，防止把 MCP server / 连接池打爆 */
  tool_parallel_max: number;
}

/** prompt 落盘（调试开关）：把发给大模型的完整 prompt 写到本地文件 */
export interface PromptDumpSettings {
  enabled: boolean;
  /** 落盘目录，如 ~/.keeper/prompt-dump */
  dir: string;
  /** 只记输入 token 超过该值的调用；0 = 全记 */
  min_prompt_tokens: number;
  /** 过期文件保留天数 */
  retain_days: number;
}

/** 一次落盘的 LLM 调用（完整 prompt，调试用） */
export interface DumpCall {
  ts?: string | null;
  /** 落盘的文件名（形如 002-react_step-034512.json） */
  file?: string | null;
  model?: string | null;
  is_stream?: boolean;
  kind?: string | null;
  step_seq?: number | null;
  /** system 提示词全文 */
  system?: string;
  messages?: { role: string; content: string; name?: string }[];
  /** 模型这次的响应（流式下为各分片拼接后的完整正文） */
  response?: string;
  /** 工具入参（增量拼接，无工具调用时为空串） */
  tool_args?: string;
  usage?: {
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    cached_tokens?: number | null;
    cache_write_tokens?: number | null;
    reasoning_tokens?: number | null;
  } | null;
  duration_ms?: number | null;
  ttft_ms?: number | null;
}

/** 某一轮的所有 dump（未开启落盘时 enabled=false） */
export interface MessageDump {
  enabled: boolean;
  dir?: string;
  count?: number;
  files: DumpCall[];
}

/** 预算 / 配额检查（上限为 0 表示未配置、不限制） */
export interface BudgetInfo {
  over: boolean;
  scope?: string | null;
  limit: number;
  used: number;
  rate: number;
  warning: boolean;
}

/** 重复调用里的一组（同工具 + 同参数） */
export interface DuplicateGroup {
  message_id?: string | null;
  tool: string;
  args_hash?: string | null;
  count: number;
  args_size: number;
}

/** 追回（read）统计：同一块被反复展开 = 上下文治理在抖动 */
export interface ReadRepeats {
  calls: number;
  repeats: number;
  repeat_rate: number;
  groups: { message_id: string | null; block_hash: string | null; count: number }[];
}

/** 重复调用 / 空转统计 */
export interface DuplicateCalls {
  total: number;
  duplicate_count: number;
  duplicate_rate: number;
  groups: DuplicateGroup[];
  /** read 单独一档（不计入空转，但要看得见抖动） */
  read?: ReadRepeats;
}

/** 追问率（效果指标：多少比例的对话被 agent 追问打断） */
export interface AskRate {
  rounds: number;
  ask_rounds: number;
  ask_count: number;
  ask_rate: number;
}

// ---- 本地运行时管理（/manage）----

/** 插件声明的运行环境要求 */
export interface PluginEnvRequirement {
  kind: "python" | "node";
  minVersion: string;
  maxVersion: string;
}

/**
 * 插件库里读出来的一个插件。
 *
 * 只读：插件库是一个**目录**，加插件就是往里放文件（手写包、或用
 * plugin-authoring 写完放进去），所以这里没有安装 / 卸载接口。
 */
export interface LibraryPlugin {
  /** 清单里声明的名字 */
  name: string;
  /** 在插件库里的目录名——链接名用它，也是用户在文件夹里看到的东西 */
  dirname: string;
  /** 插件包在磁盘上的绝对路径 */
  path: string;
  version: string;
  description: string;
  /** 能力类型：bin（可执行）/ mcp（MCP server）/ skill（纯知识包） */
  kinds: string[];
  envDependencies: PluginEnvRequirement[];
  sizeBytes: number;
  /** 清单有问题时非空（插件仍会被列出来，好让人看见并去修） */
  error: string;
}

export interface PluginLibraryResp {
  /** 插件库根目录（config.yaml 的 plugins.root 解析后的绝对路径） */
  root: string;
  /** 目录本身不存在 */
  exists: boolean;
  plugins: LibraryPlugin[];
}

/** 本地 agent（不来自平台） */
export interface LocalAgent {
  id: string;
  name: string;
  /** 来源：local=本机创建。列表默认只返回 origin=local 的，所以这里恒为 local */
  origin: "platform" | "local";
  description: string | null;
  status: string;
  /**
   * 本地启用开关（唯一开关，缺省 true）。
   *
   * 和 status 分开是故意的：status 是平台侧的配置，会被同步覆盖；这个开关写
   * 在 override 表里，平台同步不碰，所以「停用」不会在某次装载后悄悄失效。
   */
  enabled: boolean;
  loaded: boolean;
  persona: string;
  workspace_kind: string;
  workspace_path: string | null;
  // 注意：**没有** workspace_read_only。只读是**工作空间**的属性，真源在
  // user_space.read_only；要查某个 agent 能不能写，看它绑定的那个空间。
  /** 勾选的插件（库内目录名） */
  plugins: string[];
  /** 勾了但链接不可用（库里目录被删了）——装起来会少这些工具 */
  missing_plugins: string[];
}

export interface LocalAgentListResp {
  agents: LocalAgent[];
}

export interface CreateLocalAgentInput {
  name: string;
  description?: string | null;
  persona?: string;
  status?: string;
  /** 选中的用户空间 id（不选 = 无工作区）。路径与只读由后端从该记录取。 */
  workspace_space_id?: string | null;
  /** @deprecated 兼容旧调用：新代码用 workspace_space_id */
  workspace_kind?: string;
  /** @deprecated 兼容旧调用：新代码用 workspace_space_id */
  workspace_path?: string | null;
  /** 插件的库内目录名或清单 name 都接受 */
  plugins?: string[];
}

/** 本地设置（写回 config.yaml）：按管理的内容分段 */
export interface SettingsResp {
  prompt_dump: PromptDumpSettings;
  a2a: A2ASettings;
  context: ContextSettings;
  /** 各对端当前的熔断状态（只读） */
  a2a_breakers: BreakerState[];
  /** 手动重置熔断后返回：重置了几个对端 */
  reset?: number;
  /** 配置文件读取异常时的提示（此时其余字段是默认值） */
  error?: string;
}

/** 设置页整体提交：两段一起，避免改 A 冲掉 B */
export interface SettingsInput {
  prompt_dump: PromptDumpSettings;
  a2a: A2ASettings;
  context: ContextSettings;
}

/** 趋势图上的一个点（一个时间桶） */
export interface UsagePoint {
  /** 桶标识：day 为 YYYY-MM-DD，hour 为 YYYY-MM-DD HH:00（UTC） */
  bucket: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  duration_ms: number;
  cost?: number | null;
}

/** 按时间桶聚合的用量趋势 */
export interface UsageTimeseries {
  granularity: string;
  points: UsagePoint[];
  cost_currency?: string | null;
}

/** 一轮的完整时间线（可视化用）：总览 + 逐步明细 */
export interface MessageTimeline {
  message_id: string;
  /** 解析后的「触发该轮的用户消息」id（实时回答传进来的是 assistant 消息） */
  anchor_message_id?: string;
  summary: UsageSummary;
  steps: TimelineStep[];
  /** 本轮的重复调用 / 空转与追回抖动（和 token 曲线放一起看才好排查） */
  duplicates?: DuplicateCalls | null;
}

export interface AgentStep {
  thought?: string;
  tool?: string | null;
  args?: Record<string, any> | null;
  observation?: string | null;
  /** 步骤类型：ask_human（向用户提问）/ human_answer（用户补充）/ think / 工具名。
   *  前端据此选择「提问：」「用户补充：」还是「结果：」，避免提问被当成结果渲染。 */
  kind?: string | null;
  /** 可观测：本步的 token / 缓存命中 / 耗时 */
  usage?: StepUsage | null;
}

/** GET /chat 返回 */
export interface ChatResp {
  /** 会话 id；不带 session_id 请求时为新建的会话 */
  session_id?: string;
  /** 本轮助手消息的 id */
  message_id?: string;
  /** 本轮用户消息的 id */
  user_message_id?: string;
  answer: string;
  llm: boolean;
  /** processor 自主规划的步骤 */
  steps?: AgentStep[];
  /** 本轮是否实际调用了工具（MCP / skill） */
  used_tools?: boolean;
  /** 可观测：本轮汇总用量 */
  usage?: UsageSummary | null;
  /** 可观测：本轮端到端墙钟耗时（毫秒） */
  duration_ms?: number | null;
  error?: string;
  /** 模型在向用户提问，等待回答 */
  waiting_human?: boolean;
  /** 挂起的那条 step，回答时要回传以恢复上下文 */
  step_id?: string;
  /** 提问的可选项 */
  options?: string[];
  /** 本轮产出的文件（agent 写的 HTML / 图片等），实时渲染产物卡片用 */
  artifacts?: Artifact[];
  /** 用户在生成途中点了「停止生成」：已生成的内容保留，本轮提前结束 */
  canceled?: boolean;
}

/** MCP server 下挂的一件工具 */
export interface McpTool {
  /** 原始工具名（不含 server 前缀） */
  name: string;
  description?: string;
}

/** 一件能力（内置工具 / 插件）及其开关状态 */
export interface CapabilityItem {
  name: string;
  description?: string;
  enabled: boolean;
  /** 已装配 / 已关闭 / 只读工作区跳过 / 未绑定 / 依赖缺失 / 已停用 */
  state: string;
  /** 是否写操作（只读工作区下会被跳过） */
  mutating?: boolean;
  /** 仅插件有：该插件下已连上的 MCP 工具清单 */
  tools?: McpTool[];
}

/** GET /agents/{id}/capabilities：内置工具 + 插件两类能力 */
export interface CapabilitiesResp {
  builtin: CapabilityItem[];
  plugin: CapabilityItem[];
}

/** framework 检测到的本机一件运行时（python / node） */
export interface DetectedRuntime {
  /** 解释器绝对路径 */
  path: string;
  /** 解析出的版本，如 3.13.5 */
  version: string;
  /** 运行 --version 拿到的原始版本串 */
  version_string: string;
}

/** 某 agent 的环境需求说明（当前不锁定具体版本，只说明依赖） */
export interface AgentEnvRequirements {
  python: string | null;
  node: string | null;
  note: string;
}

/** GET /framework/agents/{id}/env */
export interface AgentEnvResp {
  agent_id: string;
  requirements: AgentEnvRequirements;
  /** 本机检测到的解释器，供下拉选择 */
  detected: { python: DetectedRuntime[]; node: DetectedRuntime[] };
  /** 当前生效的选择（覆盖 > framework 全局 > 无） */
  selection: { python: string | null; node: string | null };
}

/** PUT /framework/agents/{id}/env 请求体 */
export interface AgentEnvReq {
  /** 解释器绝对路径；空串表示清空（回退到 framework 全局默认） */
  python: string;
  node: string;
}

export interface CapabilityToggle {
  kind: "builtin" | "plugin";
  name: string;
  enabled: boolean;
}

/** 会话列表里的一项（对应后端 chat_sessions 表） */
export interface SessionItem {
  id: string;
  title?: string | null;
  initiator_id?: number | null;
  /** 本会话绑定的用户空间（可空 → 回落 agent 默认目录） */
  user_space_id?: string | null;
  status?: string | null;
  /**
   * 会话种类：chat=普通会话，task=任务绑定会话（D1：一个任务一个 session）。
   * 用于在同一个会话列表里区分两种来源。
   */
  kind?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

// ---- 用户空间：用户自管的命名文件路径模块 ----
export interface UserSpace {
  id: string;
  name: string;
  /** 绝对路径 */
  path: string;
  /** 是否只读（默认 true） */
  read_only: boolean;
  description?: string | null;
}

/** 创建 / 更新用户空间的请求体 */
export interface UserSpaceInput {
  name: string;
  path: string;
  read_only?: boolean;
  description?: string | null;
}

/** GET /user-spaces/browse：浏览服务器本机目录，供选择工作目录 */
export interface DirBrowseEntry {
  name: string;
  path: string;
  /** 字节数；目录为 null */
  size: number | null;
  /** 修改时间，epoch 秒 */
  mtime: number | null;
}
export interface DirBrowseResp {
  /** 当前所在目录的绝对路径 */
  path: string;
  /** 上一级目录（已是根则 null） */
  parent: string | null;
  /** 当前目录下的子目录列表 */
  dirs: DirBrowseEntry[];
  /** 当前目录下的文件列表（仅展示，不可进入） */
  files: DirBrowseEntry[];
  /** 错误信息（如权限不足），正常为 null */
  error: string | null;
}

/** 某会话当前生效的工作空间（供聊天头部展示） */
export interface SessionWorkspace {
  user_space: {
    id: string;
    name: string;
    path: string;
    read_only: boolean;
  } | null;
  /** 本 (会话, agent) 的私有产物目录（自动创建、不落库） */
  agent_space: string;
  /** 实际生效的文件根（用户空间路径，或 agent 默认目录） */
  effective_root: string;
  /** 是否只读 */
  read_only: boolean;
}

/** GET /sessions 返回 */
export interface SessionsResp {
  sessions: SessionItem[];
}

/** 会话里的一条消息（对应后端 session_messages 表） */
export interface SessionMessage {
  id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  created_at?: string | null;
  /** 本轮产出的文件引用 */
  artifacts?: Artifact[];
  /** 逐步思考过程（think / 工具调用 / 观察），由后端从 react_steps 还原 */
  steps?: AgentStep[];
  /** 可观测：本轮汇总用量（历史消息由 load_messages 按 message_id 聚合补上） */
  usage?: UsageSummary | null;
  /** 可观测：本轮端到端墙钟耗时（毫秒） */
  duration_ms?: number | null;
}

/** 会话里挂起等待用户回答的追问 */
export interface PendingAsk {
  step_id: string;
  /** 承载该提问的 assistant 消息 id */
  message_id?: string | null;
  question?: string | null;
  options?: string[] | null;
}

/** 一个 A2A 对端（运行时生效层 agent.peers 的一项） */
export interface PeerItem {
  name: string;
  a2a_url?: string | null;
  headers?: Record<string, any>;
  /** local=本进程内 agent；external=外部 agent */
  source?: "local" | "external" | null;
  /** 是否走系统代理（由来源自动推导：本进程直连、外部走代理） */
  trust_env?: boolean | null;
}

/** GET /agents/{id}/peers 返回 */
export interface PeersResp {
  peers: PeerItem[];
}

/** POST /agents/{id}/peers 的请求体 */
export interface PeerInput {
  name: string;
  a2a_url: string;
  headers?: Record<string, any>;
  /** 可选：不传时后端按 URL 自动判定（本进程 / 外部）并推导代理策略 */
  source?: "local" | "external";
  /** 可选：是否走系统代理。不传按来源推导；外部对端可在界面上开关 */
  trust_env?: boolean;
}

/**
 * GET /agents/{id}/tools/effective：运行时**实际生效**的工具（四类来源合并）。
 * 区别于 tool 表的静态登记，这是当期装配结果（含 MCP 与对端工具）。
 */
export interface EffectiveToolItem {
  name: string | null;
  description: string;
}

export interface EffectiveToolsResp {
  tools: EffectiveToolItem[];
}

/** GET /sessions/{id}/messages 返回 */
export interface SessionMessagesResp {
  session_id: string;
  messages: SessionMessage[];
  /** 非空表示该会话正等用户回答，前端据此恢复追问框 */
  pending?: PendingAsk | null;
  error?: string;
}

// ---- coding 模式：工作空间文件浏览器 / 编辑器 ----
export interface WorkspaceEntry {
  name: string;
  /** 相对工作空间根的路径 */
  path: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
  /** git 状态码（"" / "M" / "A" / "??" / "D" / "R" 等），空表示无改动 */
  git: string;
}
export interface WorkspaceTree {
  root: string;
  read_only: boolean;
  current: string;
  entries: WorkspaceEntry[];
}
export interface WorkspaceFile {
  path: string;
  name: string;
  mime: string;
  read_only: boolean;
  text: string;
  git: { status: string; original: string; modified: string };
}

// ---- 模型管理（本地维护的模型预设；agent 通过绑定引用）----
export interface ModelProfile {
  id: string;
  name: string;
  provider: string;
  model_name: string;
  api_key?: string | null;
  base_url?: string | null;
  temperature: number;
  timeout: number;
  max_retries: number;
  /** 上下文窗口上限（token）：配了才会计算并告警「⚠ 上下文 X%」 */
  context_limit?: number | null;
}

/** 创建 / 更新模型预设的请求体（id 仅更新时带） */
export type ModelProfileInput = Omit<ModelProfile, "id">;

/** 模型单价（每百万 token）。成本按调用发生的时间匹配当时生效的那一段价。 */
export interface ModelPrice {
  id: string;
  effective_from: string;
  /** 空 = 至今有效 */
  effective_to?: string | null;
  /** 一天内的时段（本地时间 00:00 起的分钟数，0–1439）；两者都为 null = 全天通用价 */
  time_from_minute?: number | null;
  time_to_minute?: number | null;
  /** 适用星期：逗号分隔的 ISO 星期号（1=周一 … 7=周日）；空 = 每天 */
  weekdays?: string | null;
  input_price_per_1m: number;
  cached_price_per_1m: number;
  output_price_per_1m: number;
  currency: string;
  note?: string | null;
}

/** 新增一段价格：改价是「新增一段」而非改旧段，历史成本才不会被重算。 */
export interface ModelPriceInput {
  effective_from: string;
  effective_to?: string | null;
  time_from_minute?: number | null;
  time_to_minute?: number | null;
  weekdays?: string | null;
  input_price_per_1m: number;
  cached_price_per_1m: number;
  output_price_per_1m: number;
  currency?: string;
  note?: string | null;
}

/** GET /manage/agents/{id}/model：本 agent 当前绑定的模型预设 */
export interface AgentModelBinding {
  llm_profile_id: string | null;
  profile_name: string | null;
}

// ---------------- 任务系统 ----------------

export type TaskStatus =
  | "draft"
  | "planning"
  | "plan_review"
  | "executing"
  | "waiting_input"
  | "waiting_review"
  | "done"
  | "failed";

export type TaskItemStatus = "pending" | "doing" | "done" | "skipped";

export interface TaskArtifact {
  type?: string;
  name?: string;
  path?: string;
  content?: string;
}

export interface TaskItem {
  id: string;
  task_id: string;
  seq: number;
  plan_version: number;
  content_md: string;
  status: TaskItemStatus;
  rounds: number;
  conclusion?: string | null;
  artifacts?: TaskArtifact[] | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface Task {
  id: string;
  agent_id: string;
  title?: string;
  description_md?: string | null;
  session_id?: string | null;
  status: TaskStatus;
  plan_md?: string | null;
  plan_feedback?: string | null;
  plan_reject_count?: number;
  plan_version?: number;
  replan_count?: number;
  review_reject_count?: number;
  review_feedback?: string | null;
  result_summary?: string | null;
  artifacts?: TaskArtifact[] | null;
  created_at?: string;
  updated_at?: string;
}

export interface TasksResp {
  tasks: Task[];
}
export interface TaskDetailResp {
  task: Task;
  items: TaskItem[];
}
export interface TaskReviewResp extends TaskDetailResp {
  /** 推进结果：done / waiting / paused（被人叫停）/ running / limit_item / ... */
  result?: string;
  /** execute 端点返回本轮助手回答 */
  answer?: string;
  /** review 端点返回本轮助手回答（打回后驱动会话） */
  last_answer?: string;
  waiting_human?: boolean;
  step_id?: string;
  options?: string[];
  question?: string | null;
}
