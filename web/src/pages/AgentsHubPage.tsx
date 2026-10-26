import { PageHeader } from "../components/ui/Page";
import AgentList from "./AgentList";
import PluginPage from "./PluginPage";

/** 两个平级入口的 key。放在路由里而不是组件内部 state：这样点菜单「智能体」总能
 *  回到智能体页，从「创建 Agent」返回时也能精确落回原来那一面。 */
export const AGENT_TABS = ["mine", "plugins"] as const;
export type AgentTab = (typeof AGENT_TABS)[number];

export function isAgentTab(v: unknown): v is AgentTab {
  return typeof v === "string" && (AGENT_TABS as readonly string[]).includes(v);
}

/**
 * 智能体 / 插件 —— 两个**独立入口**，一页只讲一件事。
 *
 * 原来这两件事挤在同一页里、用一条 tab 栏（「我的智能体 / 我的插件」）切换。
 * 有两个问题：
 *
 * 1. Keeper 底部的菜单里**已经有**「智能体」和「插件」两个平级入口了，页内再挂
 *    一条 tab 栏就成了**第二个切换器** —— 两处状态还得同步（点菜单切了 tab、
 *    页内又切回另一个，用户会以为点错了）。
 * 2. tab 栏的当前项与菜单的当前项可能对不上，出现"点了插件却显示智能体列表"。
 *
 * 现在页内不再有 tab 栏：菜单给哪个入口，就只渲染那一页的内容，页头也跟着换
 * （「智能体」/「插件」）。两处入口、两个页面，各管各的。
 *
 * tab 仍然放在路由里（而不是组件内部 state）：从菜单进来、从「创建 Agent」返回、
 * 刷新页面，都需要知道当前该显示哪一面。
 */
export default function AgentsHubPage({
  tab,
  onCreate,
}: {
  tab: AgentTab;
  onCreate: () => void;
}) {
  const plugins = tab === "plugins";
  return (
    <div className="mx-auto w-full max-w-[1180px]">
      <PageHeader
        title={plugins ? "插件" : "智能体"}
        desc={
          plugins
            ? "库里有什么、东西该往哪放。这一页只读——加插件就是往下面的目录里放文件。"
            : "我已有的智能体，以及它们用的插件。"
        }
      />

      {/* PageHeader 自带 mb-6，原来那个 mb-4 是给 tab 栏留的 —— 去掉 tab 栏就不用了。 */}
      {!plugins && <AgentList onCreate={onCreate} embedded />}
      {plugins && <PluginPage embedded />}
    </div>
  );
}
