import { memo, useState } from "react";
import { Collapse, Empty, Tag, Typography } from "antd";
import MarkdownView from "./MarkdownView";
import { CaretDownOutlined, CaretRightOutlined } from "@ant-design/icons";
import { RobotOutlined, UserOutlined } from "@ant-design/icons";
import type { ChatMessage } from "../api/types";
import ArtifactCard from "./ArtifactCard";
import { StepUsageTag, UsageTag } from "./UsageTag";

/** 截断成一行预览（去掉换行，避免占好几行） */
function clip(text: string, n: number): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > n ? flat.slice(0, n) + "…" : flat;
}

/**
 * 可折叠的一段（「调用 / 结果」）：**默认收起**，点标题才展开。
 *
 * 几十步的规划过程如果每步都把入参和工具返回全文铺开，整个面板会被几万字符
 * 淹没，真正要看的思考链和用量反而找不到了。收起时给一行预览，足够判断要不要展开。
 */
function FoldBlock({
  label,
  head,
  children,
  defaultOpen = false,
}: {
  label: string;
  /** 收起状态下显示的一行摘要 */
  head?: React.ReactNode;
  children?: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ marginTop: 2 }}>
      <span
        onClick={() => setOpen(!open)}
        style={{
          cursor: "pointer",
          userSelect: "none",
          color: "var(--kp-text-muted)",
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
        }}
        title={open ? "点击收起" : "点击展开"}
      >
        {open ? <CaretDownOutlined /> : <CaretRightOutlined />}
        <Typography.Text type="secondary">{label}：</Typography.Text>
        {!open && head}
      </span>
      {open && <div>{children}</div>}
    </div>
  );
}

/** 时间戳：今天显示 HH:mm，更早显示 MM-DD HH:mm */
function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

/**
 * 单条消息气泡。
 *
 * 包了 memo：ChatPage 的输入框是页面级 state，**每敲一个字都会重渲染整个页面**。
 * 回调因此设计成「接收 messageId」而不是「闭包捕获 id」——后者每次渲染都是新
 * 函数，会让 memo 直接失效（这是给组件加 memo 最常见的坑）。
 */
function ChatBubble({
  msg,
  agentId,
  isLocal = true,
  onUsageDetail,
  onUsageDump,
}: {
  msg: ChatMessage;
  agentId: string;
  /** 是否本机部署（决定产物卡片能否「打开文件夹」）；未传时按本机处理 */
  isLocal?: boolean;
  /** 点用量里的「查看详情」：打开本轮逐步的时间线可视化（传 messageId 而非闭包） */
  onUsageDetail?: (messageId: string) => void;
  /** 点用量里的「查看 dump」：打开该轮完整 prompt（仅落盘开启时可见） */
  onUsageDump?: (messageId: string) => void;
}) {
  const isUser = msg.role === "user";
  const timeText = msg.created_at ? formatTime(msg.created_at) : "";

  return (
    <div
      className="kp-bubble-in"
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        alignItems: "flex-end",
        gap: 8,
        marginBottom: 18,
      }}
    >
      {/* 助手头像（左） */}
      {!isUser && (
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: "50%",
            background: "var(--kp-surface)",
            color: "var(--kp-text)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 14,
            flex: "0 0 auto",
          }}
        >
          <RobotOutlined />
        </div>
      )}

      <div
        style={{
          maxWidth: "68%",
          display: "flex",
          flexDirection: "column",
          alignItems: isUser ? "flex-end" : "flex-start",
        }}
      >
      <div
        style={{
          // 用户气泡：极淡的 Apple 蓝（var(--kp-primary) 的浅底），助手保持 Apple 浅灰
          background: isUser ? "var(--kp-primary-soft)" : "var(--kp-surface)",
          color: "var(--kp-text)",
          border: isUser ? "1px solid #cfe0f7" : "none",
          borderRadius: 18,
          padding: "12px 16px",
          boxShadow: "0 1px 2px rgba(0, 0, 0, 0.03)",
        }}
      >
        {msg.streaming && !msg.text ? (
          // 流式还没产出正文：给进度反馈（有步骤就说执行到第几步），避免看起来卡住
          <span style={{ color: "var(--kp-text-muted)" }}>
            {msg.steps?.length
              ? `已执行 ${msg.steps.length} 步，正在整理回答…`
              : "正在思考…"}
            <span className="kp-caret" />
          </span>
        ) : (
          <>
            {/* 助手回复基本都是 Markdown（标题 / 列表 / 表格 / 代码块）→ 渲染；
                用户消息保持纯文本，避免 a_b 变斜体、#1 变标题这类误伤 */}
            {isUser ? (
              <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {msg.text}
              </div>
            ) : (
              <div>
                <MarkdownView
                  text={msg.text}
                  mode={msg.streaming ? "streaming" : "static"}
                />
                {msg.streaming && <span className="kp-caret" />}
              </div>
            )}
          </>
        )}

        {/* 被用户叫停：说明内容是「半截」的，避免看起来像回答不完整 */}
        {msg.canceled && !msg.streaming && (
          <div
            style={{
              marginTop: 6,
              fontSize: 12,
              color: "var(--kp-text-muted)",
            }}
          >
            已停止生成
          </div>
        )}

        {msg.role === "assistant" && msg.llm && (
          <Tag color="purple" style={{ marginTop: 6 }}>
            AI 生成
          </Tag>
        )}

        {msg.role === "assistant" && !!msg.steps?.length && (
          <Collapse
            ghost
            size="small"
            // 始终收起：过程细节由用户主动点开再看，正文才是主角。
            // 流式期间的进度靠标题里的「N 步」实时递增 + 正文光标反馈，不再自动展开。
            defaultActiveKey={[]}
            style={{
              marginTop: 8,
              background: "#fff",
              borderRadius: 12,
              border: "1px solid var(--kp-border-soft)",
            }}
            items={[
              {
                key: "steps",
                label: (
                  <span style={{ fontSize: 13 }}>
                    <Tag color="geekblue" style={{ marginRight: 6 }}>
                      Agent
                    </Tag>
                    自主规划过程（{msg.steps.length} 步）
                    {/* 收起状态下也要能看出过程还在推进 */}
                    {msg.streaming && (
                      <span style={{ color: "var(--kp-text-muted)" }}>
                        {" "}
                        · 进行中
                      </span>
                    )}
                  </span>
                ),
                children: (
                  <div style={{ fontSize: 12.5, lineHeight: 1.7 }}>
                    {msg.steps.map((s, i) => {
                      // 流式进行中：最后一步的结果默认展开，方便看到最新进展
                      const streamingLast =
                        !!msg.streaming && i === msg.steps.length - 1;
                      return (
                      <div
                        key={i}
                        style={{
                          borderLeft: "2px solid var(--kp-border)",
                          paddingLeft: 8,
                          marginBottom: 8,
                        }}
                        data-streaming-last={streamingLast ? "1" : undefined}
                      >
                        {s.thought && (
                          <div>
                            <Typography.Text type="secondary">思考：</Typography.Text>
                            {s.thought}
                          </div>
                        )}
                        {/* 调用与结果的正文都默认收起：几十步的过程如果全展开，
                            一眼望不到头，真正要看的（思考链 + 用量）反而被淹没了。 */}
                        {s.tool && (
                          <FoldBlock
                            label="调用"
                            head={
                              <>
                                <Tag color="blue" style={{ margin: "0 4px" }}>
                                  {s.tool}
                                </Tag>
                                {s.args && Object.keys(s.args).length > 0 && (
                                  <Typography.Text type="secondary">
                                    {clip(JSON.stringify(s.args), 80)}
                                  </Typography.Text>
                                )}
                              </>
                            }
                          >
                            {s.args && Object.keys(s.args).length > 0 && (
                              <pre
                                style={{
                                  whiteSpace: "pre-wrap",
                                  wordBreak: "break-word",
                                  maxHeight: 240,
                                  overflow: "auto",
                                  background: "#fff",
                                  border: "1px solid var(--kp-border-soft)",
                                  borderRadius: 8,
                                  padding: "4px 8px",
                                  margin: "2px 0 0",
                                  fontSize: 12,
                                }}
                              >
                                {JSON.stringify(s.args, null, 2)}
                              </pre>
                            )}
                          </FoldBlock>
                        )}
                        {s.observation && (
                          <FoldBlock
                            label={
                              s.kind === "ask_human"
                                ? "提问"
                                : s.kind === "human_answer"
                                ? "用户补充"
                                : "结果"
                            }
                            head={
                              <Typography.Text type="secondary">
                                {clip(s.observation, 100)}
                                {s.observation.length > 100
                                  ? ` …（共 ${s.observation.length} 字符）`
                                  : ""}
                              </Typography.Text>
                            }
                            defaultOpen={streamingLast}
                          >
                            <div
                              style={{
                                whiteSpace: "pre-wrap",
                                wordBreak: "break-word",
                                maxHeight: 320,
                                overflow: "auto",
                                background: "#fff",
                                border: "1px solid var(--kp-border-soft)",
                                borderRadius: 8,
                                padding: "4px 8px",
                                marginTop: 2,
                              }}
                            >
                              {s.observation}
                            </div>
                          </FoldBlock>
                        )}
                        {s.usage && (
                          <div style={{ marginTop: 2 }}>
                            <StepUsageTag usage={s.usage} />
                          </div>
                        )}
                      </div>
                      );
                    })}
                  </div>
                ),
              },
            ]}
          />
        )}

        {/* 本轮用量：**只在助手消息下渲染一次**。
            后端按「本轮锚点」聚合用量，而锚点就是触发这轮的用户消息 id，所以
            user 消息上也会带回一份**完全相同**的用量——再渲染一遍就是重复，
            而且容易让人以为这是「提问本身的开销」。助手消息里有就够了。 */}
        {msg.role === "assistant" && msg.usage && (
          <div style={{ marginTop: 6, textAlign: "left" }}>
            <UsageTag
              usage={msg.usage}
              wallMs={msg.duration_ms}
              onDetail={
                onUsageDetail ? () => onUsageDetail(msg.id) : undefined
              }
              onDump={onUsageDump ? () => onUsageDump(msg.id) : undefined}
            />
          </div>
        )}

        {msg.role === "assistant" && !!msg.artifacts?.length && (
          <div style={{ marginTop: 8 }}>
            {msg.artifacts.map((a) => (
              <ArtifactCard
                key={a.id}
                agentId={agentId}
                messageId={msg.id}
                artifact={a}
                isLocal={isLocal}
              />
            ))}
          </div>
        )}

        {msg.error && (
          <Typography.Text type="danger" style={{ display: "block", marginTop: 8 }}>
            {msg.error}
          </Typography.Text>
        )}

        {/* 只在「非流式且确实什么都没有」时才提示暂无回答：
            流式期间上面已经有进度反馈，这里再出现就会重复显示两种空态 */}
        {msg.role === "assistant" &&
          !msg.streaming &&
          !msg.text &&
          !msg.error &&
          !msg.steps?.length && (
            <div style={{ marginTop: 8 }}>
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无回答"
                style={{ margin: 0 }}
              />
            </div>
          )}
      </div>

        {/* 时间戳 */}
        {timeText && (
          <span
            style={{
              marginTop: 4,
              padding: "0 4px",
              fontSize: 11,
              color: "var(--kp-text-muted)",
            }}
          >
            {timeText}
          </span>
        )}
      </div>

      {/* 用户头像（右） */}
      {isUser && (
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: "50%",
            background: "#dceafb",
            color: "var(--kp-primary)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 14,
            flex: "0 0 auto",
          }}
        >
          <UserOutlined />
        </div>
      )}
    </div>
  );
}

// props 里的 msg 是**对象**，所以它能不能挡住重渲染取决于引用是否稳定——
// 流式更新走 `prev.map(m => m.id === id ? {...m, ...patch} : m)`，未变化的消息
// 保持原引用，因此历史消息在输入时会被 memo 挡住。
export default memo(ChatBubble);
