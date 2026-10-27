"""表说明接口：把「每张表是干什么用的」暴露出去。

SQLite 不支持 ``COMMENT ON TABLE``，所以说明存在 ``schema_doc`` 表里
（见 ``keeper/store/schema_doc.py``）。这个接口把那份说明读出来，让前端 /
桌面端 / 任何客户端都能查——不必去翻代码或文档。

只读，无副作用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from .schema_doc import list_schema_docs

router = APIRouter(prefix="/schema", tags=["schema"])


@router.get("/tables")
async def schema_tables(
    db: Optional[str] = Query(
        None, description="只返回某个库的说明：keeper（主库）/ search（检索索引库）；不传=两个都返回"
    ),
) -> Dict[str, Any]:
    """列出所有表的说明：干什么用的、谁写、谁读、数据生命周期、注意事项。"""
    docs: List[Dict[str, Any]] = await list_schema_docs(db)
    return {"tables": docs, "count": len(docs)}
