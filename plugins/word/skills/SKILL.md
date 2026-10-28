---
name: word
description: 需要生成 Word 文档（报告、方案、合同、说明书、总结）时使用——按结构描述产出带标题层级、表格、图片、页眉页脚、目录与页码的 .docx；也可回读已有 .docx 核对内容。不用于读 PDF / Excel，也不用于编辑别人文档里的批注与修订
always: false
keywords: [word, docx, 文档, 报告, 合同, 方案, 说明书, 总结, 目录, 页眉, 页脚, 页码, 排版, 生成文档]
tools: [word__create, word__read]
---

# Word 文档生成（.docx）

工具：`word__create`（生成）、`word__read`（回读核对）。都在工作区内生效，路径用相对路径或
`@user/` `@session/` 前缀；写的是磁盘上同一个文件，「我的空间」里下次打开即见新内容。

## 什么时候用

- 用户要一份**能直接发出去的 Word**：报告、方案、合同、说明书、会议纪要、工作总结。
- 用户要**把内容变成排好版的 docx**（标题层级、表格、封面、目录、页码）。
- 要核对刚生成的 docx 内容对不对 → `word__read` 回读。

## 流程

1. **先想清楚结构再动手**：把内容拆成「封面 / 目录 / 一级章节 / 表格 / 附录」，
   一次性把 `blocks` 写全。分多次调用生成会得到多份文件、而不是一份连续的文档。
2. **调用 `word__create`**：`path` 给输出路径（如 `报告/2026年度总结.docx`），
   `doc` 给结构描述。**排版由插件负责**，你只描述内容与层级。
3. **回读核对**：`word__read` 读回来看结构对不对（标题层级、表格内容、有没有漏块）。
4. **告知用户两件事**：文件路径；以及「目录与页码是 Word 域，在 Word 里按 `Ctrl+A` 再按 `F9`
   才会生成/刷新」。

## 结构描述怎么写

`doc.blocks` 是**按顺序渲染**的块数组，块的 `type` 决定写法：

| type | 关键字段 | 说明 |
|---|---|---|
| `heading` | `text` `level`(1-4) | 标题。level=1 是一级章节；`page_break_before:true` 可让它另起一页 |
| `paragraph` | `text` | 正文段落。`bold` `italic` `align` `size` `indent` 可选 |
| `bullets` / `numbers` | `items[]` | 无序 / 有序列表 |
| `table` | `header[]` `rows[][]` | 表格。表头自动加粗带底纹、跨页重复；`widths_cm` 给列宽；`caption` 图题 |
| `image` | `path` `width_cm` | 插图（图片路径也要在工作区内）；`caption` 图题 |
| `code` | `text` | 代码块（等宽灰底） |
| `quote` | `text` | 引用块（缩进 + 左侧竖线） |
| `caption` | `text` | 独立的说明文字 |
| `page_break` | — | 分页 |
| `spacer` | `count` | 空行 |

顶层还有：`title` `subtitle` `cover`（封面页：标题/副标题/作者/日期/单位）、
`page`（`size`: A4/LETTER/A3、`margin_cm`）、`header`（页眉文字）、`footer`（`page_numbers`
页码域）、`toc`（目录：`enabled` `levels` 如 `1-3` `title`）。

一个完整例子：

```json
{
  "path": "方案/产品方案.docx",
  "doc": {
    "cover": { "title": "产品方案", "subtitle": "v1.0", "author": "产品部", "date": "2026-10-28" },
    "toc": { "enabled": true, "levels": "1-2", "title": "目录" },
    "header": { "text": "内部资料 · 请勿外传" },
    "footer": { "page_numbers": true, "prefix": "第 ", "middle": " 页 / 共 " },
    "blocks": [
      { "type": "heading", "level": 1, "text": "一、背景" },
      { "type": "paragraph", "text": "……正文……" },
      { "type": "heading", "level": 2, "text": "1.1 现状" },
      { "type": "bullets", "items": ["要点一", "要点二"] },
      { "type": "table", "header": ["指标", "现状", "目标"], "rows": [["留存率", "31%", "45%"]], "widths_cm": [4, 5, 5], "caption": "表 1 核心指标" }
    ]
  }
}
```

## 禁区

- **只读工作区**下不能写——会被拒绝；先确认工作区可写。
- **不要覆盖用户已有文件**：`path` 指向已存在的文件时，插件会拒绝并报错；确实要覆盖就加
  `"overwrite": true`，并**先提醒用户**。
- **不要用 `word__create` 改已有文档**：它是「从零生成」。改现有 docx 的内容本插件做不到
  （需要保留原样式/图表/批注），那种情况请让用户在 Word/WPS 里改，或说明限制。
- **图片路径必须在工作区内**，且必须是真实存在的图片文件；不存在会报错。
- **目录与页码是 Word 域**：不更新域，目录位置显示的是提示文字而不是条目。提醒用户按
  `Ctrl+A` → `F9`，或别承诺「目录已经填好了」。
- **一次别塞太满**：内容特别长时按章节分块，但要注意多次调用会生成**多个文件**——
  要一份连续文档，就得在一次调用里把 blocks 写全。
- 不支持：批注、修订留痕、公式、图表、SmartArt、文本框、宏（.docm）。需要这些时如实说明，
  不要硬凑。
