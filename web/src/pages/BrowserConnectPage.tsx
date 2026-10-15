import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, Copy, RefreshCw } from "lucide-react";
import { apiAgentBridgeInfo } from "../api/client";
import { Hint, PageHeader, Section, Stack } from "../components/ui/Page";
import { toast } from "../components/ui/Toast";

/**
 * 「浏览器」页：把浏览器扩展接到本内核。
 *
 * 这一页存在的原因
 * ----------------
 * 内核原本是靠 Playwright 的 ``ctx.service_workers`` 找到扩展的 service worker，
 * 往它的 ``chrome.storage.local`` 里写内核地址和 token（见
 * ``browser/agent_browser.py::_ext_sync_token``）。那只在扩展跑在 **keeper 自带的
 * 那个 Chromium** 里时成立——那是同一个进程，内核伸手就够得着。
 *
 * 扩展装到用户自己的 Chrome 之后就不行了：另一个进程、另一个 profile，内核的
 * Playwright 连它的 service worker 都列不出来。于是变成一个死结：
 *
 *     扩展要连内核 → 需要 token
 *     要拿到 token → 内核必须能伸手进那个浏览器
 *     能伸手      → 需要 CDP 端口 → 需要用户换方式启动浏览器
 *
 * 这一页就是来打破它的：把 token 显式摆出来，由用户自己填进扩展。**用户只需要做
 * 一次**，之后内核地址/token 变化（每次启动都重新生成）时再填一次即可。
 *
 * 为什么必须显示「扩展 id」让用户核对
 * ----------------------------------
 * ``api/agent_bridge.py::_origin_ok`` 会比对浏览器带来的
 * ``Origin: chrome-extension://<id>``。而未打包扩展的 id 是**按绝对路径算出来的**
 * （``sha256(路径)`` 前 16 字节按 nibble 映射 a-p，见
 * ``browser/extensions.py::extension_id_for_path``）——用户从别的目录加载、
 * 或者从符号链接打开，id 就会变，于是连接被拒。
 *
 * 而连接被拒的表现是静默的：token 对不上返回 ``denied`` 之后扩展只报"没连上内核"，
 * 用户无从判断是路径错了还是 token 过期。所以这里把期望值和实际连上的 id 都摆出来。
 */

/** 可复制的字段行。值用等宽字体，且支持点击选中（token 那种长串靠肉眼核对没用）。 */
function CopyRow({
  label,
  value,
  placeholder,
  note,
}: {
  label: string;
  value?: string;
  placeholder?: string;
  note?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[12.5px] text-ink-muted">{label}</span>
        {value && (
          <button
            type="button"
            onClick={() => {
              navigator.clipboard?.writeText(value);
              toast.success(`已复制${label}`);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
            className="inline-flex shrink-0 items-center gap-1 text-[12px] text-ink-faint transition-colors hover:text-brand"
          >
            <Copy size={12} />
            {copied ? "已复制" : "复制"}
          </button>
        )}
      </div>
      <code className="select-all break-all rounded bg-surface px-2 py-1.5 font-mono text-[12px] text-ink">
        {value || placeholder || "—"}
      </code>
      {note && <p className="text-[11.5px] leading-5 text-ink-faint">{note}</p>}
    </div>
  );
}

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="flex gap-2.5 text-[12.5px] leading-6 text-ink-muted">
      <span className="mt-0.5 flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full bg-brand/12 text-[11px] font-semibold text-brand">
        {n}
      </span>
      <span className="min-w-0">{children}</span>
    </li>
  );
}

export default function BrowserConnectPage() {
  // 轮询而不是只取一次：扩展的 service worker 空闲 30 秒就被杀、连接随之断开，
  // 所以「连上了吗」是一个会来回变化的状态，用户很可能在填完配置后盯着这一页等。
  const q = useQuery({
    queryKey: ["agent-bridge-info"],
    queryFn: apiAgentBridgeInfo,
    refetchInterval: 2500,
  });
  const info = q.data;
  const online = (info?.connected ?? 0) > 0;
  /* 以实际连上的 id 为准，而不是拿 extension_id 反推：内核没登记过扩展 id 时
     _origin_ok会退化成「只要是某个扩展 origin」，此时比对没有意义。 */
  const gotRightExt =
    online && (info?.connected_ids ?? []).includes(info!.extension_id);

  return (
    <div className="mx-auto max-w-[860px]">
      <PageHeader
        title="浏览器"
        desc="把 keeper 的浏览器扩展装进你自己的 Chrome，页面内容就能直接交给 keeper。装好后浮球出现在每个网页上，点一下就能取到当前页面。"
        actions={
          <button
            type="button"
            onClick={() => q.refetch()}
            className="inline-flex items-center gap-1.5 rounded-[7px] border border-line-soft px-2.5 py-1.5 text-[12px] text-ink-muted transition-colors hover:bg-surface"
          >
            <RefreshCw size={13} className={q.isFetching ? "animate-spin" : ""} />
            刷新
          </button>
        }
      />

      <Stack>
        {/* ---------- 状态 ---------- */}
        <Section
          title="连接状态"
          extra={
            <span className="inline-flex items-center gap-1.5 text-[12px]">
              {online ? (
                <>
                  <CheckCircle2 size={14} className="text-emerald-500" />
                  <span className="text-emerald-600 dark:text-emerald-400">
                    已连接 {info?.connected} 个
                  </span>
                </>
              ) : (
                <>
                  <CircleDashed size={14} className="text-ink-faint" />
                  <span className="text-ink-faint">未连接</span>
                </>
              )}
            </span>
          }
        >
          <div className="space-y-3">
            {q.isError && (
              <Hint tone="danger" title="取不到内核信息">
                {String((q.error as Error)?.message || q.error)}
              </Hint>
            )}

            {online && gotRightExt && (
              <Hint tone="info" title="扩展已接入">
                装进来的就是 keeper 自带的这一个，配置正确。现在可以在任意网页右下角点浮球了。
              </Hint>
            )}

            {online && !gotRightExt && info?.extension_id && (
              <Hint tone="warn" title="连上来了，但不是这一个扩展">
                在线的是 {(info.connected_ids || []).join("、") || "未知 id"}，
                而期望的是 <code className="font-mono">{info.extension_id}</code>。
                扩展 id 由**加载时的绝对路径**算出来（见下面「安装步骤」第 3 步）——
                路径换一个位置，id 就变，内核会拒绝它的连接。
              </Hint>
            )}

            {!online && !q.isError && (
              <Hint>
                还没连上。按下面四步装一次扩展、填一次连接码即可。
                <br />
                装好后如果这里一直是 0：扩展的 service worker 空闲约 30 秒会被 Chrome
                杀掉、连接随之断开，下次点浮球会自动重连——所以「瞬时为 0」是正常的，
                持续为 0 才是没配好。
              </Hint>
            )}
          </div>
        </Section>

        {/* ---------- 连接码 ---------- */}
        <Section
          title="连接码"
          extra={
            info?.connect_code && (
              <button
                type="button"
                onClick={() => {
                  navigator.clipboard?.writeText(info.connect_code);
                  toast.success("连接码已复制");
                }}
                className="inline-flex items-center gap-1 rounded-[7px] border border-line-soft px-2 py-1 text-[12px] text-ink-muted transition-colors hover:bg-surface"
              >
                <Copy size={12} />
                复制连接码
              </button>
            )
          }
        >
          <div className="space-y-4">
            <p className="text-[12.5px] leading-5 text-ink-muted">
              这是扩展填「选项」页需要的一整串内容，已经把地址和 token 打包好了。
              内核每次启动端口和 token 都会重新生成，所以换一次启动就要重填一次。
            </p>
            <CopyRow
              label="连接码"
              value={info?.connect_code}
              note="形如 keeper1:eyJ1IjoiaHR0cDovLzEyNy4wLjAuMTo1NDUzMCIs…"
            />
            {!info?.auth_enabled && (
              <Hint tone="warn" title="内核未启用访问令牌">
                这台内核是用命令行方式起的、没有配置 token。这意味着本机上任何能访问该端口的
                进程都可以直接调用 keeper 的全部接口。自带浏览器形态下建议改由桌面壳启动。
              </Hint>
            )}
          </div>
        </Section>

        {/* ---------- 安装步骤 ---------- */}
        <Section title="安装步骤">
          <ol className="space-y-2.5">
            <Step n={1}>
              在 Chrome 地址栏打开{" "}
              <code className="select-all font-mono text-brand">chrome://extensions</code>
            </Step>
            <Step n={2}>
              打开右上角的<b>开发者模式</b>开关
            </Step>
            <Step n={3}>
              点<b>加载已解压的扩展程序</b>，选择目录{" "}
              <code className="select-all break-all font-mono text-brand">
                {info?.extension_path || "读取中…"}
              </code>
              <br />
              <span className="text-[11.5px] text-ink-faint">
                必须选这个路径。扩展 id 是按绝对路径算的，换个位置装 id 就变了。
              </span>
            </Step>
            <Step n={4}>
              在扩展卡片上点<b>详情 → 扩展程序选项</b>，把上面的连接码粘进去保存
            </Step>
          </ol>
        </Section>

        {/* ---------- 明细（自查用） ---------- */}
        <Section
          title="连接明细"
          extra={<span className="text-[11.5px] text-ink-faint">填不上时逐项核对</span>}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <CopyRow label="内核地址" value={info?.kernel_url} />
            <CopyRow label="WebSocket 地址" value={info?.ws_url} />
            <CopyRow label="令牌 token" value={info?.token} placeholder="（未启用认证）" />
            <CopyRow
              label="期望的扩展 id"
              value={info?.extension_id}
              note="装出来的不一致 → 多半是第 3 步的目录选错了"
            />
          </div>
        </Section>
      </Stack>
    </div>
  );
}