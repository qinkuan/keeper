---
name: excel
description: 需要读取或编辑 Excel(.xlsx) 时使用——读表、写单元格/区域/公式、追加行、增删改工作表；不用于 PDF 或其它文件类型
always: false
keywords: [excel, xlsx, 表格, 单元格, 公式, 工作表, 汇总, 求和, 加一行]
tools: [excel__edit]
---

# Excel 编辑（.xlsx）

工具 `excel__edit` 只在工作区内生效，路径用相对路径或 `@user/` `@session/` 前缀；改的是
磁盘上同一个文件，「我的空间」表格预览下次打开即见新内容。

参数：`path`(必填) `op`(必填) `sheet` `range` `values` `cell` `value` `formula` `new_name`。

- `read`：读整表或某区域。**改之前先 read**，确认表名、列位、现有数据。
  - `{path, op:"read"}` 读整表；加 `range:"A1:C10"` 读区域；加 `sheet:"Sheet2"` 指定表。
  - 返回制表符分隔文本，第一行常是表头。
- `write`：区域批量 `{path, op:"write", range:"A1:C2", values:[[...],[...]]}`；
  单格 `{path, op:"write", cell:"B2", value:123}`；
  公式 `{path, op:"write", cell:"C1", formula:"=SUM(B2:B100)"}`。
- `append`：末尾追加一行 `{path, op:"append", values:["名称", 1, 2]}`。
- `add_sheet` / `delete_sheet` / `rename_sheet`：增/删/改工作表（`sheet` 为目标，
  `rename_sheet` 另给 `new_name`）。

流程示例：用户说"把销量表的合计加到 C 列" → 先 `read` 看结构 → `write` 公式到 C 列各单元格
→ 告知已改。批量写优先用 `range+values`，少调用几次。

## 禁区

- 只读工作区下不要尝试写——会被拒绝；先只做 `read`。
- 不要凭猜写单元格：涉及"第几列/哪个表"时先 `read` 确认。
- openpyxl 保存对图表/图片等少数特性可能丢失；用户文件若含图表，改前提醒这一风险，或只 read。
- `path` 只给工作区内文件；别用 `..` 或工作区外绝对路径（会被拦）。
