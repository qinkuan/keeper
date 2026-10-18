"""附件上传链路回归：`python keeper/tests/test_attachment_upload.py`。

覆盖「选文件 → 上传」这一段**不需要真连厂商**的部分：

1. 类型 / 大小：本地就该拦住的，别浪费一次上传；
2. ``supports``：把不支持的情况说清楚（人话，不是干巴巴的 False）；
3. 上传本身：参数组装（purpose / base_url / key）、返回值映射、坏输入在
   **上传前**挡下、``${ENV_VAR}`` 形式的 key 要展开。

厂商侧用替身换掉 ``AsyncOpenAI.files.create``：真的打一次 DeepSeek 要花用户的
额度，而这里要验的是「**我们怎么调**」，不是「厂商怎么答」。

**隔离**：HOME 是临时的，绝不碰 ``~/.keeper``。
"""
from __future__ import annotations

import asyncio
import os
import struct
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="keeper-attach-"))
os.environ["HOME"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from keeper.llm import files as F  # noqa: E402
from keeper.llm.config import LLMConfig  # noqa: E402

FAILED: list = []


def check(ok: bool, label: str, got=None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  got={got!r}"))
    if not ok:
        FAILED.append(label)


def _png(size: int = 200) -> bytes:
    """最小合法 PNG 头 + 填充。不需要是真图，magic bytes 对就行。"""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * size


# ── 1. 类型判定：按内容，不按扩展名 ────────────────────────────────────────
def part_sniff() -> None:
    cases = [
        (_png(), "image/png"),
        (b"\xff\xd8\xff\xe0" + b"x" * 50, "image/jpeg"),
        (b"GIF89a" + b"x" * 50, "image/gif"),
        (b"RIFF" + struct.pack("<I", 100) + b"WEBP" + b"x" * 50, "image/webp"),
        # 伪装成图片的：内容骗不过 magic bytes，扩展名可以
        (b"MZ\x90\x00" + b"x" * 50, None),
        (b"PDF-1.4\n..." + b"x" * 50, None),
        (b"", None),
    ]
    for data, want in cases:
        got = F.sniff_image_mime(data)
        check(got == want, f"类型判定 → {want or '拒识'}", got)


# ── 2. supports：不支持时要说清楚为什么 ───────────────────────────────────
def part_supports() -> None:
    up = F.uploader_for("deepseek")
    check(up is not None, "deepseek 有上传器")

    ok, why = up.supports(
        LLMConfig(model_name="deepseek-flash", provider="deepseek", api_key="sk-x")
    )
    check(ok and not why, "deepseek-flash 通过", why)

    cases = [
        ("deepseek-chat", "deepseek", None, "非 flash 模型被拒"),
        ("gpt-4o", "openai", None, "别的 provider 被拒"),
        ("deepseek-flash", "deepseek", "https://api.deepseek.com/anthropic", "Anthropic 兼容端点被拒"),
    ]
    for model, provider, base, label in cases:
        ok, why = up.supports(
            LLMConfig(model_name=model, provider=provider, api_key="sk-x", base_url=base)
        )
        check((not ok) and bool(why), f"{label}，且给出原因", why)

    # 没实现的 provider：返回 None 而不是抛异常——那是预期的用户场景
    for p in ("openai", "ollama", "siliconflow", "", "SILICONFLOW"):
        check(F.uploader_for(p) is None, f"uploader_for({p!r}) → None")


# ── 3. 上传：参数、返回值、坏输入、环境变量展开 ───────────────────────────
async def part_upload() -> None:
    up = F.uploader_for("deepseek")
    cfg = LLMConfig(
        model_name="deepseek-flash", provider="deepseek",
        api_key="sk-test", base_url="https://api.deepseek.com",
    )
    seen: dict = {}

    class _Files:
        async def create(self, **kw):
            # 只覆盖本次调用的参数，别 clear：构造参数（init）要留着一起断言
            for k in list(seen):
                if k != "init":
                    del seen[k]
            seen.update(kw)
            return type("F", (), {
                "id": "file-api-abc123",
                "filename": "photo.png",
                "bytes": len(kw["file"][1]),
                "object": "file",
                "expires_at": None,
            })()

    class _Client:
        def __init__(self, **kw):
            seen["init"] = kw
            self.files = _Files()

    import keeper.llm.files as mod
    mod.AsyncOpenAI = _Client  # type: ignore[assignment]
    try:
        # ① 参数组装
        res = await up.upload(cfg, _png(300), "photo.png")
        check(seen.get("purpose") == "user_data", "上传带 purpose=user_data", seen.get("purpose"))
        check(seen["init"].get("base_url") == "https://api.deepseek.com", "base_url 透传", seen["init"])
        check(seen["init"].get("api_key") == "sk-test", "api_key 透传", seen["init"])
        check("expires_after" not in seen, "不设 TTL（永久有效）", sorted(k for k in seen if k != "init"))

        # ② 返回值映射
        check(res.file_id == "file-api-abc123", "file_id 取自响应 id", res.file_id)
        check(res.name == "photo.png", "文件名取自响应", res.name)
        check(res.mime == "image/png" and res.size == 308, "mime / size 映射正确", (res.mime, res.size))
        check(res.provider == "deepseek" and res.model == "deepseek-flash", "provider / model 记录在案")
        check(res.expires_at is None, "未设 TTL 则 expires_at 为 None", res.expires_at)

        # ③ 坏输入在**上传前**挡下（根本不打 SDK）
        seen.clear()
        for data, label in (
            (b"MZ\x90\x00" + b"x" * 20, "非图片"),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * (F.DEEPSEEK_MAX_BYTES + 5), "超限"),
        ):
            try:
                await up.upload(cfg, data, "x.png")
                check(False, f"{label} 应被拒绝")
            except ValueError as e:
                check(bool(str(e)), f"{label} 被本地拒绝（有人话）", str(e))
        check(not seen, "坏输入没有打到 SDK", sorted(seen))

        # ④ api_key 缺失
        try:
            await up.upload(LLMConfig(model_name="deepseek-flash", provider="deepseek"), _png(), "x.png")
            check(False, "缺 api_key 应被拒绝")
        except ValueError as e:
            check("key" in str(e).lower(), "缺 api_key 有人话提示", str(e))

        # ⑤ ${ENV_VAR} 形式的 key 必须展开——llm_profiles 里可以这么写
        os.environ["KEEPER_TEST_DS_KEY"] = "sk-from-env"
        seen.clear()
        await up.upload(
            LLMConfig(model_name="deepseek-flash", provider="deepseek",
                      api_key="${KEEPER_TEST_DS_KEY}", base_url="https://api.deepseek.com"),
            _png(10), "e.png",
        )
        check(seen["init"].get("api_key") == "sk-from-env",
              "${ENV_VAR} 形式的 api_key 被展开（不是当字面量发出去）", seen["init"])

        # ⑥ base_url 为空时用 DeepSeek 默认地址
        seen.clear()
        await up.upload(LLMConfig(model_name="deepseek-flash", provider="deepseek",
                                  api_key="sk-x", base_url=None), _png(10), "d.png")
        check(seen["init"].get("base_url") == F.DEEPSEEK_DEFAULT_BASE_URL,
              "base_url 为空时落到 DeepSeek 默认端点", seen["init"])

        # ⑦ 引用格式：DeepSeek 是**扁平**的（OpenAI 是嵌套的）
        check(up.ref_part("file-api-xyz") == {"type": "file", "file_id": "file-api-xyz"},
              "ref_part 是 DeepSeek 的扁平格式", up.ref_part("file-api-xyz"))
    finally:
        mod.AsyncOpenAI = real if (real := getattr(mod, "AsyncOpenAI", None)) else None


async def main() -> None:
    part_sniff()
    part_supports()
    await part_upload()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
