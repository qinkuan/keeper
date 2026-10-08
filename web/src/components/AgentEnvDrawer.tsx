import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Input, Select, Space, Spin, Tag, Typography, message } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiAgentEnv, apiUpdateAgentEnv } from "../api/client";
import { type AgentEnvResp, type DetectedRuntime, type EnvDependency, type MarketAgent } from "../api/types";

const CUSTOM = "__custom__";

/** 语义化比较两个版本号数组，a<b 返回负，a>b 返回正，相等返回 0。 */
function cmpVersion(a: number[], b: number[]): number {
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/** 本机实际版本是否落在 [min_version, max_version] 区间（无上限则只看下限）。 */
export function meetsVersion(
  actual: string,
  min?: string | null,
  max?: string | null,
): boolean {
  const av = actual.split(".").map((n) => parseInt(n, 10) || 0);
  if (min) {
    const mv = min.split(".").map((n) => parseInt(n, 10) || 0);
    if (cmpVersion(av, mv) < 0) return false;
  }
  if (max) {
    const xv = max.split(".").map((n) => parseInt(n, 10) || 0);
    if (cmpVersion(av, xv) > 0) return false;
  }
  return true;
}

/** python / node 解释器下拉：检测到的选项 + 「自定义路径」直接填绝对路径。 */
function RuntimeSelect({
  kind,
  detected,
  value,
  onChange,
}: {
  kind: "python" | "node";
  detected: DetectedRuntime[];
  value: string;
  onChange: (v: string) => void;
}) {
  const isCustom = value !== "" && !detected.some((d) => d.path === value);
  const options = [
    ...detected.map((d) => ({
      label: `${d.version_string}  (${d.path})`,
      value: d.path,
    })),
    { label: "自定义路径…", value: CUSTOM },
  ];
  if (value !== "" && !options.some((o) => o.value === value)) {
    options.unshift({ label: value, value });
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Select
        style={{ width: "100%" }}
        value={isCustom ? CUSTOM : value}
        onChange={(v: string) => onChange(v === CUSTOM ? value : v)}
        options={options}
        placeholder={kind === "python" ? "选择 Python 解释器" : "选择 Node 解释器"}
        allowClear
        onClear={() => onChange("")}
      />
      {isCustom && (
        <Input
          placeholder="填写解释器绝对路径，如 /usr/local/bin/python3"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          allowClear
        />
      )}
    </Space>
  );
}

/** 单条环境需求的合规结果。 */
function DependencyCheck({
  dep,
  detected,
}: {
  dep: EnvDependency;
  detected: DetectedRuntime[];
}) {
  const matched = detected.filter((d) => meetsVersion(d.version, dep.min_version, dep.max_version));
  const ok = matched.length > 0;
  const best = matched[0]; // detected 已降序，第一个即最高可用版本
  return (
    <Alert
      style={{ marginBottom: 8 }}
      type={ok ? "success" : "error"}
      showIcon
      message={
        <span>
          <Tag color={ok ? "green" : "red"}>{ok ? "满足" : "不满足"}</Tag>
          {dep.kind} ≥ {dep.min_version}
          {dep.max_version ? `, ≤ ${dep.max_version}` : ""}
        </span>
      }
      description={
        ok
          ? `本机可用：${best.version_string}（${best.path}）`
          : "未检测到满足该版本约束的解释器，请安装对应版本或手动指定路径。"
      }
    />
  );
}

/**
 * 某个 agent 的环境配置抽屉。
 *
 * - 展示该 agent 声明的环境依赖（kind + 版本），并与本机检测到的解释器做合规检查
 *   （绿色满足 / 红色不满足）。
 * - 已装载的 agent 可额外「指定解释器」（写到 config.yaml 的 agent_overrides），
 *   未装载时只做检测，不做安装。
 */
export default function AgentEnvDrawer({
  agent,
  loaded,
  open,
  onClose,
}: {
  agent: MarketAgent;
  loaded: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const agentId = agent.id;
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["agent-env", agentId],
    queryFn: () => apiAgentEnv(agentId),
    enabled: open && !!agentId,
  });

  const [python, setPython] = useState<string>("");
  const [node, setNode] = useState<string>("");

  useEffect(() => {
    if (q.data) {
      setPython(q.data.selection.python ?? "");
      setNode(q.data.selection.node ?? "");
    }
  }, [q.data]);

  const saveM = useMutation({
    mutationFn: () => apiUpdateAgentEnv(agentId, { python, node }),
    onSuccess: (d) => {
      message.success("已保存该 Agent 的解释器选择");
      qc.setQueryData(["agent-env", agentId], d);
    },
    onError: (e: Error) => message.error("保存失败：" + e.message),
  });

  const data: AgentEnvResp | undefined = q.data;
  const pyDetected = data?.detected.python ?? [];
  const nodeDetected = data?.detected.node ?? [];
  const deps = agent.env_dependencies ?? [];

  return (
    <Drawer
      title={`环境配置 · ${agent.name}`}
      width={560}
      open={open}
      onClose={onClose}
      footer={
        loaded ? (
          <Space style={{ float: "right" }}>
            <Button onClick={onClose}>关闭</Button>
            <Button type="primary" loading={saveM.isPending} onClick={() => saveM.mutate()}>
              保存
            </Button>
          </Space>
        ) : (
          <Space style={{ float: "right" }}>
            <Button type="primary" onClick={onClose}>
              关闭
            </Button>
          </Space>
        )
      }
    >
      {q.isLoading && <Spin />}
      {q.isError && (
        <Alert
          type="error"
          showIcon
          message="读取环境信息失败"
          description={(q.error as Error)?.message}
        />
      )}
      {data && (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="环境需求与本地合规"
            description="该 Agent 的依赖（MCP / 插件）需要 Python 与 Node 解释器来执行。下方列出它声明需要的环境，以及本机检测到的可用解释器；绿色表示满足，红色表示不满足。"
          />

          <div>
            <Typography.Text strong>该 Agent 声明需要的环境</Typography.Text>
            <div style={{ marginTop: 8 }}>
              {deps.length === 0 ? (
                <Typography.Text type="secondary">未声明环境依赖。</Typography.Text>
              ) : (
                deps.map((dep, i) => (
                  <DependencyCheck
                    key={i}
                    dep={dep}
                    detected={dep.kind === "python" ? pyDetected : nodeDetected}
                  />
                ))
              )}
            </div>
          </div>

          <div>
            <Typography.Text strong>本机检测到的解释器</Typography.Text>
            <div style={{ marginTop: 8 }}>
              <Typography.Text type="secondary">Python：</Typography.Text>
              {pyDetected.length === 0 ? (
                <Typography.Text type="warning"> 未检测到</Typography.Text>
              ) : (
                <span>
                  {pyDetected.map((d) => (
                    <Tag key={d.path}>{d.version_string}</Tag>
                  ))}
                </span>
              )}
              <br />
              <Typography.Text type="secondary">Node：</Typography.Text>
              {nodeDetected.length === 0 ? (
                <Typography.Text type="warning"> 未检测到</Typography.Text>
              ) : (
                <span>
                  {nodeDetected.map((d) => (
                    <Tag key={d.path}>{d.version_string}</Tag>
                  ))}
                </span>
              )}
            </div>
          </div>

          {loaded && (
            <div>
              <Typography.Text strong>指定解释器（覆盖默认）</Typography.Text>
              <div style={{ marginTop: 8 }}>
                <Typography.Text>Python</Typography.Text>
                <div style={{ marginBottom: 8 }}>
                  <RuntimeSelect kind="python" detected={pyDetected} value={python} onChange={setPython} />
                </div>
                <Typography.Text>Node</Typography.Text>
                <div>
                  <RuntimeSelect kind="node" detected={nodeDetected} value={node} onChange={setNode} />
                </div>
              </div>
              <Typography.Text type="secondary">
                指定的路径会按 agent 记录到
                <Typography.Text code>agent_overrides</Typography.Text>
                ，装载时优先采用。不安装任何环境。
              </Typography.Text>
            </div>
          )}
        </Space>
      )}
    </Drawer>
  );
}
