/**
 * 内置插件的安装 / 升级（桌面壳用）。
 *
 * 为什么单独一个文件
 * ------------------
 * 这套逻辑要能被 node 直接跑测试，而 ``sidecar.js`` 依赖 electron（``app`` /
 * ``process.resourcesPath``），在普通 node 里 require 就炸。所以实现放这里，
 * ``sidecar.js`` 只负责算路径再调进来——测试跑的代码和线上跑的代码是同一份，
 * 不会再出现"测试通过、线上不一致"（之前就是复制粘贴两份实现，逻辑慢慢走样）。
 *
 * 装到哪
 * ------
 * 内核读插件的位置是 config.yaml 的 ``plugins.root``（默认 ``~/.keeper/plugins``），
 * **不在**代码目录里。所以打包时必须把 app 里带的插件复制过去，否则新装的机器上
 * 插件库是空的——bash 插件（agent 唯一的执行入口）也就没有，agent 连一条命令都
 * 跑不了。
 *
 * 覆盖策略：逐文件比对哈希
 * -----------------------
 * 曾经的做法是「目录已存在就整个跳过」。那有个致命后果：用户装完之后，内置插件
 * 就永远停在**首装那一版**——插件里的 bug 修复（包括日志降噪这类）永远到不了
 * 用户机器，只能等用户手动删目录重装。
 *
 * 但也不能无脑覆盖：用户可能基于内置插件改过（plugin-authoring 插件就是干这个
 * 的），冲掉了就没了。
 *
 * 所以记一份「安装时的哈希清单」（``.builtin-manifest.json``），升级时逐文件
 * 比对。清单里每个文件记**两个**哈希：
 *
 *   - ``shipped``：应用上次写进去的那一版的哈希 —— 拿它判断"用户动过没"；
 *   - ``local``：上次检查时磁盘上那一版的哈希 —— 只作记账，便于排查。
 *
 * 判定（``disk`` = 当前磁盘文件的哈希）：
 *
 *   - 目标不存在 → 新文件，装上；
 *   - ``disk == shipped`` → 用户没动过 → 用新版覆盖；
 *   - ``disk != shipped`` → 用户改过 → **保留不动**，且 ``shipped`` 保持原值
 *     （保持原值是关键：一旦把它改成新版哈希，下次就会误判成"用户没动过"，
 *     然后把用户的改动冲掉）；
 *   - 清单里没这个文件（**旧版本装的，没有清单**）→ 来源不明：不知道磁盘上那份
 *     是"旧版内置插件"还是"用户改过的"。保守不覆盖是安全的，但那样内置插件就
 *     永远升不了级（本机就是这么卡住的：bash 插件停在了首装那一版）。所以这里
 *     改成：先把磁盘上那份**备份**到 ``.user-backup/``，再覆盖成新版——升级一定
 *     生效，用户那份也不会丢。
 *
 * 备份目录是点目录（``.user-backup``），插件库扫描会跳过点目录，所以它既不会被
 * 当成一个插件，也不会出现在插件列表里。
 */
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

/** 文件内容的 sha256（读不动就返回 null，交给上层当"没法判定"处理） */
function hashFile(file) {
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch (e) {
    return null;
  }
}

/** 列出目录下的文件（相对路径），跳过 __pycache__ 之类易变的编译缓存 */
function listFiles(dir, base, out) {
  out = out || [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return out;
  }
  for (const entry of entries) {
    if (entry.name === '__pycache__' || entry.name.endsWith('.pyc')) continue;
    const full = path.join(dir, entry.name);
    const rel = base ? base + '/' + entry.name : entry.name;
    if (entry.isDirectory()) listFiles(full, rel, out);
    else if (entry.isFile()) out.push(rel);
  }
  return out;
}

/**
 * 拷贝一个文件，并**显式**把源文件的权限带过去。
 *
 * 为什么不能只靠 copyFileSync：插件的 ``bin/*`` 是要**直接执行**的（靠
 * ``#!/usr/bin/env python3`` 这个 shebang），少一个执行位插件就装上了却起不来。
 * 各平台上 copyfile 对"目标已存在"的权限处理不一样（有的保留目标旧权限），
 * 所以显式 chmod 一次，别赌平台行为。
 */
function copyPreservingMode(src, dst) {
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst);
  try {
    fs.chmodSync(dst, fs.statSync(src).mode & 0o777);
  } catch (e) {
    // 权限拿不到就按原样留着：内容已经对了，执行位的事由内核启动时的自愈兜底。
  }
}

/**
 * 读安装清单。兼容两种格式：
 *   - v2（现在）：``{version:2, files:{key:{shipped,local}}}``；
 *   - v1（老版本）：扁平的 ``{key: hash}``。
 *
 * v1 的记账方式有个 bug：它记的是**仓库里新版的哈希**，而不是实际装到磁盘上
 * 那一版的哈希（文件被"保留"时也记新版的）。所以 v1 的记录**不能直接信**——
 * 拿它当"用户没动过"的判据，会把"其实是旧版内置文件"误判成"用户改过"，
 * 于是内置插件永远升不了级（本机 bash 插件就卡在这）。
 *
 * 所以 v1 的记录降级成 ``legacy``：只有当磁盘上那份的哈希**正好等于**它时，
 * 才认定"用户没动过"（正常装上去的文件就是这种情况）；对不上就当来源不明，
 * 走备份 + 覆盖那条路。
 */
function readManifest(manifestPath) {
  let raw = {};
  try {
    raw = JSON.parse(fs.readFileSync(manifestPath, 'utf-8')) || {};
  } catch (e) {
    raw = {}; // 没有 / 坏了 → 当作没清单
  }
  if (raw && typeof raw === 'object' && raw.files && typeof raw.files === 'object') {
    return raw.files;
  }
  const files = {};
  for (const key of Object.keys(raw || {})) {
    if (typeof raw[key] === 'string') {
      files[key] = { shipped: null, local: raw[key], legacy: raw[key] };
    }
  }
  return files;
}

/**
 * 把磁盘上那份旧文件备份到 ``<dest>/.user-backup/<key>``。
 *
 * 只在「来源不明、需要覆盖」时用：用户那份内容不该被静默冲掉，但内置插件又
 * 必须能升级——备份是这两件事唯一能同时成立的办法。
 */
function backupUserFile(dest, key) {
  try {
    const bak = path.join(dest, '.user-backup', key);
    fs.mkdirSync(path.dirname(bak), { recursive: true });
    fs.copyFileSync(path.join(dest, key), bak);
    return bak;
  } catch (e) {
    console.error('[desktop] 备份用户文件失败：' + (e && e.message ? e.message : e));
    return '';
  }
}

/**
 * 把内置插件装到 ``dest``（用户改过的文件保留不动，见文件头注释）。
 *
 * @param {string} src 内置插件所在目录
 * @param {string} dest 用户插件目录（~/.keeper/plugins）
 * @returns {{installed:number, updated:number, kept:number, backedUp:number}}
 */
function installBuiltinPlugins(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  const manifestPath = path.join(dest, '.builtin-manifest.json');
  const files = readManifest(manifestPath);
  const next = {};
  let installed = 0;
  let updated = 0;
  let kept = 0;
  let backedUp = 0;

  let names = [];
  try {
    names = fs.readdirSync(src);
  } catch (e) {
    return { installed, updated, kept, backedUp };
  }
  for (const name of names) {
    const from = path.join(src, name);
    let isDir = false;
    try {
      isDir = fs.statSync(from).isDirectory();
    } catch (e) {
      continue;
    }
    if (!isDir) continue;
    for (const rel of listFiles(from, '')) {
      const srcFile = path.join(from, rel);
      const dstFile = path.join(dest, name, rel);
      const key = name + '/' + rel;
      const newHash = hashFile(srcFile);
      const rec = files[key];

      if (!fs.existsSync(dstFile)) {
        copyPreservingMode(srcFile, dstFile);
        next[key] = { shipped: newHash, local: newHash };
        installed++;
        continue;
      }

      const diskHash = hashFile(dstFile);

      if (rec && rec.shipped) {
        if (diskHash === rec.shipped) {
          // 用户没动过 → 跟随升级
          if (diskHash === newHash) {
            next[key] = { shipped: newHash, local: diskHash };
            continue;
          }
          copyPreservingMode(srcFile, dstFile);
          next[key] = { shipped: newHash, local: newHash };
          updated++;
        } else {
          // 用户改过 → 保留。shipped 保持原值：它是"用户没动过"的判据，
          // 一旦改成新版哈希，下次就会把用户的改动当成过期内容冲掉。
          next[key] = { shipped: rec.shipped, local: diskHash };
          kept++;
        }
        continue;
      }

      // 清单里没有这个文件（v2 没有记录）→ 来源不明（旧版本装的 / 用户自己放的）。
      if (diskHash === newHash) {
        next[key] = { shipped: newHash, local: diskHash };
        continue;
      }
      // 旧版清单（v1）记的正好就是磁盘上这一份 → 用户没动过 → 直接升级，
      // 不用备份（备份的是用户自己写的内容，不是内置的旧版本）。
      if (rec && rec.legacy && rec.legacy === diskHash) {
        copyPreservingMode(srcFile, dstFile);
        next[key] = { shipped: newHash, local: newHash };
        updated++;
        continue;
      }
      // 备份后覆盖：内置插件必须能升级，用户那份留在 .user-backup 里。
      if (backupUserFile(dest, key)) backedUp++;
      copyPreservingMode(srcFile, dstFile);
      next[key] = { shipped: newHash, local: newHash };
      updated++;
    }
  }
  try {
    fs.writeFileSync(manifestPath, JSON.stringify({ version: 2, files: next }, null, 2));
  } catch (e) {
    console.error('[desktop] 写插件清单失败：' + (e && e.message ? e.message : e));
  }
  if (installed || updated || backedUp) {
    console.log(
      '[desktop] 内置插件已更新（新增 ' + installed + '，升级 ' + updated +
      '，保留用户改动 ' + kept + (backedUp ? '，备份旧文件 ' + backedUp : '') + '）'
    );
  }
  return { installed, updated, kept, backedUp };
}

module.exports = {
  installBuiltinPlugins,
  hashFile,
  listFiles,
  readManifest,
};
