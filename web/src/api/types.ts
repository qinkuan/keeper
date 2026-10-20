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

/** agent 当前绑定的插件（直接列链接目录得出，附链接是否可用）。 */
export interface AgentPluginBinding {
  name: string;
  description?: string | null;
  version?: string | null;
  manifest?: unknown | null;
  /** 当前恒为 true（列目录得到的都是已勾选的）；保留字段以备将来 */
  enabled?: boolean;
  /** 链接是否有效（库目录还在）；悬空为 false */
  available?: boolean;
}

export interface AgentPluginsResp {
  plugins: AgentPluginBinding[];
}

// ---- 聊天消息（前端本地状态）----
/** 一条产物引用：agent 本轮通过 fs.deliver 交付的「块」（文件只是块的一种）。 */
export interface Artifact {
  id: string;
  /** 文件名（内联块如 mermaid 为占位名 diagram.mmd） */
  name: string;
  /** MIME，用于决定预览 / 下载 / 展示方式 */
  mime: string;
  size?: number | null;
  /**
   * 块类型：file=普通文件（走 ArtifactCard 原逻辑）；
   * chart / mermaid / table / map / audio=非文件模态，交给 BlockView 按 kind 渲染。
   * 旧产物（库里没有 kind 字段）一律按 file 处理，向后兼容。
   */
  kind?: "file" | "chart" | "mermaid" | "table" | "map" | "audio";
  /** chart 块用的引擎（vega-lite / echarts）；其余块忽略 */
  engine?: string;
  /** 内联内容（如 mermaid 源码）；有则不必再取文件 */
  src?: string;
  /** file 块的展示偏好：inline / preview / run / download / folder（没显式给则后端按扩展名补） */
  render?: string;
  /** 语义化引用名：agent 在 fs.deliver 里给块起的名字，FINAL 文字用 [[art:ref]] 引用它实现内联 */
  ref?: string;
}

/**
 * 一条用户上传的附件。
 *
 * `file_id` 是 **LLM 厂商侧**的凭据（DeepSeek 的 Files API 返回），模型靠它
 * 看到文件内容；`cache_path` 是本地缓存的相对路径（`@tmp/` 开头），前端靠它
 * 预览原件。两者服务不同的人，缺一不可：
 *   - 没有 file_id：agent 看不到图，附件白传
 *   - 没有 cache_path：前端打不开，用户没法确认自己传的是哪张
 *
 * `file_id` 是**厂商特定且会过期/失效**的：换模型（换 provider）后旧附件全部
 * 作废，这是已知取舍（换模型就重来）。
 */
export interface Attachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  /** 这个 file_id 属于哪个厂商（deepseek / openai…），决定引用格式 */
  provider: string;
  /** 上传时用的模型：换模型后这条附件就不再可用，展示时要能看出来 */
  model: string;
  file_id: string;
  /** 过期时间（Unix 秒）；null = 永久 */
  expires_at: number | null;
  /** 本地缓存的相对路径（@tmp/ 开头），前端预览用 */
  cache_path: string;
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
  /**
   * 本轮正在跑的 run_id（来自服务端的运行注册表）。
   *
   * 只在**刷新后重建**的「进行中」气泡上有值：本地发起的流用不着它（run_id 就
   * 在闭包里）。它的作用是让「停止生成」在刷新后依然可用——run_id 由前端生成，
   * 刷新后内存全空，不带回来就不知道该往 apiAbortChat 传什么。
   */
  runId?: string | null;
  /**
   * 仅「进行中」气泡有：这是哪条 user 消息发起的（后端 `in_progress_for`）。
   *
   * 重连时要靠它把 user 消息放进流式桶——桶里那条会按 id 覆盖 msgs 里的同名
   * 消息，拿不到正确 id 的话去重会失效，页面上会多出一条空的用户气泡。
   */
  inProgressFor?: string | null;
  /** 模型在等用户回答，该消息下要渲染追问框 */
  waiting?: boolean;
  /** 挂起的 step id，回答时回传 */
  stepId?: string;
  /** 可选项 */
  options?: string[];
  /** 本轮产出的文件（agent 写的 HTML / 图片等），供展示 / 下载 */
  artifacts?: Artifact[];
  /**
   * 本轮**用户上传**的附件（只挂在 user 消息上）。
   *
   * 与 artifacts 是两条相反的链路：artifacts 是 agent 产出的、指向工作区里的
   * 真实文件；attachments 是用户塞进来的输入、指向 LLM 厂商侧的 file_id，
   * 外加一份本地缓存的相对路径（cache_path，`@tmp/` 开头）供前端预览。
   */
  attachments?: Attachment[];
  /** 生成途中被用户停止：内容是已产出的部分 */
  canceled?: boolean;
  /**
   * 输入框 `@` 快捷项：发送时把 `@标签` 原文留在气泡里渲染成高亮 chip。
   * `mentionRaw` 是用户输入原文（含 `@标签`），`mentions` 是每项已解析好的展示内容
   * （label + resolved，resolved 即占位符展开后的真实文本，如绝对路径）。
   * 真正的 `text`（已展开、无 `@标签`）发给模型；这里只用于气泡展示。
   */
  mentionRaw?: string;
  mentions?: { label: string; resolved: string }[];
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
  /** 主 / 子 agent 拆分：本会话（含所有子 agent 子会话）按会话归类的成本/用量明细 */
  session_breakdown?: SessionBreakdownItem[];
}

/** 单个会话（主会话或子 agent 子会话）在本会话树内的成本/用量明细，用于区分主/子 agent */
export interface SessionBreakdownItem {
  session_id: string;
  label: string;
  /** 是否为子 agent（delegatee）：子会话的 parent_id 非 NULL、agent_id 形如 "delegatee:code" */
  is_delegatee: boolean;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  duration_ms: number;
  cost: number | null;
  cost_currency: string | null;
  errors: number;
}

/** 一条消息里被委托出去的**子 agent**（delegatee）的调用汇总（按子会话归类） */
export interface SubagentBreakdownItem {
  session_id: string;
  label: string;
  /** 固定为 true：本结构只装子 agent */
  is_delegatee: boolean;
  /** 轮次：该子 agent 在本轮（该 message）的 LLM 调用数 */
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  duration_ms: number;
  cost: number | null;
  cost_currency: string | null;
  errors: number;
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

/** 单个模型的解析质量（by_model 的一项） */
export interface ReactParseModelStat {
  total: number;
  /** 自编轨迹的次数（检出 + 降级） */
  hallucination: number;
  /** 点破无效、最终降级执行的次数 */
  forced: number;
  /** 幻觉率；「未知」（升级前存量）没有这个字段 */
  rate?: number;
  /**
   * 吐了原生工具调用标记（DSML / invoke name=…）的次数。
   *
   * 与「幻觉」严格分开：幻觉是模型**自称做过**，这个是**格式跑偏**——本接口没注册
   * 原生工具，那些标记根本不会被执行，命中即整轮报废（线上踩过：第 12/30 步直接
   * 结束）。真要根治先查那个模型的 base_url / 端点，不是换 prompt 能解决的。
   */
  format_error?: number;
  /** 格式错误率；「未知」没有这个字段 */
  format_rate?: number;
}

/**
 * 改模型预设后，被重装配的 agent。
 *
 * 模型改动之所以要"重载"：LLM 实例是在 agent build 期按当时的配置创建的，把
 * api_key / base_url / 模型名都固化进了那个对象，不重载就得重启整个服务（而重启
 * 会中断所有正在跑的会话）。所以保存后就地重装配，并把结果如实回报——哪些生效了、
 * 哪个没起来，都不该藏在"保存成功"四个字后面。
 */
export interface ReloadedAgent {
  id: string;
  name: string;
  /** 重装配是否成功 */
  ok: boolean;
  /** 失败原因（成功时为空串） */
  reason: string;
}

/**
 * 解析结构统计：模型有多想并行、又丢了多少动作、有没有在自编工具回执。
 *
 * 判读：
 * - `multi_rate` 明显 > 0 → 并行化有真实收益；
 *   `dropped_total` 大 → 现在就在白丢信息（现状第二个 ACTION 被丢弃）。
 * - `hallucination_rate` > 0 → 模型在伪造工具回执，整轮被作废过（白烧 token）。
 *   看 `by_model` 定位到具体模型：小模型跑飞率明显更高，换模型可能直接根治。
 * - `forced_rate` 高 → 点破对小模型无效，得在解析层直接兜住。
 */
export interface ReactParseStats {
  window_days: number;
  total: number;
  multi: number;
  multi_rate: number;
  dropped_total: number;
  dropped_ratio: number;
  by_kind: Record<string, number>;
  /** 检出自编轨迹的总次数（含 forced） */
  hallucination: number;
  hallucination_rate: number;
  /** 点破无效、最终降级执行的次数 */
  forced: number;
  forced_rate: number;
  /**
   * 格式错误（吐了原生工具调用标记 / DSML）的次数，含最终降级执行的。
   *
   * 命中一次就等于那一轮白跑（工具一步没执行），严重度高于幻觉：幻觉至少还能
   * 靠点破自纠，格式错往往是端点配置问题，模型改不过来。
   */
  format_error: number;
  format_error_rate: number;
  /** 格式错误里点破无效、最终降级执行的次数 */
  format_forced: number;
  format_forced_rate: number;
  /** 按模型拆分，幻觉率降序 */
  by_model: Record<string, ReactParseModelStat>;
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
  /** 该次 dump 所属会话（子 agent 的 dump 落在各自的子会话目录下，据此区分主/子） */
  session_id?: string | null;
  /** 是否为子 agent（delegatee）的 dump */
  is_delegatee?: boolean;
  /** 子 agent 标签，如「子 agent：code」；主 agent 调用无此字段 */
  subagent_label?: string | null;
  /** 子 agent 内部迭代序号（父步 N 下的第 M 次迭代 → 标签 step{N}.{M}）；主 agent 无此字段 */
  sub_step?: number | null;
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

/**
 * 核心 agent：新建会话默认落到谁，以及它的人设覆盖。
 *
 * 四项都可空 = 走回落规则（不指定则用第一个已装载的 agent、persona 沿用画像），
 * 所以「完全没配」是合法状态，不是未完成配置。
 */
export interface CoreAgentSettings {
  name?: string | null;
  id?: string | null;
  /** 直接写的 persona 文本；persona_file 优先于它 */
  persona?: string | null;
  /** 长篇人设放独立文件时用；支持 ~ 开头 */
  persona_file?: string | null;
  /** **实际生效**的 agent（走完回落链后的结果），只读。用户没配时配置是空的，
      只看配置值无法显示"当前在跟谁对话"，所以后端额外给这一份。 */
  resolved?: { id: string | null; name: string } | null;
  /** 可选的核心 agent 清单（已装载的那些），供下拉选择 */
  agents?: { id: string | null; name: string }[];
}

/** 入站认证开关（见 keeper/auth/api.py）。``token`` 仅在「开启时新生成」那一次才回传，
    前端拿到后 adopt（存 sessionStorage）；平时不回传，避免把 token 撒进响应体。 */
export interface AuthSettings {
  enabled: boolean;
  token?: string;
}

/** 本地设置（写回 config.yaml）：按管理的内容分段。

   注意**没有**核心 agent 段：核心 agent 由本地目录构建（agent.yaml / soul.md），
   有自己独立的读写接口（see apiGetCoreAgent），不属于"本机设置"。 */
export interface SettingsResp {
  prompt_dump: PromptDumpSettings;
  a2a: A2ASettings;
  context: ContextSettings;
  /** 入站认证开关（运行时状态，来自 keeper.auth） */
  auth: AuthSettings;
  /** 各对端当前的熔断状态（只读） */
  a2a_breakers: BreakerState[];
  /** 手动重置熔断后返回：重置了几个对端 */
  reset?: number;
  /** 配置文件读取异常时的提示（此时其余字段是默认值） */
  error?: string;
}

/** 设置页整体提交：各段一起，避免改 A 冲掉 B */
export interface SettingsInput {
  prompt_dump: PromptDumpSettings;
  a2a: A2ASettings;
  context: ContextSettings;
  /** 入站认证开关；不传则不动（兼容旧调用） */
  auth?: AuthSettings;
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
  /** 本轮被委托出去的子 agent（delegatee）调用汇总，详情 / dump 里逐项展示 */
  subagents?: SubagentBreakdownItem[];
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

/**
 * 会话种类。后端 chat_sessions.kind 的取值集合（见 keeper/chat/service.py
 * 的 SESSION_KINDS）。
 *
 * 收紧成字面量联合而不是 `string`：四个取值**在列表里互斥**，任何一处漏改
 * （忘了加 browser 的筛选条件）都会表现为「那条会话在哪个列表里都看不见」，
 * 这种 bug 没有任何报错。联合类型让漏改在 tsc 阶段就暴露。
 *
 * browser=手动「对这一页提问」产生的浏览器会话；browser-automation=网页 Agent
 * 自动操作产生的会话，两者都归「浏览器」页，但分两组展示、可分别折叠。
 */
export type SessionKind = "chat" | "task" | "browser" | "browser-automation";

/** 思考强度：low / high / max（仅思考模式开启时有意义） */
export type ReasoningEffort = "low" | "high" | "max";

/** 运行模式：normal=普通 ReAct / simple=快捷一次性（翻译/OCR/总结）/ ptc=写代码解决问题 */
export type RunMode = "normal" | "simple" | "ptc";

/** 会话列表里的一项（对应后端 chat_sessions 表） */
export interface SessionItem {
  id: string;
  title?: string | null;
  initiator_id?: number | null;
  /** 本会话绑定的用户空间（可空 → 回落 agent 默认目录） */
  user_space_id?: string | null;
  status?: string | null;
  /** chat=普通会话（「对话」页）／task=任务绑定会话／browser=浏览器会话（「浏览器」页） */
  kind?: SessionKind | null;
  /** 本会话的思考模式（模型"先想再答"） */
  thinking?: boolean;
  /** 思考强度 */
  reasoning_effort?: ReasoningEffort | string | null;
  /** 本会话的运行模式 */
  mode?: RunMode | string | null;
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
  /** 本会话的思考模式（新会话的待选值由前端管；已有会话从这里回填） */
  thinking: boolean;
  /** 思考强度 */
  reasoning_effort: ReasoningEffort | string;
  /** 本会话的运行模式（新会话待选值由前端管；已有会话从这里回填） */
  mode: RunMode | string;
}

/** GET /sessions 返回 */
export interface SessionsResp {
  sessions: SessionItem[];
}

/**
 * 回收站里的一条会话（软删除，按删除时间倒序）。
 *
 * 与 {@link SessionItem} 分开而不是给它加字段：会话列表里永远不会出现已删除的条目，
 * 两者的字段含义也不同——这里多了删除时间、消息条数与所属 agent 的名字。
 */
export interface TrashSessionItem {
  id: string;
  title?: string | null;
  /** 所属 agent；查不到名字时后端回填成 id（核心 agent 不在 agent 表里） */
  agent_id?: string | null;
  agent_name?: string | null;
  /** 同 {@link SessionItem}：会话种类，决定回收站里显示成「任务/对话/浏览器」 */
  kind?: SessionKind | null;
  /** 消息条数：标题常是「新对话」这种占位，靠它判断是不是要找的那条 */
  message_count: number;
  created_at?: string | null;
  updated_at?: string | null;
  /** 删除时间；极老的存量行可能为空 */
  deleted_at?: string | null;
}

/** GET /manage/sessions/trash 返回 */
export interface TrashResp {
  sessions: TrashSessionItem[];
  /** 回收站总数（用于「显示更多」） */
  total: number;
  limit: number;
  offset: number;
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
  /** 用户本轮上传的附件（只挂 user 消息）：后端原样带回（含 file_id / cache_path），
   *  前端据此在气泡里渲染缩略图。类型与 :ts:Attachment 一致。 */
  attachments?: Attachment[];
  /** 逐步思考过程（think / 工具调用 / 观察），由后端从 react_steps 还原 */
  steps?: AgentStep[];
  /** 可观测：本轮汇总用量（历史消息由 load_messages 按 message_id 聚合补上） */
  usage?: UsageSummary | null;
  /** 可观测：本轮端到端墙钟耗时（毫秒） */
  duration_ms?: number | null;
  /**
   * 该消息是后端合成的「本轮进行中」气泡（id 形如 `inprogress-<userMsgId>`）。
   *
   * 什么时候会有：客户端断开后后端不再杀 agent（见 chat/api._sse_pump），它会
   * 继续跑完；但这一轮还没写 assistant 消息，所以 `load_messages` 拿已落库的
   * steps 合成一条带此标记的虚拟消息，让整页刷新后仍能看到「已执行 N 步」。
   * 跑完的那次刷新它会被真实消息取代。
   */
  streaming?: boolean;
  /** 仅当 streaming 为 true：这是哪条 user 消息发起的（用于对齐） */
  in_progress_for?: string;
  /**
   * 仅当 streaming 为 true：本轮正在跑的 run_id（来自服务端的运行注册表）。
   *
   * 「停止生成」靠 run_id，而 run_id 是**前端生成**的——整页刷新后前端内存全空，
   * 不知道该往 `apiAbortChat` 传什么，就停不掉正在跑的任务。有它就能接上。
   * 为空表示后端没有活跃运行（正常情况：这一轮刚结束，或进程重启过）。
   */
  in_progress_run_id?: string | null;
  /** 输入框 @ 标签展示元数据：用户输入原文（含 @标签）；无 @标签 的消息不存在此字段 */
  mention_raw?: string;
  /** 每条 @标签 的展示内容：label + 已解析文本（如绝对路径）；气泡据此渲染高亮 chip */
  mentions?: { label: string; resolved: string }[];
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
  /**
   * 本次返回的这一段**前面**还有没有更早的消息。
   *
   * 会话可能很长（一条消息一轮，对话上百轮就超过单次 limit），接口只给最近一段，
   * 更早的历史要靠前端点「加载更早的消息」继续往前翻。
   */
  has_more?: boolean;
  /**
   * 本次返回里最小的 seq，拿它当下次请求的 `before_seq` 即可接着往更早翻。
   * 消息为空时为 null。
   */
  oldest_seq?: number | null;
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
  /**
   * 更新预设时，服务端顺手重装配了哪些 agent（见 {@link ReloadedAgent}）。
   *
   * 模型改动必须重载才生效：LLM 实例是在 agent build 期创建的，api_key /
   * base_url / 模型名都固化在那个对象里，不重载就只能重启服务（会中断所有正在
   * 跑的会话）。所以「哪些生效了、哪个没起来」必须如实回报，不能让"保存成功"
   * 这四个字把人糊弄过去。
   */
  reloaded_agents?: ReloadedAgent[];
  /**
   * API Key，**只写不读**。
   *
   * 服务端永远不会把它回传（`GET /manage/models` 只回 `has_api_key`）。这个字段
   * 存在的唯一意义是「提交时把它带上」，编辑页**不应该**拿它回填输入框。
   *
   * 更新时留空 = 保持原值不变（服务端语义），否则用户改个模型名就会把 Key 清空，
   * 而他根本看不见自己清掉了什么。
   */
  api_key?: string | null;
  /** 服务端是否已配置 API Key（只读展示标志） */
  has_api_key?: boolean;
  base_url?: string | null;
  temperature: number;
  timeout: number;
  max_retries: number;
  /** 上下文窗口上限（token）：配了才会计算并告警「⚠ 上下文 X%」 */
  context_limit?: number | null;
}

/** 创建 / 更新模型预设的请求体（id 仅更新时带）
 *
 *  刻意排除 `has_api_key`：那是只读的展示标志，不该被回传。
 */
export type ModelProfileInput = Omit<ModelProfile, "id" | "has_api_key">;

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

// ---- 子 agent（delegatee）配置：每个子 agent 独立模型 + 插件 ----

/** 单个子 agent 的当前配置（GET /manage/delegatees 的数组元素）。 */
export interface DelegateeConfig {
  /** delegatee 名（"code" / "video" / ...），与后端 DELEGATEE_TYPES 对齐 */
  name: string;
  /** 一句话说明（来自类型注册表） */
  description: string;
  /** 是否启用 */
  enabled: boolean;
  /** 绑定的模型预设 id；空 = 用主 agent 同款模型 */
  llm_profile_id: string | null;
  /** 绑定的预设名（展示用，服务端回填；无绑定则为 null） */
  profile_name: string | null;
  /** 插件白名单（按插件目录名匹配工具名前缀）；空 = 不限制 */
  plugins: string[];
  /** 工具名白名单（精确匹配）；空 = 不限制 */
  tools: string[];
}

export interface DelegateeListResp {
  delegatees: DelegateeConfig[];
}

export interface DelegateeUpdate {
  llm_profile_id: string | null;
  plugins: string[];
  tools: string[];
  enabled: boolean;
}

export interface DelegateeUpdateResp {
  ok: boolean;
  name: string;
  llm_profile_id: string | null;
  enabled: boolean;
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

/** GET /manage/agents/{id}/model：本 agent 当前绑定的模型预设
 *
 *  模型配置的**唯一**来源就是这个绑定，没有隐藏兜底。所以这里必须能区分三种状态，
 *  否则界面只能说「未配置」，用户不知道该去哪儿配、是忘了绑还是预设被删了。
 */
export interface AgentModelBinding {
  llm_profile_id: string | null;
  profile_name: string | null;
  /** 是否已成功绑定到一个存在的预设 */
  bound: boolean;
  /** ok 已绑定 / missing 没绑定 / invalid 绑了但预设已被删除 */
  state: "ok" | "missing" | "invalid";
  /** 未就绪时给用户看的一句话说明（可直接展示）；已绑定为 null */
  hint: string | null;
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

// ---- 内置浏览器抓取（/browser）----
// 仅桌面形态可用：内核经 CDP 连桌面壳（Electron）里那个内置浏览器，
// 见 keeper/browser/cdp.py 与 desktop/browser.js。

/** 内置浏览器是否可用。available=false 时 reason 是可执行的原因（不是错误堆栈） */
export interface BrowserStatus {
  available: boolean;
  endpoint: string;
  reason: string;
}

/** 内置浏览器当前打开的页面（keeper 主界面已被排除） */
export interface BrowserPage {
  url: string;
  title: string;
}
export interface BrowserTargetsResp {
  pages: BrowserPage[];
}

/** 抓取到的页面文字：直接可喂 LLM */
export interface BrowserTextResp {
  url: string;
  title: string;
  text: string;
  /** 是否因超过 max_chars 被截断 */
  truncated: boolean;
  full_chars: number;
}

/** 抓取到的页面截图：base64（不带 data: 前缀，前端自己拼） */
export interface BrowserScreenshotResp {
  url: string;
  title: string;
  mime: string;
  image_base64: string;
  bytes: number;
}
 /**
 * 扩展接入所需的全部信息（GET /agent-bridge/info）。
 *
 * `connect_code` 是给用户一次复制粘贴用的：里面打包了 `kernel_url` / `ws_url` /
 * `token` / `extension_id`。分开填四个字段容易漏、也容易把 token 截断，
 * 而 token 错一个字符的表现是"点了没反应"，很难自查。
 */
export interface AgentBridgeInfo {
  kernel_url: string;
  ws_url: string;
  token: string;
  /** 内核是否启用了入站认证。未启用时 token 为空，且内核本身就是裸奔的 */
  auth_enabled: boolean;
  /** 未打包扩展按路径算出的 id；用户装出来的必须与它一致 */
  extension_id: string;
  /** 加载「已解压的扩展程序」时要选的**确切目录** */
  extension_path: string;
  /** 当前在线的扩展连接数（SW 被杀后重连会短暂为 0） */
  connected: number;
  connected_ids: string[];
  connect_code: string;
}
