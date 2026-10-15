import { PageHeader } from "../components/ui/Page";
import { Tabs } from "../components/ui/Tabs";
import AgentList from "./AgentList";
import AgentMarketPage from "./AgentMarketPage";
import PluginPage from "./PluginPage";

/** 三个 tab 的 key。tab 放在路由里而不是组件内部 state：这样点侧栏「智能体」总能
 *  回到第一个 tab，且从「创建 Agent」返回时能精确落回「我的智能体」。 */
export const AGENT_TABS = ["mine", "plugins", "market"] as const;
export type AgentTab = (typeof AGENT_TABS)[number];

export function isAgentTab(v: unknown): v is AgentTab {
  return typeof v === "string" && (AGENT_TABS as readonly string[]).includes(v);
}

/**
 * 智能体 —— 一个入口，三件事。
 *
 * 原来「智能体 / 智能体市场 / 插件」是侧栏三个平级入口，但它们其实是同一件事的
 * 三个面：**我的智能体**（库存）、**我的插件**（能力来源）、**智能体市场**（目录）。
 * 平级摆着有两个实际问题：侧栏「配置」分组被这三项占满，真正需要低频配置的东西
 * （模型、用户空间、回收站）只能往下挤；用户也分不清「插件」到底算不算智能体。
 *
 * 合成一页后按 tab 分工：页头只留一个标题，操作按钮（刷新 / 创建）交给各自的 tab
 * （embedded 模式）。
 *
 * tab 内容**按需挂载**：切走就卸载。否则三个列表的查询会同时在跑——「智能体市场」
 * 要打平台接口，一直挂着就会在用户根本没看它的时候也发请求。
 */
export default function AgentsHubPage({
  tab,
  onTabChange,
  onCreate,
}: {
  tab: AgentTab;
  onTabChange: (t: AgentTab) => void;
  onCreate: () => void;
}) {
  return (
    <div className="mx-auto w-full max-w-[1180px]">
      <PageHeader
        title="智能体"
        desc="我已有的智能体、它们用的插件，以及平台上还能装载什么。"
      />

      {/* mb-4 给 tab 栏与下方内容留呼吸位；Tabs 自带 border-b，这里不再重复描边 */}
      <Tabs
        className="mb-4"
        value={tab}
        onChange={(k) => onTabChange(k as AgentTab)}
        items={[
          { key: "mine", label: "我的智能体" },
          { key: "plugins", label: "我的插件" },
          { key: "market", label: "智能体市场" },
        ]}
      />

      {tab === "mine" && <AgentList onCreate={onCreate} embedded />}
      {tab === "plugins" && <PluginPage embedded />}
      {tab === "market" && <AgentMarketPage embedded />}
    </div>
  );
}