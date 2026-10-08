import { useState } from "react";
import {
  Button,
  Card,
  Empty,
  Layout,
  List,
  Menu,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  AppstoreOutlined,
  RobotOutlined,
  DatabaseOutlined,
  UserOutlined,
  LineChartOutlined,
  SettingOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ApiOutlined,
  ShopOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import ChatPage from "./pages/ChatPage";
import AgentList from "./pages/AgentList";
import AgentCreatePage from "./pages/AgentCreatePage";
import AgentMarketPage from "./pages/AgentMarketPage";
import PluginPage from "./pages/PluginPage";
import UserSpacePage from "./pages/UserSpacePage";
import ObservabilityPage from "./pages/ObservabilityPage";
import ModelManage from "./pages/ModelManage";
import SettingsPage from "./pages/SettingsPage";
import { apiHealth, apiListAgents } from "./api/client";

const { Header, Sider, Content } = Layout;

/** 概览：列出本进程已装载的所有 agent，点卡片进入对应对话。 */
function Overview({ onOpen }: { onOpen: (id: string) => void }) {
  const q = useQuery({ queryKey: ["agents"], queryFn: apiListAgents });

  return (
    <div style={{ height: "100%" }}>
      <Typography.Title level={3}>概览</Typography.Title>
      <Typography.Text type="secondary">
        本进程已装载的 agent。点卡片进入对话；agent 与 mcp / skill / tool 的配置统一在平台维护。
      </Typography.Text>
      {q.isLoading && <Spin />}
      {q.isError && (
        <Typography.Text type="danger">
          加载失败：{(q.error as Error)?.message}
        </Typography.Text>
      )}
      {q.data && q.data.agents.length === 0 && (
        <Empty
          style={{ marginTop: 40 }}
          description="还没有已装载的 agent。请到「智能体」页点「装载」，把平台上的配置拉到本进程装配。"
        />
      )}
      <List
        style={{ marginTop: 16 }}
        grid={{ gutter: 16, column: 3 }}
        dataSource={q.data?.agents ?? []}
        renderItem={(a) => {
          const raw = (a.description ?? "").trim();
          // 超长先按字符截断，再由 CSS 固定为 2 行省略（卡片等高）
          const desc = raw.length > 120 ? `${raw.slice(0, 120)}…` : raw;
          return (
            <List.Item>
              <Card
                hoverable
                className="kp-lift"
                onClick={() => onOpen(a.id)}
                style={{
                  height: "100%",
                  borderRadius: 18,
                  border: "1px solid var(--kp-border-soft)",
                  overflow: "hidden",
                  cursor: "pointer",
                }}
                styles={{
                  body: {
                    height: "100%",
                    padding: 20,
                    display: "flex",
                    flexDirection: "column",
                  },
                }}
              >
                {/* 头部：图标 + 名称（不用 Card 默认 title，排版更可控） */}
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    marginBottom: 12,
                  }}
                >
                  <div
                    style={{
                      width: 34,
                      height: 34,
                      borderRadius: 10,
                      background: "var(--kp-surface)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 16,
                      color: "var(--kp-text)",
                    }}
                  >
                    <RobotOutlined />
                  </div>
                  <span
                    title={a.name}
                    style={{
                      fontSize: 16,
                      fontWeight: 600,
                      letterSpacing: "-0.01em",
                      color: "var(--kp-text-strong)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {a.name}
                  </span>
                  {/* 来源：两类 agent 的配置所有权不同，必须让人一眼看出这个该去哪改 */}
                  <Tag
                    color={a.origin === "local" ? "green" : "blue"}
                    style={{ marginInlineEnd: 0, flex: "none" }}
                  >
                    {a.origin === "local" ? "本地" : "平台"}
                  </Tag>
                </div>

                {/* 简介：固定 2 行 + 超长省略 */}
                <div
                  title={raw || undefined}
                  style={{
                    display: "-webkit-box",
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                    minHeight: 44,
                    fontSize: 13,
                    lineHeight: "22px",
                    color: raw ? "var(--kp-text-secondary)" : "var(--kp-text-muted)",
                  }}
                >
                  {desc || "暂无描述"}
                </div>

                {/* 底部：状态 + 进入提示 */}
                <div
                  style={{
                    marginTop: "auto",
                    paddingTop: 14,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                  }}
                >
                  <span
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      fontSize: 12,
                      color: "var(--kp-text-secondary)",
                    }}
                  >
                    <span
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: "50%",
                        background: a.ready ? "#3fa46a" : "#e0a33a",
                      }}
                    />
                    {a.ready ? "就绪" : "未就绪"}
                    {a.mcp_connected && (
                      <Tag style={{ marginInlineStart: 4, fontSize: 11 }}>MCP</Tag>
                    )}
                  </span>
                  <span
                    className="kp-arrow"
                    style={{ fontSize: 13, color: "var(--kp-text-secondary)" }}
                  >
                    进入 →
                  </span>
                </div>
              </Card>
            </List.Item>
          );
        }}
      />
    </div>
  );
}

type Route = { page: string; agentId?: string };

export default function App() {
  // 左侧菜单栏收缩状态，持久化到本地存储以记住用户偏好。
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem("keeper_sider_collapsed") === "1"
  );
  const toggleCollapsed = () => {
    setCollapsed((c) => {
      localStorage.setItem("keeper_sider_collapsed", c ? "0" : "1");
      return !c;
    });
  };
  // 纯前端路由：菜单切换与进入对话都走 setState，不整页刷新。
  // 初始仍兼容直接访问 ?agent=<id> 的深链。
  const [route, setRoute] = useState<Route>(() => {
    const agentId = new URLSearchParams(window.location.search).get("agent");
    return agentId ? { page: "chat", agentId } : { page: "overview" };
  });

  const openAgent = (id: string) => setRoute({ page: "chat", agentId: id });
  const exitChat = () => setRoute({ page: "overview" });

  const healthQ = useQuery({
    queryKey: ["health"],
    queryFn: apiHealth,
    refetchInterval: 10000,
  });
  const online = !healthQ.isLoading && !healthQ.isError && healthQ.data?.ok;

  const inChat = route.page === "chat" && !!route.agentId;

  return (
    <Layout style={{ height: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          height: 48,
          lineHeight: "48px",
        }}
      >
        <Space size={10}>
          {/* Logo：主色的细腻渐变，呼应整体淡色 */}
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              background:
                "linear-gradient(135deg, #69b1ff 0%, var(--kp-primary) 100%)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <span style={{ color: "#fff", fontWeight: 600, fontSize: 14 }}>K</span>
          </div>
          <Typography.Title
            level={5}
            style={{ margin: 0, fontWeight: 600, color: "var(--kp-text-strong)" }}
          >
            Keeper
          </Typography.Title>
          {inChat && (
            <Button size="small" onClick={exitChat}>
              ← 返回
            </Button>
          )}
          {online ? (
            <Tag color="green">服务正常</Tag>
          ) : (
            <Tag color="red">服务离线</Tag>
          )}
        </Space>
      </Header>
      <Layout>
        <Sider
          width={168}
          collapsedWidth={64}
          collapsible
          collapsed={collapsed}
          onCollapse={setCollapsed}
          trigger={null}
          theme="light"
          style={{
            borderRight: "1px solid var(--kp-border-soft)",
            position: "relative",
          }}
        >
          <Menu
            mode="inline"
            selectedKeys={inChat ? [] : [route.page]}
            onClick={(e) => setRoute({ page: e.key })}
            items={[
              { key: "overview", icon: <AppstoreOutlined />, label: "概览", title: "概览" },
              { key: "agents", icon: <RobotOutlined />, label: "智能体", title: "智能体" },
              {
                key: "agent-market",
                icon: <ShopOutlined />,
                label: "智能体市场",
                title: "智能体市场",
              },
              { key: "plugins", icon: <ApiOutlined />, label: "插件", title: "插件" },
              { key: "models", icon: <DatabaseOutlined />, label: "模型管理", title: "模型管理" },
              { key: "user-spaces", icon: <UserOutlined />, label: "用户空间", title: "用户空间" },
              { key: "observability", icon: <LineChartOutlined />, label: "可观测", title: "可观测" },
              { key: "settings", icon: <SettingOutlined />, label: "设置", title: "设置" },
            ]}
          />
          <div
            onClick={toggleCollapsed}
            style={{
              position: "absolute",
              bottom: 16,
              left: collapsed ? "50%" : 16,
              transform: collapsed ? "translateX(-50%)" : "none",
              width: collapsed ? 36 : "calc(100% - 32px)",
              height: collapsed ? 36 : 32,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 4,
              cursor: "pointer",
              color: "rgba(0,0,0,0.55)",
              background: "#fff",
              border: "1px solid var(--kp-border-soft)",
              borderRadius: 8,
              boxShadow: "0 1px 4px rgba(0,0,0,0.08)",
              transition: "all 0.2s ease",
              userSelect: "none",
            }}
            title={collapsed ? "展开菜单" : "收起菜单"}
          >
            {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          </div>
        </Sider>
        <Content style={{ overflow: "hidden" }}>
          {route.page === "overview" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <Overview onOpen={openAgent} />
            </div>
          )}
          {route.page === "agents" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <AgentList onCreate={() => setRoute({ page: "agent-create" })} />
            </div>
          )}
          {route.page === "agent-market" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <AgentMarketPage />
            </div>
          )}
          {route.page === "agent-create" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <AgentCreatePage onDone={() => setRoute({ page: "agents" })} />
            </div>
          )}
          {route.page === "plugins" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <PluginPage />
            </div>
          )}
          {route.page === "user-spaces" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <UserSpacePage />
            </div>
          )}
          {route.page === "observability" && (
            <div className="kp-fade-up" style={{ height: "100%" }}>
              <ObservabilityPage />
            </div>
          )}
          {route.page === "models" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <ModelManage />
            </div>
          )}
          {route.page === "settings" && (
            <div
              className="kp-fade-up"
              style={{ height: "100%", overflow: "auto", padding: 32 }}
            >
              <SettingsPage />
            </div>
          )}
          {inChat && <ChatPage agentId={route.agentId!} />}
        </Content>
      </Layout>
    </Layout>
  );
}
