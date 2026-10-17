import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Copy,
  RefreshCw,
  TriangleAlert,
} from "lucide-react";
import { apiAgentBridgeInfo } from "../api/client";
import { Hint, Section } from "./ui/Page";
import { toast } from "./ui/Toast";

/**
 * 浏览器页顶部的「扩展接入」折叠面板。
 *
 * 为什么折叠
 * ----------
 * 扩展接好之后，这一整块（连接码 / 安装步骤 / 连接明细）就变成**一次性**操作，
 * 却要长期占掉浏览器页的上半屏——而那个位置现在是会话列表和对话区。所以默认状态
 * 跟着接入状态走：没接上时展开（用户正需要指引），接上后收成一行状态条。
 *
 * 但不能做成「接上就再也打不开」：连接会掉（MV3 的 service worker 空闲 30 秒就被
 * 杀掉），而 token 每次内核重启都会变，于是用户偶尔需要重新看一眼连接码。所以保留
 * 一个手动开关，且**手动展开过的状态优先于自动判定**——自动逻辑只在用户没表达过
 * 意图时接管，否则「我刚展开想看连接码」会被下一轮轮询偷偷收回去。
 *
 * 面板为什么存在（这段背景别删，它解释的是「为什么必须用户手填一次 token」）
 * --------------------------------------------------------------------------
 * 内核原本是靠 Playwright 的 ``ctx.service_workers`` 找到扩展的 service worker，
 * 往它的 ``chrome.storage.local`` 里写内核地址和 token。那只在扩展跑在 **keeper 自带
 * 的那个 Chromium** 里时成立——那是同一个进程，内核伸手就够得着。
 *
 * 扩展装到用户自己的 Chrome 之后就不行了：另一个进程、另一个 profile。于是变成死结：
 *
 *     扩展要连内核 → 需要 token
 *     要拿到 token → 内核必须能伸手进那个浏览器
 *     能伸手      → 需要 CDP 端口 → 需要用户换方式启动浏览器
 *
 * 打破它的办法只有一条：把 token 显式摆出来，由用户自己填进扩展。
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

export default function BrowserConnectPanel() {
  // 轮询而不是只取一次：扩展的 service worker 空闲 30 秒就被杀、连接随之断开，
  // 所以「连上了吗」是一个会来回变化的状态。
  const q = useQuery({
    queryKey: ["agent-bridge-info"],
    queryFn: apiAgentBridgeInfo,
    refetchInterval: 2500,
  });
  const info = q.data;
  const online = (info?.connected ?? 0) > 0;
  /* 以实际连上的 id 为准，而不是拿 extension_id 反推：内核没登记过扩展 id 时
     _origin_ok 会退化成「只要是某个扩展 origin」，此时比对没有意义。 */
  const gotRightExt =
    online && (info?.connected_ids ?? []).includes(info!.extension_id);

  /* null = 交给自动判定（没接上就展开）；非 null = 用户手动拨过，尊重它。 */
  const [override, setOverride] = useState<boolean | null>(null);
  const autoOpen = !online || !gotRightExt;
  const open = override ?? autoOpen;

  const tone = q.isError
    ? { icon: TriangleAlert, cls: "text-amber-500", text: "取不到内核信息" }
    : online && gotRightExt
      ? {
          icon: CheckCircle2,
          cls: "text-emerald-500",
          text: `扩展已连接${info?.connected ? `（${info.connected} 个）` : ""}`,
        }
      : online
        ? {
            icon: TriangleAlert,
            cls: "text-amber-500",
            text: "连上来了，但不是这一个扩展",
          }
        : { icon: CircleDashed, cls: "text-ink-faint", text: "扩展未连接" };
  const StatusIcon = tone.icon;

  return (
    <section
      className="shrink-0 border-b border-line-soft bg-panel/60"
      aria-label="扩展接入"
    >
      {/* 收起时也只有这一行：状态 + 一键展开。不做成「接上就彻底消失」——
            连接会掉、token 会变，用户得随时能再要一次连接码。 */}
      <button
        type="button"
        onClick={() => setOverride(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2 text-left transition-colors hover:bg-surface/60"
      >
        <StatusIcon size={14} className={`shrink-0 ${tone.cls}`} />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-muted">
          {tone.text}
          {online && gotRightExt && (
            <span className="text-ink-faint">
              {" "}
              · 在任意网页点浮球即可开始
            </span>
          )}
        </span>
        {!online && (
          <span className="shrink-0 text-[11.5px] text-ink-faint">
            需要一次设置
          </span>
        )}
        <ChevronDown
          size={14}
          className={`shrink-0 text-ink-faint transition-transform ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {open && (
        <div className="animate-fade-up space-y-4 px-4 pb-4 pt-1">
          <div className="flex items-center justify-end">
            <button
              type="button"
              onClick={() => q.refetch()}
              className="inline-flex items-center gap-1.5 rounded-[7px] border border-line-soft px-2.5 py-1.5 text-[12px] text-ink-muted transition-colors hover:bg-surface"
            >
              <RefreshCw size={13} className={q.isFetching ? "animate-spin" : ""} />
              刷新
            </button>
          </div>

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
                扩展 id 由**加载时的绝对路径**算出来（见下面第 3 步）——路径换一个位置，
                id 就变，内核会拒绝它的连接。
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
                这是扩展浮球面板里要填的一整串内容，已经把地址和 token 打包好了。
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

          <Section title="安装步骤">
            <ol className="space-y-2.5">
              <Step n={1}>
                在 Chrome 地址栏打开{" "}
                <code className="select-all font-mono text-brand">
                  chrome://extensions
                </code>
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
                在任意网页右下角点开浮球，把上面的连接码粘进输入框保存
              </Step>
            </ol>
          </Section>

          <Section
            title="连接明细"
            extra={
              <span className="text-[11.5px] text-ink-faint">填不上时逐项核对</span>
            }
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <CopyRow label="内核地址" value={info?.kernel_url} />
              <CopyRow label="WebSocket 地址" value={info?.ws_url} />
              <CopyRow
                label="令牌 token"
                value={info?.token}
                placeholder="（未启用认证）"
              />
              <CopyRow
                label="期望的扩展 id"
                value={info?.extension_id}
                note="装出来的不一致 → 多半是第 3 步的目录选错了"
              />
            </div>
          </Section>
        </div>
      )}
    </section>
  );
}
