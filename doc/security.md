# 安全边界

> 本文记录 keeper 的**威胁模型**、现有防护、以及**已知缺口**。
> 目的不是"说明我们很安全"，而是让人能自己判断：**哪些能信、哪些不能信、以及哪里还没堵上。**

---

## 一、威胁模型

内核里有**能让 agent 执行本机命令**的接口（文件读写、shell、代码执行）。
而它监听在一个本地 TCP 端口上——同源策略只拦"读响应"，**不拦"发请求"**。

这意味着：**用户打开的任意一个网页，都能向那个端口发请求。**
所以"谁能访问这个端口"必须靠认证解决，浏览器自己不会帮你解决。

| 威胁主体 | 是否在防护范围内 | 说明 |
|---|---|---|
| 用户打开的恶意网页 | ✅ 在防护内 | 猜不到 token；带自定义头的请求要先过 CORS 预检，而预检结果网页 JS 读不到 |
| 同网段设备扫到端口 | ✅ 在防护内 | 握手即 401 |
| 恶意/无关扩展连 WebSocket | ✅ 在防护内 | `Origin` 必须是 `chrome-extension://<期望id>`，握手时比对 |
| **用户本机的其他进程** | ❌ **不在防护内** | 能直连端口、能读 `/agent-bridge/info` 拿到 token |
| 拿到了内核进程的人 | ❌ 不在防护内 | 能读环境变量（含 `KEEPER_TOKEN`）、能读数据库 |

**一句话**：keeper 假定「用户本机上的进程是可信的」。
它防的是**浏览器里的网页**和**局域网里的其他设备**。

---

## 二、认证机制

### token 从哪来

| 形态 | 来源 | 生命周期 |
|---|---|---|
| 桌面（`desktop/start_desktop.sh`） | `sidecar.js` 每次启动现生成 32 字节随机值，经环境变量 `KEEPER_TOKEN` 交给内核 | **每次启动重新生成**，不落盘 |
| 命令行（`./start_keeper.sh`） | `config.yaml` 的 `server.auth.token`，或同名环境变量 | 用户自己配置 |
| 两者都没有 | **不启用认证**，但启动时打一条醒目的警告 | — |

优先级是环境变量 > config.yaml（`api/auth.py::resolve_token`）。

### 前端怎么拿到 token

1. **preload 桥**（桌面形态）：`window.keeperHost.token`。
   页面读得到是**刻意的**——这一页就是它的用户。别的网页读不到（不同源、不同进程）。
2. **URL 查询参数**（命令行形态）：启动日志打印一条 `?keepertoken=` 的完整 URL，
   取到后立刻存进 sessionStorage 并把地址栏洗掉。
3. `VITE_KEEPER_TOKEN`（vite dev server）。

### 为什么不用 cookie / session

cookie 会被浏览器**自动附带**，等于把 token 又送回给每个跨站请求，CSRF 立刻回来。
自定义头不会被浏览器自动带上——这正是我们要的性质。

### 绑回环

`config.yaml` 里 `server.host` 必须是 `127.0.0.1`。
`0.0.0.0` 会让同网段设备直接访问到内核，而内核里有能执行本机命令的接口。

启动时会检查：开了认证但不是绑回环的，打警告——因为 token 解决的是
「谁能访问这个端口」，不是「访问者是谁」。

---

## 三、浏览器扩展侧的防护

扩展装在**用户自己的 Chrome** 里，由用户从 `chrome://extensions` 加载。
它连内核走 `ws://127.0.0.1:<port>/agent-bridge`，握手要过两关：

1. **token**：`?token=<值>`，与 `api/auth.py::current_token()` 做
   `hmac.compare_digest` 比对
2. **Origin**：`chrome-extension://<id>` 必须在内核登记过的扩展 id 列表里

失败时回 `{"event":"denied","reason":"token"|"origin"}` 然后 `close(4401)`。

### 未打包扩展的 id 是按绝对路径算的

见 `browser/extensions.py::extension_id_for_path`：`sha256(绝对路径)` 前 16 字节
按 nibble 映射到 `a`–`p`，得 32 字符。**尾斜杠会改变结果。**

所以用户从别的目录加载、或通过符号链接打开，id 就会变，连接会被拒。
内核必须把**该装出来的 id** 显示出来（浏览器页），让差异无处可藏——
而握手被拒在扩展侧只表现为"没连上内核"，没有任何线索。

---

## 四、已知缺口

> 这一节是本文档存在的主要理由。上面三节讲的是"现在挡住了什么"，
> 这一节讲的是"**没挡住什么**"。

### 🔴 缺口 1：`config.yaml` 里的明文 token 进了 git（未修）

```yaml
server:
  auth:
    token: "e-ZqRKxQOKb7WuKz5dkC97typHSXArMXS7d5H9XLNy4"
platform:
  token: _csVBbp8d53wmqTfahy3kujRWBnxUbQ8-NuGeF2Zzek   # 第三方平台凭据
```

**现状**：`config.yaml` **被 git 跟踪**，且从未出现在 `.gitignore` 里。
两个凭据都出现在 3 个提交里（`1c61b59`、`dbf5059`、`b3ac32e`）。

**为什么 `enabled: false` 不代表它没用**：`resolve_token` 只看 `cfg.auth.token`
有没有值，**不看 `enabled`**。所以命令行形态认的就是这个提交进仓库的 token。

**后果**：任何拿到这份仓库（及其 git 历史）的人，都能直接调内核接口——
而内核里有能让 agent 执行本机命令的对话接口。

桌面形态不受影响（`sidecar.js` 每次启动生成的随机 token 会通过环境变量覆盖）。

**修法**（待办）：
1. `platform.token` **立刻去平台轮换**——它是第三方凭据，已经泄露
2. `config.yaml` 移出 git 跟踪 + 加进 `.gitignore`；
   真实配置改由 `config.local.yaml`（本地生成、不提交）承载
3. `resolve_token` 尊重 `enabled`：写了 `false` 就不该启用 token
4. 历史里的两个 token 视作已泄露（`auth.token` 影响限于本机回环，
   `platform.token` 需要真的轮换）

### 🟡 缺口 2：`/agent-bridge/info` 不需要认证

这个接口会返回 `connect_code`——里面就打包着**明文 token**。

- **恶意网页读不到**（CORS 挡读不挡发，响应它拿不到）——这一点是安全的
- **本机进程能读到**——落在第一节说的"不在防护范围内"里

设计上可接受（它就是给人看、给人复制的那一屏），但要明确：
**拿到 token 的门槛，就是「能向本机端口发一个 HTTP GET」。**

### 🟡 缺口 3：token 在 WebSocket URL 的查询串里

`ws://127.0.0.1:<port>/agent-bridge?token=<值>`

查询串会出现在：任何 HTTP 访问日志、反代日志、以及调试输出里。
当前形态（回环 + 无反代）风险有限，但一旦有人给它套 nginx 或做请求日志，
token 就会落盘。

替代方案是 `Sec-WebSocket-Protocol` 传 token——它不进 URL、不进访问日志。
尚未实施。

### ⚪ 缺口 4：命令行形态的 token 是"用户自己配的"

`.gitignore` 里没有 `config.yaml` 这件事（缺口 1）之外，
还有一个观念上的缺口：`config.yaml` 里的 token **必然是长期不变的**。
一份长期不变的凭据 + 一次泄露 = 永久泄露，直到用户自己想起来换。

桌面形态每次启动重生成，天然没这个问题。

---

## 五、每次内核重启都要重贴连接码

这是**设计上的有意取舍**，不是 bug。

端口随机 + token 每次启动重生成 ⇒ 用户每次重启内核后，
要在浏览器页重新复制连接码、粘进浮球。

代价是麻烦，收益是：
- 凭据不落盘（不写进任何配置文件）
- 用户不用配置任何东西，因此不会误提交进仓库
- 泄露的窗口被限制在"某一次内核运行期间"

**这个取舍不要为了方便改掉。** 相比之下，"配置 token 方便一点"的收益远小于
"凭据长期不变"的风险。

---

## 六、扩展失效是 MV3 的固有现象

改完扩展点「重新加载」之后，**所有已打开页面里旧的 content script 立刻失效**——
JS 还在跑、浮球还在画、按钮还能点，但通往 service worker 的路已经断了。

这会表现为 `Extension context invalidated`，或表现为"点下去什么都不会发生且不报错"。
这是 MV3 的机制，不是安全事件，**刷新页面即可**。
