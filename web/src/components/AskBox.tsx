import { useState } from "react";
import { Button, Input, Space, Typography } from "antd";

interface Props {
  /** 模型给出的候选，直接点即提交 */
  options?: string[];
  disabled?: boolean;
  onSubmit: (text: string) => void;
}

/** 模型追问时，在该条消息下方渲染的答题区（选项 + 输入框） */
export default function AskBox({ options, disabled, onSubmit }: Props) {
  const [val, setVal] = useState("");

  const submit = (text: string) => {
    const t = text.trim();
    if (!t || disabled) return;
    onSubmit(t);
    setVal("");
  };

  return (
    <div style={{ marginBottom: 16, paddingLeft: 4 }}>
      {!!options?.length && (
        <Space wrap style={{ marginBottom: 8 }}>
          {options.map((o) => (
            <Button
              key={o}
              size="small"
              disabled={disabled}
              onClick={() => submit(o)}
            >
              {o}
            </Button>
          ))}
        </Space>
      )}

      {/* 不用 Space.Compact：贴合无缝会显得挤，改为独立控件 + 间距 */}
      <div
        style={{
          display: "flex",
          gap: 10,
          width: "100%",
          maxWidth: 520,
          marginTop: 4,
        }}
      >
        <Input
          value={val}
          disabled={disabled}
          placeholder="回答上面的问题，Enter 发送"
          onChange={(e) => setVal(e.target.value)}
          onPressEnter={() => submit(val)}
          style={{ flex: 1 }}
        />
        <Button
          type="primary"
          disabled={disabled}
          onClick={() => submit(val)}
          style={{ height: 32, minWidth: 80 }}
        >
          回答
        </Button>
      </div>

      <Typography.Text
        type="secondary"
        style={{ fontSize: 11, display: "block", marginTop: 4 }}
      >
        在这里回答会接上刚才的上下文；用下方输入框则开启新话题
      </Typography.Text>
    </div>
  );
}
