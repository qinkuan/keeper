import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider } from "antd";
import type { ThemeConfig } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import "./theme.css";
import "./monaco";
import App from "./App";

/**
 * 简约工作向主题：主色沿用 emerald，其余保持中性低饱和。
 * 控件更紧凑、圆角收敛、阴影弱化，避免装饰性视觉干扰日常工作。
 */
const theme: ThemeConfig = {
  token: {
    /* 按钮 / 强调：比展示类更深的蓝，突出可操作与重点 */
    colorPrimary: "#1677ff",
    colorPrimaryHover: "#4096ff",
    colorPrimaryActive: "#0958d9",
    colorInfo: "#1677ff",
    colorLink: "#1677ff",
    /* 语义色也走柔和路线，不用高饱和的系统色 */
    colorSuccess: "#3fa46a",
    colorWarning: "#e0a33a",
    colorError: "#e0685f",
    colorTextBase: "#2b2f36",
    colorBgContainer: "#ffffff",
    colorBorder: "#e2e5ea",
    colorBorderSecondary: "#edeff3",
    borderRadius: 12,
    fontSize: 14,
    lineHeight: 1.5,
    controlHeight: 32,
    boxShadow: "0 8px 24px rgba(0, 0, 0, 0.08)",
    /* Apple 缓动与节奏 */
    motionEaseOut: "cubic-bezier(0.25, 0.1, 0.25, 1)",
    motionEaseInOut: "cubic-bezier(0.25, 0.1, 0.25, 1)",
    motionDurationMid: "0.3s",
    motionDurationSlow: "0.45s",
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif',
  },
  components: {
    Layout: {
      headerBg: "#ffffff",
      bodyBg: "#f7f8fa",
      siderBg: "#ffffff",
      headerPadding: "0 24px",
    },
    Menu: {
      itemSelectedBg: "var(--kp-primary-soft)",
      itemSelectedColor: "#1677ff",
      itemHoverBg: "#f5f7fa",
      itemActiveBg: "var(--kp-primary-soft)",
      itemBorderRadius: 10,
      itemHeight: 38,
      itemMarginInline: 8,
    },
    Button: {
      primaryShadow: "0 2px 8px rgba(22, 119, 255, 0.26)",
      defaultShadow: "0 1px 2px rgba(31, 45, 61, 0.04)",
      dangerShadow: "0 2px 8px rgba(224, 104, 95, 0.18)",
      fontWeight: 500,
    },
    Card: {
      borderRadiusLG: 18,
      boxShadow: "0 8px 24px rgba(31, 45, 61, 0.06)",
    },
    Select: {
      optionSelectedBg: "var(--kp-primary-soft)",
      optionSelectedColor: "#1677ff",
    },
    Input: {
      activeShadow: "0 0 0 4px rgba(22, 119, 255, 0.16)",
    },
    Tag: {
      /* 标签用小圆角：胶囊太圆，小尺寸下显胖 */
      borderRadiusSM: 6,
      defaultBg: "#f2f5f9",
      defaultColor: "#5b6472",
    },
    Table: {
      headerBg: "#f7f8fa",
      headerColor: "#7a8290",
      rowHoverBg: "#f7f8fa",
      borderColor: "#edeff3",
    },
  },
};

const qc = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider theme={theme} locale={zhCN}>
      <QueryClientProvider client={qc}>
        <App />
      </QueryClientProvider>
    </ConfigProvider>
  </React.StrictMode>
);
