"""Word(.docx) 只读渲染：docx → IR → 页面。

对外只暴露两个东西：
- ``extract_docx_ir(data)``：纯函数，字节 → IR
- ``docx_router``：HTTP 端点（``GET /documents/docx?path=...``）

为什么要单独一个包而不是塞进 zonefilesystem：zonefilesystem 管的是**受空间边界
约束的文件读写**；这里是**文件内容的语义解析**（把 docx 变成可渲染的树）。
两件事的边界不同：前者关心"能不能碰这个路径"，后者关心"这个文件长什么样"。
"""
from .extract import extract_docx_ir
from .ir import Document, to_json

__all__ = ["extract_docx_ir", "Document", "to_json"]
