import tailwindcssAnimate from "tailwindcss-animate";

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // 自建 UI 后全局基础样式由 theme.css 统一负责（font/heading/scrollbar/focus
  // 都在那里），这里关掉 preflight 避免 Tailwind 再重置一遍。
  corePlugins: {
    preflight: false,
  },
  theme: {
    extend: {
      colors: {
        /* 语义色：全部走 CSS 变量，换主题只改 theme.css 一处。
           brand / accent / ink / surface / line 用 channel 形式
           （--*-rgb: "79 70 229"），这样 bg-brand/10、text-ink/60
           这类透明度修饰符才可用。 */
        app: "rgb(var(--kp-bg-app-rgb) / <alpha-value>)",
        panel: "rgb(var(--kp-bg-rgb) / <alpha-value>)",
        /* 菜单栏是半透明的浅蓝（要透出应用底色），所以走 rgba 变量，
           不能再用 channel 形式——<alpha-value> 会把已有的透明度覆盖掉 */
        sidebar: "var(--kp-sidebar)",
        /* 顶栏走 rgba（玻璃材质要透），不能用 channel 形式 */
        toolbar: "var(--kp-toolbar)",
        surface: {
          DEFAULT: "rgb(var(--kp-surface-rgb) / <alpha-value>)",
          soft: "rgb(var(--kp-surface-soft-rgb) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--kp-border-rgb) / <alpha-value>)",
          soft: "rgb(var(--kp-border-soft-rgb) / <alpha-value>)",
        },
        ink: {
          DEFAULT: "rgb(var(--kp-text-rgb) / <alpha-value>)",
          strong: "rgb(var(--kp-text-strong-rgb) / <alpha-value>)",
          muted: "rgb(var(--kp-text-secondary-rgb) / <alpha-value>)",
          faint: "rgb(var(--kp-text-muted-rgb) / <alpha-value>)",
        },
        brand: {
          DEFAULT: "rgb(var(--kp-primary-rgb) / <alpha-value>)",
          hover: "var(--kp-primary-hover)",
          active: "var(--kp-primary-active)",
          soft: "var(--kp-primary-soft)",
          /* 品牌渐变第二端 */
          accent: "rgb(var(--kp-accent-rgb) / <alpha-value>)",
          /* 主色底上的**深色文字/图标**。主色明度约 60%，白字压上去只有
             3.8:1，深蓝字反而有 4.3:1 —— 所以大片主色作底的地方
             （菜单项 hover、选中标签）统一用它当前景色。 */
          deep: "var(--kp-primary-deep)",
        },
      },
      backgroundImage: {
        /* 全站唯一一条品牌渐变：按钮 / 徽标 / 头像共用，
           保证「主色的光」只有一种，不会各处自渐变出好几种蓝紫。 */
        "brand-gradient": "var(--kp-gradient-brand)",
        "brand-gradient-soft": "var(--kp-gradient-brand-soft)",
      },
      borderRadius: {
        card: "var(--kp-radius-lg)",
        ctrl: "var(--kp-radius)",
        float: "var(--kp-radius-xl)",
        sm: "6px",
      },
      boxShadow: {
        /* 与 theme.css 的 --kp-shadow-* 同源，这里只是给 Tailwind 一个短名字。
           层次用途固定：xs 贴地 / sm 卡片静置 / md 卡片 hover / lg 浮层 / brand 主按钮 */
        "2xs": "var(--kp-shadow-sm)",
        card: "var(--kp-shadow-sm)",
        lift: "var(--kp-shadow-md)",
        pop: "var(--kp-shadow-lg)",
        brand: "var(--kp-shadow-brand)",
        hair: "inset 0 0 0 1px rgb(var(--kp-border-rgb) / 1)",
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          "Arial",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(10px)" },
          to: { opacity: "1", transform: "none" },
        },
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "bubble-in": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "none" },
        },
        "caret-blink": {
          "0%, 45%": { opacity: "1" },
          "55%, 100%": { opacity: "0" },
        },
        "pulse-dot": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.45", transform: "scale(0.82)" },
        },
        "slide-left-in": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(0)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.42s var(--kp-ease-out) both",
        "fade-in": "fade-in 0.4s var(--kp-ease-out) both",
        "bubble-in": "bubble-in 0.3s var(--kp-ease-out) both",
        caret: "caret-blink 1s steps(1, end) infinite",
        "pulse-dot": "pulse-dot 1.4s var(--kp-ease) infinite",
        "slide-left-in": "slide-left-in 0.34s var(--kp-ease-out) both",
      },
      transitionTimingFunction: {
        apple: "var(--kp-ease-out)",
      },
    },
  },
  plugins: [tailwindcssAnimate],
};