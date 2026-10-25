/**
 * 「我的空间」文件监听：文件一变，立刻通知内核把这个文件重新索引。
 *
 * 为什么在桌面壳里做，而不是在 Python 里做
 * --------------------------------------
 * Node 的 `fs.watch` 一个 API 就覆盖了 macOS(FSEvents) / Windows(ReadDirectoryChangesW)，
 * Linux(inotify) —— 但递归监听在部分平台上不支持，需要逐目录挂 watcher（见 _watchTree）。
 * 而 keeper 未来的 UI 是桌面专属的，所以这层天然属于桌面壳。
 *
 * 监听只是**提示**，不是事实
 * -------------------------
 * 三条现实坑：
 * 1. 编辑器保存是「写临时文件 + rename」，事件里的文件名可能是临时文件；
 * 2. 事件会合并、延迟，队列溢出会丢；
 * 3. 目录被改名/移动时，事件给的是目录名，里面文件的新路径不会出现在事件里。
 *
 * 所以这里只做两件事：**把可能变了的 .md 路径收集起来，debounce 之后交给内核**。
 * 真正「变了没有」由内核用内容 hash 判定（见 keeper/search/indexer.py）。
 * 漏掉的事件由内核的定时扫描兜底（keeper/search/sync.py）。
 *
 * 只通知内核「哪些文件可能变了」，不自己判断内容 —— 判断内容要读文件，
 * 那是内核的活（它有索引库、有模型）。
 */
const fs = require('fs');
const path = require('path');

/** 与内核 detect.MARKDOWN_EXTS 保持一致：只有 Markdown 才进检索索引 */
const MD_EXT = new Set(['.md', '.markdown', '.mdown', '.mkd']);

/** 合并窗口：编辑器保存往往连发好几个事件（write + rename），攒一起发一次 */
const DEFAULT_DEBOUNCE_MS = 400;

function isMarkdown(p) {
  return MD_EXT.has(path.extname(String(p)).toLowerCase());
}

/**
 * 逐目录挂 watcher 的兜底方案。
 *
 * `fs.watch(dir, {recursive:true})` 在 Linux 上要 Node 20+ 才支持，
 * 平台不支持时抛 ERR_FEATURE_UNAVAILABLE_ON_PLATFORM —— 这时只能自己递归。
 * 缺点：新建的目录不会被自动加监听（要靠内核的定时扫描兜底）。
 */
function _watchTree(root, onEvent, log) {
  const watchers = [];
  const walk = (dir) => {
    let watcher;
    try {
      watcher = fs.watch(dir, { persistent: true }, (evt, filename) => onEvent(dir, filename));
    } catch (e) {
      return;
    }
    watcher.on('error', (e) => log(`[watch] 监听 ${dir} 出错：${e.message}`));
    watchers.push(watcher);

    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (e) {
      return;
    }
    for (const ent of entries) {
      if (ent.isDirectory()) walk(path.join(dir, ent.name));
    }
  };
  walk(root);
  return watchers;
}

/**
 * 开始监听「我的空间」。
 *
 * @param {object} opts
 * @param {string} opts.root  「我的空间」根目录（绝对路径）
 * @param {string} opts.url   内核地址（http://127.0.0.1:<port>）
 * @param {number} [opts.debounceMs]
 * @param {(msg: string) => void} [opts.log]
 * @returns {{root: string, close: () => void} | null}
 */
function startSpaceWatch({ root, url, debounceMs = DEFAULT_DEBOUNCE_MS, log = console.log } = {}) {
  if (!root || !url) return null;
  if (!fs.existsSync(root)) {
    log(`[watch] 「我的空间」不存在，不监听：${root}`);
    return null;
  }

  /** 本轮攒下来、还没通知内核的路径 */
  const pending = new Set();
  let timer = null;
  let closed = false;

  const flush = async () => {
    timer = null;
    if (closed || pending.size === 0) return;
    const paths = Array.from(pending);
    pending.clear();
    try {
      const res = await fetch(`${url}/search/index`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      });
      const data = await res.json().catch(() => ({}));
      log(`[watch] 已通知内核更新 ${paths.length} 个文件（${data.status || '?'}）`);
    } catch (e) {
      log(`[watch] 通知内核失败：${e && e.message}`);
    }
  };

  const onEvent = (dir, filename) => {
    if (closed || !filename) return;
    const full = path.join(dir, filename);
    // 非 Markdown 不进检索索引（xlsx / 图片等目前没有切片器）
    if (!isMarkdown(full)) return;
    pending.add(full);
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, debounceMs);
    timer.unref?.();
  };

  let watchers = [];
  try {
    const w = fs.watch(root, { recursive: true, persistent: true }, (evt, filename) =>
      onEvent(root, filename)
    );
    w.on('error', (e) => log(`[watch] 监听出错：${e.message}`));
    watchers = [w];
  } catch (e) {
    log(`[watch] 递归监听不可用（${e && e.message}），改用逐目录监听`);
    watchers = _watchTree(root, onEvent, log);
  }

  if (!watchers.length) {
    log('[watch] 监听启动失败');
    return null;
  }

  log(`[watch] 开始监听「我的空间」：${root}`);
  return {
    root,
    close() {
      closed = true;
      if (timer) clearTimeout(timer);
      for (const w of watchers) {
        try {
          w.close();
        } catch (e) {
          /* 关不掉就算了，进程退出时自然释放 */
        }
      }
      watchers = [];
    },
  };
}

module.exports = { startSpaceWatch, isMarkdown };
