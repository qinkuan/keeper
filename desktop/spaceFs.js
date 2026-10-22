'use strict';
/**
 * 「我的空间」文件操作——桌面原生实现（Electron 主进程 / Node）。
 *
 * 对应 Python 端 keeper/zonefilesystem/space_ops，但跑在 Electron 主进程里，给
 * 「我的空间」面板的人肉操作（复制 / 粘贴 / 导入 / 重命名 / 删除 / 新建 / 列目录 / 读取）
 * 用。会话与内核侧的「系统操作」仍走 Python 后端，这里不动它。
 *
 * 安全边界：所有写操作都过 resolveSpacePath，把路径锁在「我的空间」根目录内，越界即抛错，
 * 与 Python 端 resolve_target 语义一致，保证 UI 和 agent 看到的是同一套约束。
 *
 * 注意：本机根目录由前端从内核（可信）取来后作为参数传入，不在 Electron 里另读配置，
 * 避免两套来源不一致。
 */
const fs = require('fs');
const fsp = fs.promises;
const path = require('path');
const { dialog, shell } = require('electron');

const MAX_TEXT = 2 * 1024 * 1024; // 与 Python 端一致的“过大不读”阈值
const MAX_BINARY = 64 * 1024 * 1024; // 预览二进制（xlsx/pdf 等）上限，超出不读，避免 OOM

/** 把 p 解析到 root 之下；越界（逃逸出 root）抛错。返回绝对路径。 */
function resolveSpacePath(root, p) {
  const r = path.resolve(root);
  const resolved = p ? path.resolve(r, p) : r;
  const rel = path.relative(r, resolved);
  if (rel.startsWith('..') || path.isAbsolute(rel)) {
    const e = new Error('路径越界：只能操作「我的空间」目录内');
    e.code = 'OUT_OF_BOUNDS';
    throw e;
  }
  return resolved;
}

/** 目标目录已存在同名时自动加 (1)/(2)…，避免覆盖。 */
function uniqueName(dir, name) {
  if (!fs.existsSync(path.join(dir, name))) return name;
  const dot = name.lastIndexOf('.');
  const hasExt = dot > 0 && dot < name.length - 1;
  const base = hasExt ? name.slice(0, dot) : name;
  const ext = hasExt ? name.slice(dot) : '';
  let i = 1;
  while (fs.existsSync(path.join(dir, `${base} (${i})${ext}`))) i++;
  return `${base} (${i})${ext}`;
}

/** 粗略判断一段字节是不是文本（二进制文件别喂给编辑器）。 */
function looksText(buf) {
  if (buf.includes(0)) return false; // 含 NUL 视为二进制
  let ctrl = 0;
  for (let i = 0; i < buf.length; i++) {
    const c = buf[i];
    if (c < 0x09 || (c > 0x0d && c < 0x20)) ctrl++;
    if (ctrl > buf.length / 20) return false;
  }
  return true;
}

async function list(root, dir) {
  const d = resolveSpacePath(root, dir);
  const entries = await fsp.readdir(d, { withFileTypes: true });
  const nodes = await Promise.all(
    entries.map(async (e) => {
      const full = path.join(d, e.name);
      let size = 0;
      let mtime = 0;
      try {
        const st = await fsp.stat(full);
        size = st.size;
        mtime = st.mtimeMs;
      } catch (_) {}
      return { name: e.name, path: full, isDir: e.isDirectory(), size, mtime };
    })
  );
  nodes.sort((a, b) =>
    a.isDir === b.isDir ? a.name.localeCompare(b.name) : a.isDir ? -1 : 1
  );
  return { path: d, items: nodes };
}

async function readFile(root, file) {
  const f = resolveSpacePath(root, file);
  const st = await fsp.stat(f);
  if (st.isDirectory())
    throw Object.assign(new Error('目录不能读取为文本'), { code: 'IS_DIR' });
  // 只读头部做探测，避免把超大文件整块读进内存
  const fh = await fsp.open(f, 'r');
  const buf = Buffer.alloc(MAX_TEXT + 1024);
  const { bytesRead } = await fh.read(buf, 0, buf.length, 0);
  await fh.close();
  const head = buf.subarray(0, bytesRead);
  const isText = head.length <= MAX_TEXT && looksText(head);
  return {
    name: path.basename(f),
    path: f,
    isDir: false,
    size: st.size,
    mtime: st.mtimeMs,
    is_text: isText,
    too_large: st.size > MAX_TEXT,
    text: isText ? head.toString('utf8') : '',
  };
}

/** 读文件二进制字节，回 base64（预览 xlsx/pdf 用）。路径锁在根内；超过上限抛错。 */
async function readBinary(root, file, maxBytes = MAX_BINARY) {
  const f = resolveSpacePath(root, file);
  const st = await fsp.stat(f);
  if (st.isDirectory())
    throw Object.assign(new Error('目录不能读取为字节'), { code: 'IS_DIR' });
  if (st.size > maxBytes)
    throw Object.assign(
      new Error(`文件过大（> ${Math.round(maxBytes / 1048576)}MB），无法预览`),
      { code: 'TOO_LARGE' }
    );
  const buf = await fsp.readFile(f);
  return { name: path.basename(f), size: st.size, base64: buf.toString('base64') };
}

/** 写回文件内容（「我的空间」内联编辑保存用）。父目录不存在则建好，路径锁在根内。 */
async function writeFile(root, file, content) {
  const f = resolveSpacePath(root, file);
  await fsp.mkdir(path.dirname(f), { recursive: true });
  await fsp.writeFile(f, content ?? '', 'utf8');
  const st = await fsp.stat(f);
  return { ok: true, path: f, size: st.size, mtime: st.mtimeMs };
}

async function mkdir(root, parent, name) {
  const p = resolveSpacePath(root, path.join(parent || '', name));
  await fsp.mkdir(p, { recursive: true });
  return { ok: true, path: p, name: path.basename(p) };
}

async function rename(root, file, newName) {
  const f = resolveSpacePath(root, file);
  const dest = path.join(path.dirname(f), newName);
  await fsp.rename(f, dest);
  return { ok: true, path: dest, name: newName };
}

async function remove(root, file) {
  const f = resolveSpacePath(root, file);
  await fsp.rm(f, { recursive: true, force: true });
  return { ok: true };
}

async function _copyOne(srcAbs, dstDirAbs) {
  const name = uniqueName(dstDirAbs, path.basename(srcAbs));
  const dst = path.join(dstDirAbs, name);
  // Node 16.7+ 的 fs.cp 递归即可覆盖文件与目录；Electron 30 自带 Node 20，可用
  await fsp.cp(srcAbs, dst, { recursive: true });
  return { ok: true, path: dst, name };
}

async function copy(root, src, dstDir) {
  const s = resolveSpacePath(root, src);
  const d = resolveSpacePath(root, dstDir);
  if (!fs.statSync(d).isDirectory())
    throw Object.assign(new Error('目标必须是一个目录'), { code: 'NOT_DIR' });
  if (d === s || d.startsWith(s + path.sep))
    throw Object.assign(new Error('不能复制到自身或其子目录内'), {
      code: 'BAD_TARGET',
    });
  return _copyOne(s, d);
}

async function importFromLocal(root, src, dstDir) {
  // 源是用户本机任意路径（桌面形态下与内核同机），位置不限
  const s = path.resolve(src);
  if (!fs.existsSync(s))
    throw Object.assign(new Error('源文件不存在'), { code: 'NO_SRC' });
  const d = resolveSpacePath(root, dstDir);
  if (!fs.statSync(d).isDirectory())
    throw Object.assign(new Error('目标必须是一个目录'), { code: 'NOT_DIR' });
  if (d === s || d.startsWith(s + path.sep))
    throw Object.assign(new Error('不能复制到自身或其子目录内'), {
      code: 'BAD_TARGET',
    });
  return _copyOne(s, d);
}

// ── 原生对话框（浏览器做不到，桌面壳专有）──

/** 选一个目录，返回绝对路径或 null（取消）。 */
async function pickFolder() {
  const r = await dialog.showOpenDialog({
    properties: ['openDirectory', 'createDirectory'],
  });
  if (r.canceled || !r.filePaths.length) return null;
  return r.filePaths[0];
}

/** 选文件 / 文件夹，返回绝对路径数组（multi=true 可多选）；取消返回 []。 */
async function pickFile(multi = false) {
  const r = await dialog.showOpenDialog({
    properties: [
      'openFile',
      'openDirectory',
      'createDirectory',
      ...(multi ? ['multiSelections'] : []),
    ],
  });
  if (r.canceled || !r.filePaths.length) return [];
  return r.filePaths;
}

/** 在系统文件管理器里定位并选中该路径（目录或文件均可）。 */
function reveal(p) {
  shell.showItemInFolder(path.resolve(p));
  return { ok: true };
}

module.exports = {
  resolveSpacePath,
  list,
  readFile,
  readBinary,
  writeFile,
  mkdir,
  rename,
  remove,
  copy,
  importFromLocal,
  pickFolder,
  pickFile,
  reveal,
};
