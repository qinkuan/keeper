import { useEffect } from "react";
import {
  Alert,
  Button,
  Card,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Space,
  Select,
  Spin,
  Switch,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  apiGetSettings,
  apiReloadSettings,
  apiResetBreaker,
  apiUpdateSettings,
} from "../api/client";
import type {
  A2ASettings,
  ContextSettings,
  PromptDumpSettings,
  SettingsInput,
} from "../api/types";

/** 表单即整份设置：各段与后端一一对应，保存时整体提交 */
interface SettingsForm {
  a2a: A2ASettings;
  prompt_dump: PromptDumpSettings;
  context: ContextSettings;
}

const DEFAULT_A2A: A2ASettings = {
  request_timeout: 15,
  task_timeout: 120,
  poll_interval: 3,
  cancel_on_timeout: true,
  breaker_enabled: true,
  breaker_threshold: 3,
  breaker_cooldown: 60,
};

const DEFAULT_CONTEXT: ContextSettings = {
  enabled: true,
  externalize_min_chars: 5000,
  never_externalize: "",
  retain_days: 7,
  preview_head: 400,
  preview_tail: 200,
  dir: "~/.keeper/context-store",
  compact_enabled: true,
  model_context_limit: 64000,
  max_output_tokens: 16384,
  compact_ratio: 0.7,
  compact_min_steps: 20,
  compact_min_ratio: 0.7,
  compact_min_interval: 5,
  keep_recent_steps: 6,
  compact_max_chars: 1500,
  observation_limit: 3500,
  max_steps: 30,
  read_budget: 6000,
  max_ctx_recall: 5,
  chars_per_token: 2.5,
  pin_recent_reads: 2,
  capability_preload: "auto",
  cap_small_limit: 6000,
  tool_group_min: 3,
  tool_parallel: 0,
  tool_parallel_max: 5,
};

const DEFAULT_DUMP: PromptDumpSettings = {
  enabled: false,
  dir: "~/.keeper/prompt-dump",
  min_prompt_tokens: 0,
  retain_days: 7,
};

/**
 * 设置：本机可调的配置（写回 settings.yaml，与平台无关）。
 *
 * 与元配置 config.yaml（端口 / 鉴权 / 用哪个 agent）分开：这里改的是「用的时候
 * 调」的用户设置，保存不会动元配置，改完立即生效。
 *
 * 按**管理的内容**分区：
 *   协作（A2A）  —— 调对端时的超时与熔断，决定「对端出问题时我们等多久」
 *   调试（落盘） —— 排查用的完整 prompt 落盘，默认关
 */
export default function SettingsPage() {
  const qc = useQueryClient();
  const [form] = Form.useForm<SettingsForm>();
  const settingsQ = useQuery({
    queryKey: ["settings"],
    queryFn: apiGetSettings,
  });

  useEffect(() => {
    if (settingsQ.data) {
      form.setFieldsValue({
        a2a: settingsQ.data.a2a ?? DEFAULT_A2A,
        prompt_dump: settingsQ.data.prompt_dump ?? DEFAULT_DUMP,
        context: settingsQ.data.context ?? DEFAULT_CONTEXT,
      });
    }
  }, [settingsQ.data, form]);

  const saveM = useMutation({
    mutationFn: (v: SettingsForm) => apiUpdateSettings(v as SettingsInput),
    onSuccess: (d) => {
      message.success("已保存（立即生效，无需重启）");
      form.setFieldsValue({
        a2a: d.a2a,
        prompt_dump: d.prompt_dump,
        context: d.context,
      });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => message.error("保存失败：" + e.message),
  });

  const reloadM = useMutation({
    mutationFn: () => apiReloadSettings(),
    onSuccess: (d) => {
      message.success("已重新加载");
      form.setFieldsValue({
        a2a: d.a2a,
        prompt_dump: d.prompt_dump,
        context: d.context,
      });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => message.error("重新加载失败：" + e.message),
  });

  const resetM = useMutation({
    mutationFn: (peer?: string) => apiResetBreaker(peer),
    onSuccess: (d) => {
      message.success(`已重置 ${d.reset ?? 0} 个对端的熔断状态`);
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: Error) => message.error("重置失败：" + e.message),
  });

  if (settingsQ.isLoading) {
    return (
      <div style={{ textAlign: "center", padding: 48 }}>
        <Spin />
      </div>
    );
  }

  const breakers = settingsQ.data?.a2a_breakers ?? [];
  /** 配置没读出来（后端返回 error）：此时表单里是默认值，必须禁掉保存 */
  const settingsError = settingsQ.data?.error;

  const a2aTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        对端是别人的进程，快慢与可用性都不由我们控制。这里决定「等它多久」和
        「它一直失败时怎么办」——改完立即生效，已在途的调用不受影响。
      </Typography.Paragraph>

      <Form.Item
        label="单次请求超时（秒）"
        name={["a2a", "request_timeout"]}
        extra="SendMessage / GetTask / 拉 AgentCard 都用它；10~30 秒为宜"
      >
        <InputNumber min={1} max={600} step={5} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="等待对端任务完成（秒）"
        name={["a2a", "task_timeout"]}
        extra="从发出问题算起的总时长；超时按下方策略处理。0 = 不限（不推荐，可能挂死整轮）"
      >
        <InputNumber min={0} max={3600} step={30} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="轮询间隔（秒）"
        name={["a2a", "poll_interval"]}
        extra="等终态时查询对端状态的频率；长任务可放宽，短任务可收紧"
      >
        <InputNumber min={0.5} max={60} step={0.5} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="超时后取消对端任务"
        name={["a2a", "cancel_on_timeout"]}
        valuePropName="checked"
        extra="对端已经不回我们了，让它继续跑只是白烧对端的 token"
      >
        <Switch />
      </Form.Item>

      <Divider orientation="left" plain style={{ margin: "4px 0" }}>
        熔断
      </Divider>
      <Form.Item
        label="开启熔断"
        name={["a2a", "breaker_enabled"]}
        valuePropName="checked"
        extra="连续失败（连不上 / 超时）达到阈值后，短期内直接快速失败，不再每轮都等满超时"
      >
        <Switch />
      </Form.Item>
      <Form.Item
        label="连续失败阈值（次）"
        name={["a2a", "breaker_threshold"]}
        extra="对端「业务上失败」（返回 FAILED）不计入——那是它给的正常答案"
      >
        <InputNumber min={1} max={100} step={1} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="冷却时长（秒）"
        name={["a2a", "breaker_cooldown"]}
        extra="熔断后等多久再放行一次探测请求；成功即恢复，失败重新计时"
      >
        <InputNumber min={5} max={3600} step={30} style={{ width: "100%" }} />
      </Form.Item>

      <Card size="small" title="对端熔断状态（运行时）">
        {breakers.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无失败记录，所有对端都正常"
          />
        ) : (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {breakers.map((b) => (
              <div
                key={b.key}
                style={{ display: "flex", alignItems: "center", gap: 8 }}
              >
                <Tag color={b.open ? "red" : b.failures ? "orange" : "green"}>
                  {b.open ? "已熔断" : b.failures ? `失败 ${b.failures} 次` : "正常"}
                </Tag>
                <span style={{ flex: 1, fontSize: 12 }}>{b.name}</span>
                {b.open ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    剩余 {Math.ceil(b.remaining)}s
                  </Typography.Text>
                ) : null}
                {b.last_error ? (
                  <Typography.Text
                    type="secondary"
                    style={{ fontSize: 12, maxWidth: 260 }}
                    ellipsis={{ tooltip: b.last_error }}
                  >
                    {b.last_error}
                  </Typography.Text>
                ) : null}
              </div>
            ))}
            <Button
              size="small"
              loading={resetM.isPending}
              onClick={() => resetM.mutate(undefined)}
            >
              全部重置
            </Button>
          </Space>
        )}
      </Card>
    </Space>
  );

  const contextTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        ReAct 每步都把思考与工具结果累加进上下文，长任务下会线性膨胀。这里的策略是
        「先不把大东西放进上下文、实在膨胀了再压早期步骤」——两种手段都会把原文存成
        块，模型可用{" "}
        <code>read(block_id=&quot;ctx:...&quot;)</code> 追回，信息不丢。
      </Typography.Paragraph>

      <Form.Item
        label="开启上下文治理"
        name={["context", "enabled"]}
        valuePropName="checked"
        extra="关闭后行为与旧版一致：工具输出只做硬截断，不做外部化与压缩"
      >
        <Switch />
      </Form.Item>

      <Divider orientation="left" plain style={{ margin: "4px 0" }}>
        外部化（工具输出）
      </Divider>
      <Form.Item
        label="外部化阈值（字符）"
        name={["context", "externalize_min_chars"]}
        extra={
          "唯一触发条件：这一步已退出「保留最近步数」窗口 且 超过这个长度，才换成引用。" +
          "刚返回的结果永远完整可见——模型下一步要用它，换预览会漏文件、漏条目。0 = 不外部化"
        }
      >
        <InputNumber min={0} max={100000} step={500} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="豁免名单（永不外部化的工具）"
        name={["context", "never_externalize"]}
        extra="默认空 = 所有工具一视同仁。想让某类产物始终留在上下文里就填它，逗号分隔，支持 task.* 前缀"
      >
        <Input placeholder="留空 = 不豁免" />
      </Form.Item>
      <Divider orientation="left" plain style={{ margin: "4px 0" }}>
        单条输出与步数（决定一轮能涨多大）
      </Divider>
      <Form.Item
        label="单条工具输出截断（字符）"
        name={["context", "observation_limit"]}
        extra="唯一的即时保护：任何一次工具返回都不会超过它。调小更省 token，但更容易看不全"
      >
        <InputNumber min={200} max={200000} step={500} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="每轮最大步数"
        name={["context", "max_steps"]}
        extra="ReAct 一轮最多跑多少步，超过就强制总结"
      >
        <InputNumber min={1} max={200} step={5} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="token 粗估系数（字符/token）"
        name={["context", "chars_per_token"]}
        extra="只用于算压缩水位，不参与记账。中文多调小（1.5~2），代码/英文多调大（3~4）"
      >
        <InputNumber min={1} max={10} step={0.5} style={{ width: "100%" }} />
      </Form.Item>

      <Divider orientation="left" plain style={{ margin: "4px 0" }}>
        取回（read / recall）
      </Divider>
      <Form.Item
        label="read 展开预算（字符）"
        name={["context", "read_budget"]}
        extra="一次 read 多块时的总字符上限，防止取回时又把上下文撑爆"
      >
        <InputNumber min={500} max={200000} step={1000} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="recall 返回上下文块上限"
        name={["context", "max_ctx_recall"]}
        extra="recall 额外返回「本轮外部化块」的条数；0 = 不返回"
      >
        <InputNumber min={0} max={50} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="追回内容保活条数"
        name={["context", "pin_recent_reads"]}
        extra="read 回来的内容保持原文的条数。0 = 不保（走出窗口就会被再外部化，可能出现「读了又被压又读」的抖动）；调大更稳但上下文更容易涨"
      >
        <InputNumber min={0} max={50} style={{ width: "100%" }} />
      </Form.Item>

      <Divider orientation="left" plain style={{ margin: "12px 0" }}>
        工具 / 技能按需加载
      </Divider>
      <Form.Item
        label="工具组折叠门槛"
        name={["context", "tool_group_min"]}
        extra="同一个插件 / MCP server 下的工具达到几个就折成一行：只列工具名、不列参数说明，真正调用时系统会自动把完整定义补进上下文（模型不会因为折叠而不会用）。1 = 从不折叠，等同以前的全量清单"
      >
        <InputNumber min={1} max={100} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="工具并行执行"
        name={["context", "tool_parallel"]}
        extra="同一轮里模型连写了多个只读工具时并发执行以降低时延。0=关闭（默认，行为与从前完全一致）；1=只并行只读工具（读文件 / 查询类）；2=再加上插件清单里显式标了 mutating: false 的工具。写类工具与问用户永远串行"
      >
        <Select
          options={[
            { value: 0, label: "0 · 关闭（默认）" },
            { value: 1, label: "1 · 只并行只读工具" },
            { value: 2, label: "2 · 只读 + 显式声明无副作用的工具" },
          ]}
        />
      </Form.Item>
      <Form.Item
        label="单轮最大并发数"
        name={["context", "tool_parallel_max"]}
        extra="一次最多并发几个工具。防止模型一次写出几十个调用把 MCP server 或数据库连接池打爆"
      >
        <InputNumber min={1} max={20} style={{ width: "100%" }} />
      </Form.Item>

      <Form.Item
        label="技能正文预加载策略"
        name={["context", "capability_preload"]}
        extra="每轮开头系统要不要替模型把技能正文取来：off=不预加载，正文只等模型自己按名字读（最纯粹的「概要常驻 + 按需加载」）；keyword=拿技能 keywords 跟问题做包含匹配，命中才取，零成本；llm=让模型看一眼清单挑名字，最准但多一次调用；auto=技能少就全取，多了先 keyword、没命中再判读"
      >
        <Select
          options={[
            { value: "auto", label: "auto（推荐：技能少全取，多了先关键词再判读）" },
            { value: "off", label: "off（不预加载，纯靠模型自己读）" },
            { value: "keyword", label: "keyword（关键词匹配，零额外调用）" },
            { value: "llm", label: "llm（每次判读，最准）" },
          ]}
        />
      </Form.Item>
      <Form.Item
        label="「技能少」判定线（token）"
        name={["context", "cap_small_limit"]}
        extra="auto 模式下：按需技能的正文 token 粗估合计低于这个数就全部取来、连判读都不做——这时省下的 token 抵不过多一次调用。0 = 永不走全量分支"
      >
        <InputNumber min={0} max={200000} step={500} style={{ width: "100%" }} />
      </Form.Item>

      <Form.Item
        label="外部化块保留天数"
        name={["context", "retain_days"]}
        extra="过期块自动清理（最多每小时扫一次）。外部化会持续产块，且「先外部化、后又被压缩打包」会留下不再被引用的旧块，所以需要回收；0 = 不清理"
      >
        <InputNumber min={0} max={365} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="预览：开头字符数"
        name={["context", "preview_head"]}
        extra="留在上下文里的开头部分"
      >
        <InputNumber min={0} max={5000} step={100} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="预览：结尾字符数"
        name={["context", "preview_tail"]}
        extra="结论、报错常在末尾，值得留一小段"
      >
        <InputNumber min={0} max={5000} step={100} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item label="外部块目录" name={["context", "dir"]}>
        <Input placeholder="~/.keeper/context-store" />
      </Form.Item>

      <Divider orientation="left" plain style={{ margin: "4px 0" }}>
        自动压缩（早期步骤）
      </Divider>
      <Form.Item
        label="开启自动压缩"
        name={["context", "compact_enabled"]}
        valuePropName="checked"
        extra="到水位时把最早的一批步骤压成「骨架 + block_id」，头部与最近几步不动（保住前缀缓存）"
      >
        <Switch />
      </Form.Item>
      <Form.Item
        label="模型窗口（token）"
        name={["context", "model_context_limit"]}
        extra="按所用模型的实际窗口填，用于计算水位"
      >
        <InputNumber min={1000} max={2000000} step={8000} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="单次输出上限（token）"
        name={["context", "max_output_tokens"]}
        extra="一次回复最多能输出多少 token。0 = 不限制（交给模型默认）。设了之后系统提示词会把这个数字告诉模型，让它把大文件、长脚本分段输出——因为撞上上限时输出是被静默截断的，模型自己察觉不到，只会表现为参数 JSON 不完整、工具莫名失败"
      >
        <InputNumber min={0} max={200000} step={1000} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="压缩水位（占窗口比例）"
        name={["context", "compact_ratio"]}
        extra="输入估算达到窗口的这个比例就压缩一次；留足空间给输出与后续增长"
      >
        <InputNumber min={0.1} max={0.95} step={0.05} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="步数阈值"
        name={["context", "compact_min_steps"]}
        extra={
          "步数达到这个值**且**已过下方水位下限才触发。只看步数会压得太早：实测 20 步时输入才占窗口 22%，" +
          "此时省下的多是缓存命中的廉价 token，却让后续一批变未命中价，每步成本反而涨"
        }
      >
        <InputNumber min={2} max={500} step={5} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="步数触发的水位下限"
        name={["context", "compact_min_ratio"]}
        extra="步数触发的**必要条件**：输入至少占到窗口的这个比例，否则步数再多也不压"
      >
        <InputNumber min={0.05} max={0.95} step={0.05} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="最小压缩间隔（步）"
        name={["context", "compact_min_interval"]}
        extra="两次压缩之间至少隔这么多步，避免刚压完又压（每次压缩都会让前缀缓存失效一次）"
      >
        <InputNumber min={0} max={100} step={1} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="保留最近步数（原文）"
        name={["context", "keep_recent_steps"]}
        extra="最近这几步永远保留原文——模型最依赖最近上下文，压掉会明显掉质量"
      >
        <InputNumber min={1} max={100} step={1} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="骨架摘要上限（字符）"
        name={["context", "compact_max_chars"]}
        extra="压缩后那段摘要的长度上限"
      >
        <InputNumber min={200} max={20000} step={100} style={{ width: "100%" }} />
      </Form.Item>
    </Space>
  );

  const dumpTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        开启后，把每次发给大模型的完整 prompt（system + 全部历史消息）与模型
        响应写到本地文件，用于排查上下文膨胀、模型答非所问、某步为什么这么慢。
        落盘结构：
        <br />
        <code>&lt;目录&gt;/&lt;sessionId&gt;/&lt;messageId&gt;/&lt;序号-步骤&gt;.json</code>
        <br />
        即「会话 → 某一轮 → 该轮每次调用」，与用量详情里的分步一一对应。
      </Typography.Paragraph>

      <Form.Item label="开启落盘" name={["prompt_dump", "enabled"]} valuePropName="checked">
        <Switch />
      </Form.Item>
      <Form.Item
        label="落盘目录"
        name={["prompt_dump", "dir"]}
        extra="支持 ~ 开头的家目录路径"
      >
        <Input placeholder="~/.keeper/prompt-dump" />
      </Form.Item>
      <Form.Item
        label="最小输入 token"
        name={["prompt_dump", "min_prompt_tokens"]}
        extra="只记录输入 token 超过该值的调用；0 = 全部记录。排查上下文膨胀时建议设 50000，避免每次调用都写几万字"
      >
        <InputNumber min={0} step={1000} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item
        label="保留天数"
        name={["prompt_dump", "retain_days"]}
        extra="过期文件自动清理（最多每小时扫一次）"
      >
        <InputNumber min={0} max={365} style={{ width: "100%" }} />
      </Form.Item>
    </Space>
  );

  return (
    <div>
      <Typography.Title level={3} style={{ marginTop: 0 }}>
        设置
      </Typography.Title>
      <Typography.Text type="secondary">
        本机用户设置，写回 settings.yaml（与元配置 config.yaml 分开），改完立即生效。
      </Typography.Text>

      {settingsQ.data?.error ? (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12, maxWidth: 680 }}
          message="配置文件读取异常，已禁用保存"
          description={
            <>
              <div>{settingsQ.data.error}</div>
              <div style={{ marginTop: 4 }}>
                页面里显示的是内置默认值，不是你当前的真实配置。此时保存会把默认值
                整份写进 settings.yaml，把真实配置冲掉（落盘开关、超时、并行档位都会
                被打回默认）。请先修复上面的读取问题，或点「重新加载」重试。
              </div>
            </>
          }
        />
      ) : null}

      <Form
        form={form}
        layout="vertical"
        style={{ marginTop: 16, maxWidth: 680 }}
        onFinish={(v) => {
          // 双重保险：按钮已禁用，但回车提交也会走这里
          if (settingsError) return;
          saveM.mutate({
            // 兜底：万一某个 Tab 的字段没注册上，也别把整段提交成空
            a2a: { ...DEFAULT_A2A, ...(v.a2a || {}) },
            prompt_dump: { ...DEFAULT_DUMP, ...(v.prompt_dump || {}) },
            context: { ...DEFAULT_CONTEXT, ...(v.context || {}) },
          });
        }}
      >
        <Tabs
          items={[
            { key: "context", label: "上下文（压缩）", children: contextTab },
            { key: "a2a", label: "协作（A2A）", children: a2aTab },
            { key: "debug", label: "调试（Prompt 落盘）", children: dumpTab },
          ]}
        />
        <Space>
          <Button
            type="primary"
            loading={saveM.isPending}
            // 配置没读出来时表单里是默认值，保存会把真实配置冲掉——直接禁掉
            disabled={!!settingsError}
            onClick={() => form.submit()}
          >
            保存
          </Button>
          <Button
            loading={reloadM.isPending}
            onClick={() => reloadM.mutate()}
            title="绕过设置页直接改了 settings.yaml 时用；设置页保存会自动生效"
          >
            重新加载
          </Button>
        </Space>
      </Form>
    </div>
  );
}
