import { useState } from "react";
import {
  Activity,
  Bot,
  Database,
  PanelLeft,
  Plus,
  Puzzle,
  Settings,
  ShoppingBag,
  Users,
} from "lucide-react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  SETTINGS_SECTIONS,
  SettingsDialog,
  type SettingsSection,
} from "./components/SettingsDialog";

/* Radix 的 dropdown-menu 拆成很多子组件，这里给最常用的几个起短名，
   免得下面每处都写一长串前缀 */
const {
  Trigger: DropdownMenuTrigger,
  Content: DropdownMenuContent,
  Item: DropdownMenuItem,
} = DropdownMenu;
import { useQuery } from "@tanstack/react-query";
import { cn } from "./lib/utils";
import ChatPage from "./pages/ChatPage";
import AgentList from "./pages/AgentList";
import AgentCreatePage from "./pages/AgentCreatePage";
import AgentMarketPage from "./pages/AgentMarketPage";
import PluginPage from "./pages/PluginPage";
import UserSpacePage from "./pages/UserSpacePage";
import ObservabilityPage from "./pages/ObservabilityPage";
import ModelManage from "./pages/ModelManage";
import { apiHealth, apiListAgents } from "./api/client";
import { ToastHost } from "./components/ui/Toast";
import { Pill } from "./components/ui/Pill";
import { Stat } from "./components/ui/Stat";

/**
 * 侧栏导航。
 *
 * 按截图的做法分成两组：主功能在上，低频的「管理」类在下（用一条分组标题
 * 隔开）。分组标题是 11px 的次级色文字，不是分隔线——桌面应用里分组靠
 * 字重和颜色区分，靠线条会显得很碎。
 */
const NAV_MAIN = [
  { key: "agents", label: "智能体", icon: Bot },
  { key: "agent-market", label: "智能体市场", icon: ShoppingBag },
  { key: "plugins", label: "插件", icon: Puzzle },
] as const;

const NAV_ADMIN = [
  { key: "models", label: "模型管理", icon: Database },
  { key: "user-spaces", label: "用户空间", icon: Users },
  { key: "observability", label: "可观测", icon: Activity },
] as const;

type Route = { page: string; agentId?: string };

export default function App() {
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem("keeper_sider_collapsed") === "1"
  );
  const toggleCollapsed = () => {
    setCollapsed((c) => {
      localStorage.setItem("keeper_sider_collapsed", c ? "0" : "1");
      return !c;
    });
  };
  const [route, setRoute] = useState<Route>(() => {
    const agentId = new URLSearchParams(window.location.search).get("agent");
    // 落地页 =「智能体」：概览页已撤下，而"开始对话"需要先有已装载的 agent，
    // 所以默认停在能给出这个前提的那一页
    return agentId ? { page: "chat", agentId } : { page: "agents" };
  });

  /* 退出对话回到「智能体」页（原先是「概览」，那一页已撤下）。 */
  const exitChat = () => setRoute({ page: "agents" });

  /* 设置弹窗当前展示哪一段；null = 未打开。设置从侧栏底部齿轮进入，
     选一段开一个弹窗，用完即走（低频操作不该占一个常驻导航位）。 */
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(
    null
  );

  /* 核心 agent = 新建会话的默认落点，由后端从设置里解析（未配则取第一个已装载的）。
     与概览页共用 ["agents"] 缓存，不会多打一次请求。 */
  const agentsQ = useQuery({ queryKey: ["agents"], queryFn: apiListAgents });
  const coreAgentId = agentsQ.data?.core_agent_id ?? null;
  /* 开始对话：直接进核心 agent 的会话。没有核心 agent（一个都没装载）时退到
     「智能体」页——那时用户唯一能做的就是先去装载一个。 */
  const startChat = () => {
    if (coreAgentId) setRoute({ page: "chat", agentId: coreAgentId });
    else setRoute({ page: "agents" });
  };

  const healthQ = useQuery({
    queryKey: ["health"],
    queryFn: apiHealth,
    refetchInterval: 10000,
  });
  const online = !healthQ.isLoading && !healthQ.isError && healthQ.data?.ok;
  const inChat = route.page === "chat" && !!route.agentId;

  /** 侧栏导航条目 */
  const NavItem = ({
    item,
    badge,
  }: {
    item: { key: string; label: string; icon: typeof Bot };
    badge?: string;
  }) => {
    const Icon = item.icon;
    const active = !inChat && route.page === item.key;
    return (
      <button
        type="button"
        title={collapsed ? item.label : undefined}
        onClick={() => setRoute({ page: item.key })}
        aria-current={active ? "page" : undefined}
        className={cn(
          /* 三种状态，三种"材质"：
               · 常态：**什么都不加**——没有边框、没有底色，只有文字浮在浅蓝上；
               · hover：整块填满深蓝 + 文字/图标转白；
               · 选中：白底。
             宽度 = 侧栏宽 - 左右各 10px 内边距，即与菜单栏同宽（只留一点边距），
             所以色块是"贴着"侧栏边缘的一整条，而不是缩在中间的小药丸。 */
          "group relative mb-0.5 flex items-center gap-2.5 rounded-[8px] py-[6px] px-2.5 text-[14px]",
          "transition-colors duration-150 ease-apple",
          collapsed ? "w-[34px] justify-center px-0" : "w-full",
          active ? "bg-panel font-medium text-ink-strong" : "text-ink-muted",
          /* hover 用主色的**深色文字**而不是白字：#3B7BF6 上白字只有
             3.8:1，而深蓝字在同一底色上有 4.3:1 —— 后者读起来更稳。
             这也让"浅蓝底 + 深蓝字"的观感贯穿侧栏的常态与 hover。 */
          !active && "hover:bg-brand hover:text-brand-deep"
        )}
      >
        {/* 图标跟着文字一起走：常态最淡、hover 深蓝、选中上品牌色。 */}
        <Icon
          className={cn(
            "h-[16px] w-[16px] shrink-0 transition-colors duration-150",
            active
              ? "text-brand"
              : "text-ink-faint group-hover:text-brand-deep"
          )}
        />
        {!collapsed && (
          <>
            <span className="min-w-0 flex-1 truncate text-left">{item.label}</span>
            {badge && (
              <span
                className={cn(
                  "shrink-0 rounded-full px-1.5 py-px text-[10.5px] font-medium tabular-nums transition-colors duration-150",
                  active
                    ? "bg-black/[0.06] text-ink-muted dark:bg-white/10"
                    : "bg-black/[0.06] text-ink-muted group-hover:bg-brand-deep/15 group-hover:text-brand-deep dark:bg-white/10"
                )}
              >
                {badge}
              </span>
            )}
          </>
        )}
      </button>
    );
  };

  return (
    <div className="flex h-screen w-full overflow-hidden bg-panel text-ink">
      {/* ============ 侧栏 ============ */}
      {/*
        截图里的侧栏结构（自上而下）：
          1. 产品或工作区名称 + 版本号（两行小字）
          2. 常驻搜索框（不是导航项）
          3. 导航（图标 + 文字 + 可选徽标）
          4. 分组标题 + 分组内容
          5. 底部账号条
        侧栏底色比内容区深一档（--kp-sidebar），这是桌面应用与网页最明显的
        区别：分区靠底色，不靠描边。
      */}
      <aside
        className={cn(
          /* 菜单栏底色是主题里的浅蓝（--kp-sidebar）；分隔线用浅蓝本身而非
             中性灰线，否则一条灰线会横穿在蓝底和蓝底之间很突兀。 */
          "flex shrink-0 flex-col bg-sidebar",
          "transition-[width] duration-200 ease-apple",
          collapsed ? "w-[52px]" : "w-[168px]"
        )}
        style={{ borderRight: "1px solid var(--kp-sidebar-line)" }}
      >
        {/* 1. 产品名 + 在线状态 + 折叠按钮

            logo 用品牌渐变 + 一道内高光（inset 白色半透明细线），这是让一个
            纯色方块看起来"有厚度"的最小成本做法。

            折叠按钮放在这里（右上角、纯图标）而不是底部：它是**视图状态**，
            不是导航项——放底部会让人误以为那里还有一个可去的页面。放在标题栏
            右上角，位置固定、也符合桌面应用的习惯（工具栏收拢按钮）。 */}
        <div
          className={cn(
            "flex shrink-0 items-center gap-2.5 pt-3.5",
            /* 左内边距与外层导航区的 px-2.5 对齐，logo、齿轮、药丸三者
               的左边缘才能落在同一条竖线上 */
            collapsed ? "justify-center px-0 pb-3" : "pl-2.5 pr-2 pb-3"
          )}
        >
          <button
            type="button"
            onClick={toggleCollapsed}
            title={collapsed ? "展开菜单" : "收起菜单"}
            className={cn(
              "flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[7px] transition-colors duration-150",
              collapsed
                ? "text-brand-deep hover:bg-white/70 dark:text-brand dark:hover:bg-white/[0.12]"
                : "text-brand-deep hover:bg-white/60 dark:text-brand dark:hover:bg-white/[0.12]"
            )}
          >
            <PanelLeft className="h-[15px] w-[15px]" />
          </button>
          <div className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[8px] bg-brand-gradient text-[12px] font-semibold text-white shadow-[0_2px_6px_-1px_rgba(91,155,245,0.5),inset_0_1px_0_rgba(255,255,255,0.28)]">
            K
          </div>
          {!collapsed && (
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-semibold leading-tight tracking-[-0.01em] text-ink-strong">
                Keeper
              </div>
              <div className="mt-[1px] flex items-center gap-1.5 text-[10.5px] leading-tight text-ink-faint">
                <span
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full",
                    online ? "bg-emerald-500" : "bg-red-500"
                  )}
                />
                {online ? "服务正常" : "服务离线"}
              </div>
            </div>
          )}
        </div>

        {/* 2. **开始对话**：会话是一等公民，所以它排在所有管理入口之前。

            原来这里放的是「搜索」框，点进去跳「智能体」页——那是 agent-first
            的残留：用户想聊天却先被丢到一份配置清单里。现在直接落核心 agent，
            一次点击就进对话；找不到智能体的时候才退回去让用户挑。
            「概览」页也随之撤下（它的全部职能就是"挑一个 agent 来聊"，
            正是这一步要消灭的决策成本）。 */}
        <div className="px-2.5 pb-2">
          <button
            type="button"
            onClick={startChat}
            disabled={!coreAgentId}
            title={
              coreAgentId
                ? "开始对话"
                : "还没有已装载的智能体，先到「智能体」页装载一个"
            }
            className={cn(
              "flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-[7px] text-[14px] font-medium transition-colors duration-150 ease-apple",
              coreAgentId
                ? "bg-white/70 text-ink-strong shadow-card hover:bg-white dark:bg-white/[0.12] dark:text-ink-strong dark:hover:bg-white/[0.16]"
                : "cursor-not-allowed text-ink-faint"
            )}
          >
            <Plus
              className={cn(
                "h-4 w-4 shrink-0",
                coreAgentId ? "text-brand" : "text-ink-faint"
              )}
            />
            {!collapsed && <span>开始对话</span>}
          </button>
        </div>

        {/* 3-4. 导航 + 分组 */}
        <nav className="flex-1 overflow-y-auto overflow-x-hidden px-2.5">
          {!collapsed && (
            <div className="pl-2.5 pb-1.5 pt-1 text-[11px] font-medium uppercase tracking-[0.06em] text-ink-faint">
              配置
            </div>
          )}
          {NAV_MAIN.map((item) => (
            <NavItem key={item.key} item={item} />
          ))}

          {!collapsed && (
            /* 分组标题与药丸左边缘对齐（药丸 pl-3 = 12px），标题也补 12px */
            <div className="pl-2.5 pb-1.5 pt-4 text-[11px] font-medium uppercase tracking-[0.06em] text-ink-faint">
              管理
            </div>
          )}
          {collapsed && <div className="my-2 border-t border-line-soft" />}
          {NAV_ADMIN.map((item) => (
            <NavItem key={item.key} item={item} />
          ))}
        </nav>

        {/* 5. 底部：设置齿轮

            设置是**低频操作**，所以不占侧栏一个常驻导航位（那会让"管理"分组
            永远挂着一项几乎没人点的条目），而是收进底部齿轮。点开是一个**向上**
            弹出的菜单：设置在底部，菜单往上弹才不会被窗口边缘挡住。

            菜单项带一行说明文字——四段设置的名字（上下文 / 协作 / 调试）都是
            内部术语，光看名字猜不出里面能改什么。 */}
        <div
          className="shrink-0 px-2.5 py-2"
          style={{ borderTop: "1px solid var(--kp-sidebar-line)" }}
        >
          <DropdownMenu.Root>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                title="设置"
                className={cn(
                  // 与导航项同一套状态语言与同样的宽度（hover 也是主色底 + 深色字）
                  "flex items-center gap-2.5 rounded-[8px] py-[6px] px-2.5 text-[14px] text-ink-muted",
                  "transition-colors duration-150 ease-apple",
                  "hover:bg-brand hover:text-brand-deep",
                  collapsed ? "mx-auto w-[34px] justify-center px-0" : "w-full"
                )}
              >
                <Settings className="h-[15px] w-[15px] shrink-0" />
                {!collapsed && <span>设置</span>}
              </button>
            </DropdownMenuTrigger>
            {/* side="top"：贴着触发器向上弹出 */}
            <DropdownMenuContent
              side="top"
              align="start"
              sideOffset={6}
              className="z-[1700] min-w-[236px] overflow-hidden rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
            >
              {SETTINGS_SECTIONS.map((s) => (
                <DropdownMenuItem
                  key={s.key}
                  onSelect={() => setSettingsSection(s.key)}
                  className="flex cursor-pointer flex-col gap-0.5 rounded-[7px] px-2.5 py-2 outline-none transition-colors duration-100 data-[highlighted]:bg-brand/12"
                >
                  <span className="text-[13px] font-medium text-ink-strong">
                    {s.label}
                  </span>
                  <span className="text-[11px] leading-4 text-ink-muted">
                    {s.desc}
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu.Root>
        </div>
      </aside>

      {/* ============ 主区 ============ */}
      <div className="flex min-w-0 flex-1 flex-col bg-panel">
        {/*
          这里原来有一条 44px 的顶栏（左「返回 / 页名」，右「智能体 / 设置」）。
          已整体移除：每个页面自己就有 h1 标题，顶栏那份是重复信息；右侧两个
          图标在侧栏「智能体」「设置」里都有常驻入口，属于多余的第二入口。
          去掉后内容从窗口顶部开始，页面自己的标题就是层级起点。

          唯一的例外是「返回」——它只在对话页有意义（对话页没有 h1，
          也没有别的回去的路），所以改由 ChatPage 自己负责退出。
        */}
        <main className="kp-canvas min-h-0 flex-1 overflow-hidden">
          {/* 「概览」页已撤下：它的全部职能是"列出 agent 让用户挑一个来聊"，
              而这正是本次要消灭的决策成本（侧栏「开始对话」直接落核心 agent）。
              「智能体」页继续承担"装载 / 配置 / 管理"的职能。 */}
          {route.page === "agents" && (
            <div className="h-full animate-fade-up overflow-auto px-7 py-6">
              <AgentList onCreate={() => setRoute({ page: "agent-create" })} />
            </div>
          )}
          {route.page === "agent-market" && (
            <div className="h-full animate-fade-up overflow-auto px-7 py-6">
              <AgentMarketPage />
            </div>
          )}
          {route.page === "agent-create" && (
            <div className="h-full animate-fade-up overflow-auto px-7 py-6">
              <AgentCreatePage onDone={() => setRoute({ page: "agents" })} />
            </div>
          )}
          {route.page === "plugins" && (
            <div className="h-full animate-fade-up overflow-auto px-7 py-6">
              <PluginPage />
            </div>
          )}
          {route.page === "user-spaces" && (
            <div className="h-full animate-fade-up overflow-auto px-7 py-6">
              <UserSpacePage />
            </div>
          )}
          {route.page === "observability" && (
            <div className="h-full animate-fade-up">
              <ObservabilityPage />
            </div>
          )}
          {route.page === "models" && (
            <div className="h-full animate-fade-up overflow-auto px-7 py-6">
              <ModelManage />
            </div>
          )}
          {inChat && (
            <ChatPage agentId={route.agentId!} onExit={exitChat} />
          )}
        </main>
      </div>

      {/* 设置弹窗：从侧栏底部齿轮 → 向上弹出的菜单 → 选一段开一个弹窗。
          「设置」已从侧栏导航里移除（它现在是这个齿轮），所以这里没有对应路由。 */}
      <SettingsDialog
        section={settingsSection}
        onClose={() => setSettingsSection(null)}
        onGoToAgents={() => {
          setSettingsSection(null);
          setRoute({ page: "agents" });
        }}
      />

      {/* 全局 Toast 挂载点 */}
      <ToastHost />
    </div>
  );
}
