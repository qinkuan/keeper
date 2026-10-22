#!/usr/bin/env python3
"""excel 插件的可执行文件：读取 / 编辑 .xlsx（keeper 的 bin 组件协议）。

协议：stdin 一行 JSON {"tool":"edit","args":{...},"context":{"workspace","read_only","agent_space"}}；
stdout 最后一行 JSON {"ok":true,"result":...} 或 {"ok":false,"error":...}。
业务级问题（越界/只读/缺参/文件不存在）走 ok:true + result；意外异常才 ok:false。

依赖 openpyxl，装在 keeper 的 .venv；子进程由 keeper 以 venv 的 PATH 拉起，
`#!/usr/bin/env python3` 即解析到带依赖的解释器。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_ROWS = 200
MAX_COLS = 50


class Biz(Exception):
    """业务级问题：作为正常 result 回给模型，不当成崩溃。"""


def _resolve(ctx: dict, rel: str) -> Path:
    ws = (ctx.get("workspace") or "").strip()
    ag = (ctx.get("agent_space") or "").strip()
    s = (rel or "").strip()
    if s == "@tmp" or s.startswith("@tmp/") or s.startswith("@session/"):
        base = ag
        if s.startswith("@session/"):
            s = s[len("@session/"):]
        elif s.startswith("@tmp/"):
            s = s[len("@tmp/"):]
        label = "会话工作区"
    else:
        if s.startswith("@user/"):
            s = s[len("@user/"):]
        base = ws
        label = "用户工作区"
    if not base:
        raise Biz(f"{label}不可用（没有工作区上下文）")
    b = Path(base).resolve()
    s = s.lstrip("/")
    if not s:
        raise Biz("空路径")
    p = Path(s)
    target = p.resolve() if p.is_absolute() else (b / s).resolve()
    if not (target == b or b in target.parents):
        raise Biz(f"路径越界（不在{label}内）：{rel}")
    return target


def _cells_to_text(rows) -> str:
    out = []
    for r in rows[:MAX_ROWS]:
        out.append("\t".join("" if c is None else str(c) for c in r[:MAX_COLS]))
    text = "\n".join(out)
    if len(rows) > MAX_ROWS:
        text += "\n…（行数过多已截断）"
    return text


def handle(args: dict, ctx: dict) -> str:
    try:
        import openpyxl
        from openpyxl.utils import range_boundaries
    except Exception as e:  # noqa: BLE001
        raise Biz(f"未安装 openpyxl：{e}")

    rel = (args.get("path") or "").strip()
    op = (args.get("op") or "").strip().lower()
    if not rel:
        raise Biz("[缺少 path] 请给出工作区内的 .xlsx 文件路径")
    if not op:
        raise Biz("[缺少 op] op：read / write / append / add_sheet / delete_sheet / rename_sheet")
    p = _resolve(ctx, rel)
    ro = bool(ctx.get("read_only"))

    if op == "read":
        if not p.exists():
            raise Biz(f"[文件不存在] {rel}")
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        names = wb.sheetnames
        sheet = args.get("sheet") or (names[0] if names else None)
        if not sheet or sheet not in names:
            wb.close()
            raise Biz(f"[工作表不存在] 可选：{names}")
        ws = wb[sheet]
        rng = args.get("range")
        if rng:
            # 只读模式下 ws[range] 返回的是 ReadOnlyCell 占位对象、取不到值；改用
            # iter_rows(values_only=True) 按区域边界流式读，拿到的就是真实值。
            try:
                min_col, min_row, max_col, max_row = range_boundaries(str(rng))
            except Exception:
                wb.close()
                raise Biz(f"[range 无效] {rng}")
            rows = [
                list(r)
                for r in ws.iter_rows(
                    min_row=min_row,
                    max_row=max_row,
                    min_col=min_col,
                    max_col=max_col,
                    values_only=True,
                )
            ]
        else:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        return f"文件 {rel}｜工作表：{names}｜当前「{sheet}」\n{_cells_to_text(rows)}"

    if ro:
        raise Biz("[只读工作区] 当前会话绑定的工作区是只读的，不能修改文件")

    sheet = args.get("sheet")

    if op == "add_sheet":
        new_name = (sheet or args.get("new_name") or "").strip()
        if not new_name:
            raise Biz("[缺少 sheet] add_sheet 需要新表名")
        wb = openpyxl.load_workbook(p) if p.exists() else openpyxl.Workbook()
        if new_name in wb.sheetnames:
            wb.close()
            raise Biz(f"[已存在] 工作表「{new_name}」")
        wb.create_sheet(new_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        wb.save(p)
        wb.close()
        return f"已新建工作表「{new_name}」→ {rel}"

    if op == "delete_sheet":
        if not p.exists():
            raise Biz(f"[文件不存在] {rel}")
        wb = openpyxl.load_workbook(p)
        target = sheet or (wb.sheetnames[0] if wb.sheetnames else None)
        if not target or target not in wb.sheetnames:
            wb.close()
            raise Biz(f"[工作表不存在] 可选：{wb.sheetnames}")
        del wb[target]
        wb.save(p)
        wb.close()
        return f"已删除工作表「{target}」→ {rel}"

    if op == "rename_sheet":
        if not p.exists():
            raise Biz(f"[文件不存在] {rel}")
        new_name = (args.get("new_name") or "").strip()
        wb = openpyxl.load_workbook(p)
        target = sheet or (wb.sheetnames[0] if wb.sheetnames else None)
        if not target or target not in wb.sheetnames:
            wb.close()
            raise Biz(f"[工作表不存在] 可选：{wb.sheetnames}")
        if not new_name:
            wb.close()
            raise Biz("[缺少 new_name] rename_sheet 需要新表名")
        wb[target].title = new_name
        wb.save(p)
        wb.close()
        return f"已把「{target}」改名为「{new_name}」→ {rel}"

    wb = openpyxl.load_workbook(p) if p.exists() else openpyxl.Workbook()
    if not wb.sheetnames:
        wb.create_sheet("Sheet1")
    ws = wb[sheet] if (sheet and sheet in wb.sheetnames) else wb[wb.sheetnames[0]]

    if op == "append":
        values = args.get("values")
        if not isinstance(values, (list, tuple)):
            wb.close()
            raise Biz("[缺少 values] append 需要一维数组，如 [\"甲\", 1, 2]")
        ws.append(list(values))
        p.parent.mkdir(parents=True, exist_ok=True)
        wb.save(p)
        n = ws.max_row
        wb.close()
        return f"已追加一行到「{ws.title}」（现共 {n} 行）→ {rel}"

    if op == "write":
        rng = args.get("range")
        values = args.get("values")
        cell = args.get("cell")
        value = args.get("value")
        formula = args.get("formula")
        if formula:
            target = cell or rng
            if not target:
                wb.close()
                raise Biz("[缺少 cell/range] 写公式需要目标单元格")
            ws[target] = formula
            p.parent.mkdir(parents=True, exist_ok=True)
            wb.save(p)
            wb.close()
            return f"已在 {target} 写入公式 {formula} → {rel}"
        if rng and values is not None:
            if values and isinstance(values[0], (list, tuple)):
                ws[rng] = [list(r) for r in values]
            else:
                ws[rng] = list(values) if isinstance(values, (list, tuple)) else values
            p.parent.mkdir(parents=True, exist_ok=True)
            wb.save(p)
            wb.close()
            return f"已写入区域 {rng} → {rel}"
        if cell and value is not None:
            ws[cell] = value
            p.parent.mkdir(parents=True, exist_ok=True)
            wb.save(p)
            wb.close()
            return f"已在 {cell} 写入值 → {rel}"
        wb.close()
        raise Biz("[参数不足] write 需要 (range+values) 或 (cell+value) 或 (cell+formula)")

    wb.close()
    raise Biz(f"[未知 op] {op}")


def main() -> None:
    line = sys.stdin.readline()
    try:
        req = json.loads(line)
    except Exception as e:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"stdin 不是合法 JSON：{e}"}) + "\n")
        return
    tool = req.get("tool")
    args = req.get("args") or {}
    ctx = req.get("context") or {}
    if tool != "edit":
        sys.stdout.write(json.dumps({"ok": False, "error": f"未知工具 {tool}（本插件只提供 edit）"}) + "\n")
        return
    try:
        result = handle(args, ctx)
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n")
    except Biz as e:
        sys.stdout.write(json.dumps({"ok": True, "result": str(e)}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}) + "\n")


if __name__ == "__main__":
    main()
