"""与可视化 / 多模态交付相关的后端代码统一放这里。

核心概念是「块（Block）」：agent 用 fs.deliver([...]) 交付带类型的块，
文件只是块的一种，图表 / 表格 / 地图 / 流程图是另外的 kind。
前端按 kind 选渲染器（web/src/components/blocks.ts）。
"""

from .block import (
    BLOCK_KINDS,
    capability_bullets,
    block_file_ref,
    default_render_hint,
    normalize_block,
    parse_blocks,
    validate_chart_spec,
)

__all__ = [
    "BLOCK_KINDS",
    "capability_bullets",
    "block_file_ref",
    "default_render_hint",
    "normalize_block",
    "parse_blocks",
    "validate_chart_spec",
]
