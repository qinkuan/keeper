---
name: pdf
description: 需要读取或编辑 PDF 时使用——抽取文字、合并、拆分取页、旋转、删页；不用于 Excel 或其它文件类型
always: false
keywords: [pdf, 抽取, 文字, 合并, 拆分, 取页, 旋转, 删页, extract]
tools: [pdf__edit]
---

# PDF 编辑

工具 `pdf__edit` 只在工作区内生效，路径用相对路径或 `@user/` `@session/` 前缀；破坏性操作
默认另存新文件，「我的空间」预览下次打开即见。

参数：`path`(必填) `op`(必填) `inputs` `pages` `angle` `output` `max_chars`。

- `info`：页数 / 标题。
- `extract_text`：抽文字，`pages:"1,3,5-7"` 可选；扫描件/图片型 PDF 会抽不到（如实说明，别编）。
- `merge`：`{path:"a.pdf", op:"merge", inputs:["b.pdf","c.pdf"]}` → 合成 `a_merged.pdf`。
- `split`：`{path, op:"split", pages:"2-4"}` → 抽出这些页成新文件。
- `rotate`：`{path, op:"rotate", pages:"1", angle:90}`。
- `delete_pages`：`{path, op:"delete_pages", pages:"3"}`。

`merge/split/rotate/delete` **默认另存为带后缀的新文件，不覆盖原文件**；要指定名字或覆盖
就传 `output`（同样锁在工作区内）。页码都是 1-based。

## 禁区

- 只读工作区下不要尝试写——会被拒绝；先只做 `info` / `extract_text`。
- 扫描件抽不到文字时，如实告知"这是图片型 PDF，抽不出文字"，不要臆造内容。
- `path` 只给工作区内文件；别用 `..` 或工作区外绝对路径（会被拦）。
