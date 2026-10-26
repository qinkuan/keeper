import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Bot,
  ChevronRight,
  Database,
  MessageSquare,
  PanelLeft,
  Search,
  Settings,
  Trash2,
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
  Sub: DropdownMenuSub,
  SubTrigger: DropdownMenuSubTrigger,
  SubContent: DropdownMenuSubContent,
  Separator: DropdownMenuSeparator,
} = DropdownMenu;
import { useQuery } from "@tanstack/react-query";
import { cn } from "./lib/utils";
import ChatPage from "./pages/ChatPage";
import AgentsHubPage, { isAgentTab, type AgentTab } from "./pages/AgentsHubPage";
import AgentCreatePage from "./pages/AgentCreatePage";
import UserSpacePage from "./pages/UserSpacePage";
import ObservabilityPage from "./pages/ObservabilityPage";
import ModelManage from "./pages/ModelManage";
import RecycleBinPage from "./pages/RecycleBinPage";
import SearchPage from "./pages/SearchPage";
import { apiHealth, apiListAgents } from "./api/client";
import { ToastHost } from "./components/ui/Toast";
import { Pill } from "./components/ui/Pill";
import { Stat } from "./components/ui/Stat";

/**
 * 侧栏常驻导航。三项，都是"去干活"而不是"去配置"：
 *
 * - **对话**：点一下开一段新对话并进去。它是侧栏里的**主动作**，但形式上和
 *   「智能体」一样是导航项 —— 因为两者回答的是同一个问题："我要干什么"，
 *   而不是一个是动作、一个是页面。点击时会先建好会话（见 startChat）。
 * - **智能体**：装下「我的智能体 / 我的插件」两个 tab。
 *
 * 会话记录本身（增删改查）搬进了对话页的「会话管理 / 任务管理」面板：那是会话
 * 真正待着的地方，把它们拆在两个界面只会让人找不到重命名和删除。
 *
 * 低频配置（设置 / 可观测 / 回收站 / 模型管理）收在底部 Keeper 菜单；
 * 「我的空间」是高频入口，提升为顶部主导航项。
 */
/**
 * macOS 桌面壳：窗口**没有系统标题栏**（main.js 设了 titleBarStyle: 'hidden'），
 * 只留三个红绿灯浮在窗口左上角。
 *
 * 顶上因此要自己补一条能抓住窗口的地方：横跨整窗的拖拽条（TopDragBar）。
 * 它盖在**侧栏和内容区上面**，侧栏与内容区都从它下面开始 ——
 * 这样"侧栏 / 顶条 / 内容区"三者的交点就在一个点上，
 * 内容面板的左上圆角正好画在这个交点上（VS Code 的编辑器区就是这么衔接的）。
 *
 * 只在 macOS 桌面壳下出现：Windows / Linux 仍是系统标题栏（那里不需要），
 * 纯 Web 版也没有红绿灯 —— 否则顶部会平白多出一条。
 */
function isMacDesktopShell(): boolean {
  if (typeof window === "undefined") return false;
  const host = (window as unknown as { keeperHost?: { platform?: string } }).keeperHost;
  return host?.platform === "darwin";
}

/**
 * 顶条：横跨整窗的一条（红绿灯在最左），可以拖着它移动窗口。
 *
 * 外观与内容区一致（同一块底色），不放标题：每个页面都有自己的大标题，
 * 这里再写一遍是重复。
 */
function TopDragBar() {
  if (!isMacDesktopShell()) return null;
  return <div className="kp-dragbar" />;
}

/** 可拖拽竖分隔条：拖动时按水平位移回调，父级据此调整右栏宽度。 */
function HResizer({ onResize }: { onResize: (dx: number) => void }) {
  const dragging = useRef(false);
  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (dragging.current) onResize(e.movementX);
    };
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [onResize]);
  return (
    <div
      onMouseDown={() => {
        dragging.current = true;
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      }}
      style={{
        width: 6,
        flex: "0 0 auto",
        cursor: "col-resize",
        background: "var(--kp-border-soft)",
      }}
      className="transition-colors hover:bg-brand/40"
    />
  );
}

const NAV_MAIN = [
  { key: "chat", label: "对话", icon: MessageSquare },
  { key: "agents", label: "智能体", icon: Bot },
  { key: "user-spaces", label: "我的空间", icon: Users },
] as const;

type Route = {
  page: string;
  agentId?: string;
  /**
   * 「智能体」页当前停在哪个 tab（我的智能体 / 我的插件）。
   *
   * 放路由里而不是组件内部 state：点侧栏「智能体」要能回到第一个 tab（而不是
   * 记住上次那个），从「创建 Agent」返回也要能精确落回「我的智能体」。缺省即第一个。
   */
  agentTab?: AgentTab;
};

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

  /* 设置弹窗当前展示哪一段；null = 未打开。设置从侧栏底部齿轮进入，
     选一段开一个弹窗，用完即走（低频操作不该占一个常驻导航位）。 */
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(
    null
  );

  /* 「我的空间」右侧常驻对话面板的宽度（可拖拽调节，落 localStorage）。 */
  const [chatWidth, setChatWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem("keeper_chat_dock_width"));
    return saved >= 300 && saved <= 720 ? saved : 400;
  });
  const onChatResize = useCallback(
    (dx: number) => setChatWidth((w) => Math.max(300, Math.min(720, w - dx))),
    []
  );
  useEffect(() => {
    localStorage.setItem("keeper_chat_dock_width", String(chatWidth));
  }, [chatWidth]);
  /* 常驻对话面板是否收起（仿侧栏「隐藏菜单」的折叠交互）。 */
  const [chatCollapsed, setChatCollapsed] = useState(false);

  /* 核心 agent = 新建会话的默认落点。它的 id 由后端从配置里解析
   （config.yaml 的 core_agent.id，默认 "core"），所以一定有值——除非
   一个 agent 都没装配成功。共用 ["agents"] 缓存，不会多打一次请求。 */
  const agentsQ = useQuery({ queryKey: ["agents"], queryFn: apiListAgents });
  const coreAgentId = agentsQ.data?.core_agent_id ?? null;

  /* 「对话」：**直接进对话页**，不预建会话。

     原先这里会先 POST 一条空会话再进去，理由是"空页面刷新就没了"。但那条会话
     是**建在点击的那一刻**的：用户点「对话」往往只是想回到刚才那段对话，结果
     每次都被丢进一条新的空白会话，历史会话反而要靠左上角的列表入口去找。

     现在不预建 —— 对话页自己会用 localStorage 恢复上次看的那条会话（见
     ChatPage 的 remembered()），没有可用会话时就是欢迎语空态，用户在那边点
     「新对话」或直接发第一条消息即可（后端在第一条消息时才真正建会话）。
     想显式开新对话，走对话页里的「新对话」按钮，那才是语义正确的位置。 */
  const openChat = () => {
    if (!coreAgentId) {
      // 一个 agent 都没有：用户唯一能做的就是先去装载一个
      setRoute({ page: "agents" });
      return;
    }
    setRoute({ page: "chat", agentId: coreAgentId });
  };

  /* 「智能体」页的 tab：路由里可能缺省（点侧栏进来）或带着旧值，统一规范化成一个
     合法 key。非法值（手改 URL、旧版本残留的路由状态）落到第一个 tab，而不是让
     Tabs 一个都不选中。 */
  const agentTab: AgentTab = isAgentTab(route.agentTab) ? route.agentTab : "mine";

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
    onClick,
    disabled,
  }: {
    item: { key: string; label: string; icon: typeof Bot };
    /** 覆盖默认的「跳到该页」。「对话」用它做"新建一段并进去" */
    onClick?: () => void;
    /** 没有已装载的 agent 时为 true：用户唯一能做的就是先去装载一个 */
    disabled?: boolean;
  }) => {
    const Icon = item.icon;
    // 在对话页时，「对话」这一项仍然算选中：它就是用户此刻所在的地方，
    // 而不是一个"已经离开"的页面。
    const active = inChat ? item.key === "chat" : route.page === item.key;
    return (
      <button
        type="button"
        title={
          disabled
            ? "还没有已装载的智能体，先到「智能体」页装载一个"
            : collapsed
              ? item.label
              : undefined
        }
        disabled={disabled}
        onClick={disabled ? undefined : (onClick ?? (() => setRoute({ page: item.key })))}
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
          disabled && "cursor-not-allowed opacity-45",
          /* 选中态是**淡蓝底**而不是白块：侧栏底色是 App Store 的冷灰（#F2F2F7），
             白块会和底色糊在一起，"当前在哪"就看不清了。 */
          active
            ? "bg-sidebar-active font-medium text-ink-strong"
            : "text-ink-muted",
          /* hover 用主色的**深色文字**而不是白字：#3B7BF6 上白字只有
             3.8:1，而深蓝字在同一底色上有 4.3:1 —— 后者读起来更稳。
             这也让"浅蓝底 + 深蓝字"的观感贯穿侧栏的常态与 hover。 */
          !active && "hover:bg-brand hover:text-white"
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
          <span className="min-w-0 flex-1 truncate text-left">{item.label}</span>
        )}
      </button>
    );
  };

  /**
   * 底部 Keeper 菜单里"点了就跳页面"的项。
   *
   * 与设置子菜单里的项不同：这些有**确定的目标路由**，所以要能显示"当前正在这一页"，
   * 否则用户点了菜单却看不出自己在哪（尤其菜单会自动关闭）。高亮用淡蓝底而不是
   * 主色实底——菜单是浮层，实底会和菜单自身抢注意力。
   */
  const MenuLink = ({
    icon: Icon,
    label,
    active,
    onSelect,
  }: {
    icon: typeof Bot;
    label: string;
    active?: boolean;
    onSelect: () => void;
  }) => (
    <DropdownMenuItem
      onSelect={onSelect}
      className={cn(
        "flex cursor-pointer items-center gap-2.5 rounded-[7px] px-2.5 py-[7px] text-[13px] outline-none transition-colors duration-100",
        active ? "bg-brand/12" : "data-[highlighted]:bg-brand/12"
      )}
    >
      <Icon
        className={cn(
          "h-[15px] w-[15px] shrink-0",
          active ? "text-brand" : "text-ink-faint"
        )}
      />
      <span
        className={cn(
          "flex-1 font-medium",
          active ? "text-brand-deep dark:text-brand-strong" : "text-ink-strong"
        )}
      >
        {label}
      </span>
      {active && <span className="text-[10.5px] text-brand-deep dark:text-brand-strong">当前</span>}
    </DropdownMenuItem>
  );

  return (
    /* 根容器用**侧栏的底色**：内容面板左上角切了圆角后，圆角里透出来的就是根容器，
       于是侧栏的颜色自然地绕进内容面板的角上（VS Code 的编辑器区就是这么衔接的）。
       侧栏和内容列各自铺满自己的位置，根底色只在圆角那一小块露出来。 */
    <div className="relative flex h-screen w-full flex-col overflow-hidden bg-sidebar text-ink">
      {/* 顶条：横跨整窗的一条（红绿灯在最左），侧栏与内容区都从它下面开始。
          这样"侧栏 / 顶条 / 内容区"三面交在同一个点上，内容面板的左上圆角
          正好画在这个交点上。 */}
      <TopDragBar />
      <div className="flex min-h-0 flex-1">
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
          /* 菜单栏底色是主题里的浅蓝（--kp-sidebar）。 */
          "flex shrink-0 flex-col bg-sidebar",
          "transition-[width] duration-200 ease-apple",
          /* box-border：项目关了 preflight，box-sizing 默认 content-box，
             边框/内边距会额外加在宽度外面。 */
          "box-border",
          /* 收起态就是 52px（图标窄轨）。红绿灯那组按钮宽约 52px，
             侧栏比它窄时按钮会探出一点、压到顶条上 —— 顶条是空的（只有拖拽区），
             所以压不到任何东西。不必为了"把红绿灯包进侧栏"而把侧栏撑宽。 */
          collapsed ? "w-[52px]" : "w-[168px]"
        )}
        style={{
          /* 侧栏从顶条下面开始（不再自己留红绿灯的位置）：顶条已经横跨整窗，
             红绿灯浮在顶条上。侧栏与内容区的分界就是内容面板的圆角那条线，
             所以这里不画右边框 —— 画了会是直线，圆角接不上。 */
        }}
      >
        {/* 1. **开始对话**

            这里原来有一块"产品名 + Keeper 字样 + 在线状态 + 折叠按钮"。它已整块
            移到底部（见第 3 块）：顶部不再有任何块 —— 侧栏现在只有两个导航项。

            这里原先有两块：「开始对话」按钮 + 下面的会话轨（SessionRail）。
            两块都已撤掉，理由不同：
            - 会话轨（列出会话 + 重命名 / 删除）搬进了**对话页**的「会话管理 /
              任务管理」面板。增删改查聚在一处，用户不必在两个界面之间来回找动作；
            - 「开始对话」变成下面 nav 里的**「对话」导航项**——它是侧栏里的主动作，
              但形式上和「智能体」一样是导航项（两者回答同一个问题："我要干什么"），
              不该一个用按钮形态、一个用菜单项形态。点击时仍然先建会话再进去。*/}
        {/* 侧栏顶部留出的 38px（aside 的 padding-top）是给红绿灯的位置：
            侧栏底色一直铺到窗口上沿，但内容要压到红绿灯下面。*/}
        <nav className="shrink-0 px-2.5 pt-3.5">
          {NAV_MAIN.map((item) =>
            item.key === "chat" ? (
              <NavItem
                key={item.key}
                item={item}
                onClick={openChat}
                /* 没有已装载的 agent 时禁用：用户唯一能做的就是先去装载一个。 */
                disabled={!coreAgentId}
              />
            ) : (
              <NavItem key={item.key} item={item} />
            )
          )}
        </nav>

        {/* 4. 底部：Keeper 身份块 —— **也是全局菜单的入口**

            `mt-auto`：吃掉剩余高度把这一块压到窗口下沿。让导航块去顶（`flex-1` +
            `justify-end`）同样能贴底，但会把「智能体」一起推下去，看起来像菜单的一部分。

            logo 用品牌渐变 + 一道内高光（inset 白色半透明细线），这是让一个纯色方块
            看起来"有厚度"的最小成本做法。

            折叠按钮：展开态在身份块右侧同一行；**折叠态改为竖排**（logo 在上、按钮在
            下）——52px 宽塞不进并排的两个 26px 控件，硬挤会把 logo 压掉一半；而且收起
            之后必须还留着**一个能直接点开**的入口，不能逼用户先点开菜单才找得到。 */}
        <div
          className="mt-auto shrink-0 px-2.5 py-2"
          style={{ borderTop: "1px solid var(--kp-sidebar-line)" }}
        >
          {/* 折叠态竖排（logo 上 / 展开按钮 下），展开态横排 —— 见上面说明 */}
          <div className={cn("flex gap-1", collapsed ? "flex-col items-center" : "items-center")}>
            <DropdownMenu.Root>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  title="Keeper — 设置与管理"
                  className={cn(
                    "flex items-center gap-2.5 rounded-[8px] py-[6px] pl-2.5 pr-2",
                    "text-left transition-colors duration-150 ease-apple",
                    "hover:bg-brand/12 dark:hover:bg-brand/20",
                    collapsed ? "w-[34px] justify-center px-0" : "min-w-0 flex-1"
                  )}
                >
                  <span className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[8px] bg-brand-gradient text-[12px] font-semibold text-white shadow-[0_2px_6px_-1px_rgba(66,133,244,0.5),inset_0_1px_0_rgba(255,255,255,0.28)]">
                    K
                  </span>
                  {!collapsed && (
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] font-semibold leading-tight tracking-[-0.01em] text-ink-strong">
                        Keeper
                      </span>
                      <span className="mt-[1px] flex items-center gap-1.5 text-[10.5px] leading-tight text-ink-faint">
                        <span
                          className={cn(
                            "h-1.5 w-1.5 shrink-0 rounded-full",
                            online ? "bg-emerald-500" : "bg-red-500"
                          )}
                        />
                        {online ? "服务正常" : "服务离线"}
                      </span>
                    </span>
                  )}
                </button>
              </DropdownMenuTrigger>
              {/* side="top"：菜单在底部，必须往上弹才不会顶出窗口 */}
              <DropdownMenuContent
                side="top"
                align="start"
                sideOffset={6}
                className="z-[1700] min-w-[236px] overflow-hidden rounded-[10px] border border-line-soft bg-panel p-1 shadow-pop"
              >
                {/* 设置：带子菜单。子菜单由 Radix 在**悬停**时自动展开——
                    这比"点开子菜单"少一次点击，也符合桌面应用的习惯。
                    「我的空间」是单独的顶层菜单项（直接开配置页），
                    不再归在「设置」子菜单里。 */}
                <DropdownMenu.Sub>
                  <DropdownMenuSubTrigger
                    className="flex cursor-pointer items-center gap-2.5 rounded-[7px] px-2.5 py-[7px] text-[13px] outline-none transition-colors duration-100 data-[highlighted]:bg-brand/12"
                  >
                    <Settings className="h-[15px] w-[15px] shrink-0 text-ink-faint" />
                    <span className="flex-1 font-medium text-ink-strong">
                      设置
                    </span>
                    <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-faint" />
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent
                    sideOffset={4}
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
                  </DropdownMenuSubContent>
                </DropdownMenu.Sub>

                <MenuLink
                  icon={Activity}
                  label="可观测"
                  active={route.page === "observability"}
                  onSelect={() => setRoute({ page: "observability" })}
                />
                <MenuLink
                  icon={Search}
                  label="搜索"
                  active={route.page === "search"}
                  onSelect={() => setRoute({ page: "search" })}
                />
                <MenuLink
                  icon={Trash2}
                  label="回收站"
                  active={route.page === "trash"}
                  onSelect={() => setRoute({ page: "trash" })}
                />
                <MenuLink
                  icon={Database}
                  label="模型管理"
                  active={route.page === "models"}
                  onSelect={() => setRoute({ page: "models" })}
                />
              </DropdownMenuContent>
            </DropdownMenu.Root>

            {/* 折叠 / 展开按钮：两个状态都要**直接可见**。
                收起之后如果只能点开菜单才找得到展开入口，用户会以为侧栏再也
                打不开了 —— 视图开关的可见性不该依赖另一层交互。 */}
            <button
              type="button"
              onClick={toggleCollapsed}
              title={collapsed ? "展开菜单栏" : "收起菜单栏"}
              className="flex h-[26px] w-[24px] shrink-0 items-center justify-center rounded-[7px] text-brand-deep transition-colors duration-150 hover:bg-white/60 dark:text-brand dark:hover:bg-white/[0.12]"
            >
              <PanelLeft className="h-[15px] w-[15px]" />
            </button>
          </div>
        </div>
      </aside>

      {/* ============ 主区 ============ */}
      {/* kp-canvas（带光晕的画布底色）挂在**这一列**上，而不是 main：
          顶上那条可拖动的边要和内容区是同一块底色，底色画在列上，
          两边才是连续的（画在 main 上，中间隔着拖拽边就会有一道接缝）。

          rounded-tl + 左边框：内容面板的左上角切一个圆角，侧栏的底色从圆角里透出来，
          视觉上像"内容面板搭在侧栏上"（VS Code 的编辑器区就是这么衔接的）。
          分隔线画在**这一列的左边**而不是侧栏的右边 —— 前者会跟着圆角走，
          后者是一条直线，圆角就接不上。
          overflow-hidden：让子元素（页面内容）也跟着圆角裁切，
          否则内容的直角会戳出圆角。 */}
      <div
        className="kp-canvas flex min-w-0 flex-1 flex-col box-border overflow-hidden rounded-tl-[14px]"
        style={{ borderLeft: "1px solid var(--kp-sidebar-line)" }}
      >
        {/*
          这里原来有一条 44px 的顶栏（左「返回 / 页名」，右「智能体 / 设置」）。
          已整体移除：每个页面自己就有 h1 标题，顶栏那份是重复信息；右侧两个
          图标在侧栏「智能体」「设置」里都有常驻入口，属于多余的第二入口。
          去掉后内容从窗口顶部开始，页面自己的标题就是层级起点。

          唯一的例外是「返回」——它只在对话页有意义（对话页没有 h1，
          也没有别的回去的路），所以改由 ChatPage 自己负责退出。
        */}
        <main className="min-h-0 flex-1 overflow-hidden">
          {/* 「概览」页已撤下：它的全部职能是"列出 agent 让用户挑一个来聊"，
              而这正是本次要消灭的决策成本（侧栏「开始对话」直接落核心 agent）。
              「智能体」页继续承担"装载 / 配置 / 管理"的职能。 */}
          {route.page === "agents" && (
            /* box-border：项目关了 Tailwind preflight，box-sizing 是 content-box，
               h-full（height:100%）之外还要再加 py-6 的 48px 内边距——容器比父级
               高出 48px，而 main 是 overflow-hidden，底部那截被裁掉且滚不到。
               症状：页面滚到底，最后一块仍然贴着窗口底边，没有留白。
               改成 border-box 后内边距算进高度里，底部留白才露得出来。 */
            <div className="h-full animate-fade-up box-border overflow-auto px-7 py-6">
              <AgentsHubPage
                tab={agentTab}
                onTabChange={(t) => setRoute({ page: "agents", agentTab: t })}
                onCreate={() =>
                  // 进创建页时记住来源 tab：创建完要落回它，而不是把用户丢回
                  // 另一个 tab（现在来源固定是「我的智能体」）
                  setRoute({ page: "agent-create", agentTab })
                }
              />
            </div>
          )}
          {route.page === "agent-create" && (
            <div className="h-full animate-fade-up box-border overflow-auto px-7 py-6">
              <AgentCreatePage
                onDone={() => setRoute({ page: "agents", agentTab })}
              />
            </div>
          )}
          {route.page === "user-spaces" && (
            <div className="flex h-full animate-fade-up">
              {/* 左：我的空间（文件树 + 预览） */}
              <div className="min-w-0 flex-1 overflow-auto px-7 py-6">
                <UserSpacePage />
              </div>
              {/* 右：常驻对话面板——可拖宽、可整体收起（仿侧栏「隐藏菜单」）。
                  收起时不卸载，只把宽度置 0 + overflow-hidden 隐藏，从而保留对话现场。 */}
              {!chatCollapsed && <HResizer onResize={onChatResize} />}
              <div
                className={
                  chatCollapsed
                    ? "flex shrink-0 flex-col overflow-hidden"
                    : "flex shrink-0 flex-col overflow-hidden border-l border-line-soft"
                }
                style={{ width: chatCollapsed ? 0 : chatWidth }}
              >
                <div className="flex h-9 shrink-0 items-center border-b border-line-soft px-3 text-[13px] font-medium text-ink-strong">
                  对话
                  <button
                    type="button"
                    onClick={() => setChatCollapsed(true)}
                    title="收起对话"
                    className="ml-auto flex h-6 w-6 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                  >
                    <PanelLeft className="h-3.5 w-3.5" />
                  </button>
                </div>
                <div className="min-h-0 flex-1">
                  {coreAgentId ? (
                    <ChatPage agentId={coreAgentId} />
                  ) : (
                    <div className="flex h-full items-center justify-center px-4 text-center text-[12px] leading-5 text-ink-muted">
                      请先在「智能体」页装载一个智能体，才能开始对话
                    </div>
                  )}
                </div>
              </div>
              {chatCollapsed && (
                <div className="flex w-9 shrink-0 flex-col items-center border-l border-line-soft py-2">
                  <button
                    type="button"
                    onClick={() => setChatCollapsed(false)}
                    title="展开对话"
                    className="flex h-7 w-7 items-center justify-center rounded-md text-ink-faint transition-colors hover:bg-surface hover:text-ink-strong"
                  >
                    <MessageSquare className="h-4 w-4" />
                  </button>
                </div>
              )}
            </div>
          )}
          {route.page === "observability" && (
            <div className="h-full animate-fade-up">
              <ObservabilityPage />
            </div>
          )}
          {route.page === "search" && (
            <div className="h-full animate-fade-up box-border overflow-auto px-7 py-6">
              <SearchPage />
            </div>
          )}
          {route.page === "trash" && (
            <div className="h-full animate-fade-up box-border overflow-auto px-7 py-6">
              <RecycleBinPage />
            </div>
          )}
          {route.page === "models" && (
            <div className="h-full animate-fade-up box-border overflow-auto px-7 py-6">
              <ModelManage />
            </div>
          )}
          {inChat && (
            <ChatPage agentId={route.agentId!} />
          )}
        </main>
      </div>
      </div>

      {/* 设置弹窗：从侧栏底部齿轮 → 向上弹出的菜单 → 选一段开一个弹窗。
          「设置」已从侧栏导航里移除（它现在是这个齿轮），所以这里没有对应路由。 */}
      <SettingsDialog
        section={settingsSection}
        onClose={() => setSettingsSection(null)}
      />

      {/* 全局 Toast 挂载点 */}
      <ToastHost />
    </div>
  );
}
