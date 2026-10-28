// 内置插件安装/升级策略自测。
//
// 跑的是**线上那份实现**（desktop/seedPlugins.js），不是抄一遍的——之前两边各
// 抄一份，逻辑慢慢走样（清单格式从 v1 换到 v2 时测试还在按 v1 断言）。
//
// 验证六件事：
//   1. 首次安装：所有文件装上，清单写成 v2；
//   2. 升级：用户**没改过**的文件被换成新版；
//   3. 升级：用户**改过**的文件保持不动，且**下一次升级也不会被冲掉**；
//   4. 升级：新增文件 → 装上；
//   5. __pycache__ / .pyc 不进清单；
//   6. 没有清单（旧版本装的）→ 备份旧文件后覆盖成新版（升级不能被卡死）。
//
// 跑法：node tests/test_seed_plugins.js（需要 node）
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const assert = require('assert');

const {
  installBuiltinPlugins,
  hashFile,
} = require('../desktop/seedPlugins.js');

function hashOf(p) {
  return hashFile(p);
}

let pass = 0;
let fail = 0;
function check(cond, msg) {
  if (cond) {
    pass++;
    console.log('  ✓ ' + msg);
  } else {
    fail++;
    console.log('  ✗ ' + msg);
  }
}

function write(p, content) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, content, 'utf-8');
}

function read(p) {
  return fs.readFileSync(p, 'utf-8');
}

function manifestOf(dest) {
  return JSON.parse(read(path.join(dest, '.builtin-manifest.json')));
}

function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-seed-'));
  const src = path.join(tmp, 'src');
  const dest = path.join(tmp, 'dest');

  console.log('1) 首次安装');
  write(path.join(src, 'pdf', 'bin', 'pdf.py'), 'v1');
  write(path.join(src, 'pdf', 'keeper-plugin.json'), '{}');
  write(path.join(src, 'bash', 'bin', 'bash_server.py'), 'v1');
  let r = installBuiltinPlugins(src, dest);
  check(r.installed === 3, '3 个文件都装上了');
  check(fs.existsSync(path.join(dest, 'pdf', 'bin', 'pdf.py')), 'pdf 插件在用户目录里');
  check(fs.existsSync(path.join(dest, '.builtin-manifest.json')), '写了哈希清单');
  check(manifestOf(dest).version === 2, '清单是 v2 格式');
  check(
    manifestOf(dest).files['pdf/bin/pdf.py'].shipped ===
      manifestOf(dest).files['pdf/bin/pdf.py'].local,
    'v2 清单里 shipped / local 都记了'
  );

  console.log('2) 升级：用户没改过 → 换成新版');
  write(path.join(src, 'pdf', 'bin', 'pdf.py'), 'v2');
  write(path.join(src, 'bash', 'bin', 'bash_server.py'), 'v2');
  r = installBuiltinPlugins(src, dest);
  check(r.updated === 2, '2 个文件升级了');
  check(read(path.join(dest, 'pdf', 'bin', 'pdf.py')) === 'v2', 'pdf 插件内容是新版');

  console.log('3) 升级：用户改过 → 保留（而且下一轮也不能被冲掉）');
  write(path.join(dest, 'pdf', 'bin', 'pdf.py'), '用户自己改的');
  write(path.join(src, 'pdf', 'bin', 'pdf.py'), 'v3');
  r = installBuiltinPlugins(src, dest);
  check(read(path.join(dest, 'pdf', 'bin', 'pdf.py')) === '用户自己改的', '用户改过的文件没被动');
  check(r.kept === 1, '计入"保留用户改动"');
  // 再升一轮：这是以前记账方式漏掉的地方——shipped 被写成新版哈希之后，
  // 用户改动会被当成"过期内容"冲掉。
  write(path.join(src, 'pdf', 'bin', 'pdf.py'), 'v4');
  r = installBuiltinPlugins(src, dest);
  check(
    read(path.join(dest, 'pdf', 'bin', 'pdf.py')) === '用户自己改的',
    '再升一轮，用户改过的文件仍然没被动'
  );

  console.log('4) 升级：新增文件 → 装上');
  write(path.join(src, 'pdf', 'skills', 'SKILL.md'), '新文件');
  r = installBuiltinPlugins(src, dest);
  check(fs.existsSync(path.join(dest, 'pdf', 'skills', 'SKILL.md')), '新增文件装上了');

  console.log('5) __pycache__ 不进清单');
  write(path.join(src, 'pdf', 'bin', '__pycache__', 'x.pyc'), 'junk');
  installBuiltinPlugins(src, dest);
  check(
    !Object.keys(manifestOf(dest).files).some((k) => k.includes('__pycache__')),
    '清单里没有 __pycache__'
  );

  console.log('6) 没有清单（老版本装的）→ 备份后覆盖成新版');
  const dest2 = path.join(tmp, 'dest2');
  write(path.join(dest2, 'pdf', 'bin', 'pdf.py'), '老版本装的老内容');
  r = installBuiltinPlugins(src, dest2);
  check(
    read(path.join(dest2, 'pdf', 'bin', 'pdf.py')) === 'v4',
    '来源不明的旧文件被覆盖成新版（升级不能被卡死）'
  );
  check(
    read(path.join(dest2, '.user-backup', 'pdf', 'bin', 'pdf.py')) === '老版本装的老内容',
    '覆盖前那份留了备份'
  );
  check(r.backedUp === 1, '计入"备份旧文件"');

  console.log('7) 执行位：bin/* 必须可执行');
  const dest3 = path.join(tmp, 'dest3');
  write(path.join(src, 'bash', 'bin', 'bash_server.py'), 'v5');
  fs.chmodSync(path.join(src, 'bash', 'bin', 'bash_server.py'), 0o755);
  installBuiltinPlugins(src, dest3);
  const mode = fs.statSync(path.join(dest3, 'bash', 'bin', 'bash_server.py')).mode & 0o777;
  check((mode & 0o111) !== 0, '装过去的 bin 保留了执行位');

  console.log('8) 旧版清单（v1）迁移');
  // 8a) v1 记的正好是磁盘上这一份 → 用户没动过 → 直接升级，不备份
  const dest4 = path.join(tmp, 'dest4');
  write(path.join(dest4, 'pdf', 'bin', 'pdf.py'), 'v1');
  write(
    path.join(dest4, '.builtin-manifest.json'),
    JSON.stringify({ 'pdf/bin/pdf.py': hashOf(path.join(dest4, 'pdf', 'bin', 'pdf.py')) })
  );
  write(path.join(src, 'pdf', 'bin', 'pdf.py'), 'v9');
  r = installBuiltinPlugins(src, dest4);
  check(read(path.join(dest4, 'pdf', 'bin', 'pdf.py')) === 'v9', 'v1 清单对得上 → 升级了');
  check(
    !fs.existsSync(path.join(dest4, '.user-backup')),
    'v1 清单对得上 → 不用备份（那份是内置旧版，不是用户写的）'
  );

  // 8b) v1 记的跟磁盘上那份对不上（老版本的记账 bug：它记的是仓库新版哈希）
  //     → 当成来源不明 → 备份 + 覆盖，升级不被卡死
  const dest5 = path.join(tmp, 'dest5');
  write(path.join(dest5, 'pdf', 'bin', 'pdf.py'), '老版本装的老内容');
  write(
    path.join(dest5, '.builtin-manifest.json'),
    JSON.stringify({ 'pdf/bin/pdf.py': hashOf(path.join(src, 'pdf', 'bin', 'pdf.py')) })
  );
  r = installBuiltinPlugins(src, dest5);
  check(read(path.join(dest5, 'pdf', 'bin', 'pdf.py')) === 'v9', 'v1 清单对不上 → 也升级了');
  check(
    read(path.join(dest5, '.user-backup', 'pdf', 'bin', 'pdf.py')) === '老版本装的老内容',
    '覆盖前那份留了备份'
  );

  fs.rmSync(tmp, { recursive: true, force: true });
  console.log('\n结果：' + pass + ' 通过，' + fail + ' 失败');
  process.exit(fail ? 1 : 0);
}

main();
