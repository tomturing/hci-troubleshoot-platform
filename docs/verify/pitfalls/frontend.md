# 前端避坑（pnpm / TypeScript / Vue）

## V-007：单对象删除不得携带无关草稿，校验错误必须保留稳定定位

**症状：** 用户恢复或编辑多条 Signal 后，删除其中一条却因另一条无效草稿失败；错误只显示 `None is not valid under anyOf`，无法知道哪条 Signal、哪个字段需要修改。

**根因：** 页面把服务端权威文档与未保存本地草稿合并成同一个 `signalList`，删除时以整份 PATCH 模拟单对象意图；同时把后端结构化校验降级成字符串 toast，丢失稳定 ID 和字段路径。

**修复：** 本地新增/恢复对象删除只撤销本地状态；已持久化对象只提交稳定 `delete_signal_id`，由服务端作用于权威工作稿。校验错误保留 `signal_id/field_path/location/message/action`，页面按稳定 ID 展开、滚动并高亮对应卡片。

**预防：**

- 任何 delete/approve/retry 等单对象动作均审查请求体是否夹带无关编辑态；
- 服务端错误契约包含机器定位与用户文案，前端不得只取 `String(error)`；
- 测试至少包含“一条无效未保存草稿 + 删除另一条持久化对象”和“筛选/排序后仍按稳定 ID 定位”反例。

## PIT-005：pnpm workspace 下子包未声明依赖直接引用

子包使用其他子包的代码时，必须在 `package.json` 中显式声明 `workspace:*` 依赖，否则 pnpm strict 模式下构建失败。

## PIT-023：SPA 部署在子路径时 vite base 和 Vue Router base 必须同步配置

**现象：** admin-ui 通过 `/admin` 子路径访问时页面空白，nginx 返回 200 但 JS/CSS 资源 404 或加载了 customer-ui 的资源。

**根因：** Traefik Ingress 不剥离路径前缀（`pathType: Prefix /admin` 原样转发 `/admin/*`）。若 `vite.config.ts` 的 `base: '/'`，打包后的 `index.html` 引用 `/assets/index.js`（绝对路径），浏览器请求 `/assets/` 向上路由到 Traefik → 命中 `/`（customer-ui）→ 404，白屏。

**修复（三处必须同时修改）：**
1. `vite.config.ts`: `base: '/admin/'`
2. `src/router/index.ts`: `createWebHistory('/admin/')`
3. `nginx.conf`: 用 `alias` 映射请求路径到文件系统根：
```nginx
location /admin/ {
    alias /usr/share/nginx/html/;
    index index.html;
    try_files $uri $uri/ /index.html;  # fallback 指向根路径 /index.html
}
location = /admin { return 301 /admin/; }
location / { try_files $uri $uri/ /index.html; }
```

**注意 `try_files` 的 fallback 写 `/index.html` 而非 `/admin/index.html`**，否则 nginx 循环重定向 → 500。

## PIT-025：nginx 未设置 HTML no-cache 导致路由变更后浏览器使用旧缓存

**现象：** 服务端路由已修复（如 `/grafana` 不再指向 customer-ui），但浏览器仍显示旧内容；强制刷新（Ctrl+Shift+R）后恢复正常。

**根因：** nginx 默认没有对 HTML 文件设置 `Cache-Control: no-store`，浏览器会缓存 HTML 及其内嵌的 iframe src。当 Traefik 路由规则短暂错误时，浏览器缓存了错误响应，即使后端修复也不会失效。

**修复：** 在 nginx.conf 的所有 `location` 中对 HTML 响应加头：
```nginx
location /admin/ {
    alias /usr/share/nginx/html/;
    add_header Cache-Control "no-store, no-cache, must-revalidate" always;
    try_files $uri $uri/ /index.html;
}
location / {
    add_header Cache-Control "no-store, no-cache, must-revalidate" always;
    try_files $uri $uri/ /index.html;
}
```
静态 JS/CSS 资源（带 hash）仍可走长期缓存，只需对根 HTML 设置 no-store。

**诊断：** DevTools → Network → 查看 index.html 响应头是否有 `Cache-Control: no-store`。

## PIT-028：Clash TUN 环境下 Docker build 中 npm install 超时

**现象：** `docker build` 执行 `npm install` 时报错：
```
npm error: connect ETIMEDOUT 198.18.0.19:443
npm error network request to https://registry.npmjs.org/... failed
```
或使用国内镜像 `registry.npmmirror.com` 同样报 `ETIMEDOUT 198.18.0.4:443`。

**根因：** Docker 构建容器使用独立网络命名空间，Clash TUN 的系统代理**不覆盖**容器内流量，
容器直接走真实网络，而 Clash TUN 模式下 DNS 将所有域名劫持到 `198.18.x.x` 虚拟 IP，导致容器内无法访问任何外网地址。

**修复：加 `--network host` 参数，让构建容器使用宿主机网络（走 Clash 代理）：**
```bash
docker build --network host -t <image>:<tag> -f <Dockerfile> <context>
```

**注意：**
- `--network host` 在 Linux 上完全生效；macOS/Windows Docker Desktop 受限，效果不同
- 同理适用于任何在 Clash TUN 宿主机上的 `docker build` + `npm/pip/apt` 网络请求

## PIT-029：前端 Dockerfile layer 顺序错误导致每次全量 npm install

**现象：** 每次修改任何源码（哪怕只改一行 Vue）都要重跑 `npm install`，构建 8-15 分钟。

**根因：** 原 Dockerfile 先 `COPY shared/ + COPY admin/`（源码），再 `RUN npm install`。
源码任何改动都会使 `npm install` 层缓存失效，触发全量安装。

**修复：把 `package.json` 的 COPY 和 `npm install` 单独作为一层（在源码 COPY 之前）：**
```dockerfile
# ✅ 正确顺序
WORKDIR /app
COPY package.json .npmrc ./          # 依赖声明文件
COPY shared/package.json shared/
COPY admin/package.json admin/
RUN npm install                      # 只要 package.json 不变，永远命中缓存

COPY shared/ shared/                 # 源码变动只触发 vite build（约 5 秒）
COPY admin/ admin/
RUN cd admin && node ../node_modules/vite/bin/vite.js build
```

**效果：** 依赖不变时，`npm install` 层完全跳过，从 8 分钟降至 **~20 秒**（仅 vite build）。

## V-001：pnpm v9 默认禁止依赖构建脚本导致 Docker 构建失败

**现象：** CI构建失败，`pnpm install` 报错：
```
[ERR_PNPM_IGNORED_BUILDS] Ignored build scripts: esbuild@0.25.12, vue-demi@0.14.10
Run "pnpm approve-builds" to pick which dependencies should be allowed to run scripts.
```

**根因：** pnpm v9 引入新安全机制，默认禁止依赖包运行构建脚本（如 esbuild 的 native binary 编编）。这些构建脚本对某些依赖是必需的，禁止后会导致安装失败。

**修复：** 使用白名单机制仅放行必需依赖，并固定 pnpm 版本：
```dockerfile
# 固定 pnpm 9.x 版本，避免未来大版本变化导致行为漂移
RUN npm install -g pnpm@9 --registry https://registry.npmmirror.com

# 仅对白名单依赖放行 build scripts，避免全量执行生命周期脚本（供应链安全）
RUN pnpm config set onlyBuiltDependencies[0] esbuild \
    && pnpm config set onlyBuiltDependencies[1] vue-demi \
    && pnpm install --no-frozen-lockfile
```

**注意：**
- 此问题仅影响 pnpm v9+，pnpm v8 无此安全特性
- `--ignore-scripts=false` 会允许**所有**依赖执行生命周期脚本，扩大供应链攻击面，不推荐
- 白名单机制更安全，仅放行必需的 esbuild/vue-demi 等依赖

## V-002：大命令输出不能在浏览器/HTTP 之后才截断

**现象：** 远端命令已经 exit 0，但页面长期显示“正在等待输出”；terminal_bridge 有 `exec.done`，API Gateway / conversation-service 却没有对应 `/exec-result`。浏览器内存、CPU 或主线程占用异常。

**根因：** 后端截断发生在浏览器提交 `/exec-result` 之后，无法保护前置 WebSocket、JSON 解析和 HTTP。若前端按 4 KiB 分块使用 `buffer += chunk` 拼接几十 MB 输出，还会产生 O(n²) 字符串复制；bridge 最后再发送一份完整结果会进一步放大峰值。尤其要注意旧协议的聚合字段 `output`：即使 stdout/stderr 已筛选，若 `output` 仍保留原文，它会作为第三份副本进入 HTTP 并使 Gateway OOM。

**修复：** 优先用原生命令参数在数据源缩小结果集；仍需选行时，把平台定义的字面量 `output_filters` 前移到 terminal_bridge，在本机逐行筛选。只回传命中行，stdout/stderr 共用 256 KiB 总预算，超限返回稳定错误并 Fail Closed。Customer UI 使用分块数组和 remainder 处理跨 chunk 行边界，并为无法原子升级的旧 bridge 保留相同规则的兼容筛选。只要存在物理流，HTTP 兼容字段 `output` 必须由筛选后的 stdout/stderr 重建，禁止沿用 bridge 聚合值。

**预防：**

- 禁止以“后端最终会截断”为理由放行无界原始输出。
- 审查最终 payload 的全部输出字段；stdout/stderr/output 任一字段都不能残留未筛选副本。
- Gateway 必须在 JSON 解析前执行请求体硬限制；业务模型的字段长度校验不能替代传输层限制。
- 新筛选协议只能表达 source/include/exclude/all|any/case_sensitive，不能表达 shell、正则、grep、awk 或管道。
- 自动测试必须覆盖跨 chunk 关键字、无换行最后一行、stdout/stderr 共享上限、筛选后仍超限以及 SSE 中断后 running 卡片收敛。
- 现场验证同时检查 bridge 的 raw/filtered 字节数和 `/exec-result` 到达情况，不能只看命令退出码。

## V-003：展示序号不能作为跨层业务身份

**现象：** 用户点击页面③项，后端却处理了另一个对象；过滤一个非法项后错位更容易出现。

**根因：** `①②③` / 数组下标是渲染位置，会随过滤、排序、分页和版本改变。将它当作跨 UI、API、数据库和 Agent 的业务键，必然产生 TOCTOU/错位问题。

**修复：**

- 选项必须携带稳定领域键，如 `category_code`、UUID 或不可变记录 ID。
- 前端可按当前数组位置显示①②③，但回传 metadata 必须使用稳定键。
- 兼容历史序号时，必须保留并匹配原 optionId，过滤后不得重排。
- 消费端必须再校验稳定键属于当前候选和权威目录，未命中时 Fail Closed。

**预防测试：** 回归数据必须包含“前置非法项被过滤”和“排序改变”，不能只测试全部合法、连续编号的 happy path。

## V-005：普通 Markdown 代码块不能取得 Agent 自动执行权限

**现象**：S0 或普通 Assistant 消息返回一个 `bash` fenced code block 后，Customer UI 将其渲染成 `CommandBlock`；当用户偏好为 Aggressive 且 SSH 已连接时，倒计时结束后通过 `ssh_input` 把展示文本当作真实命令执行。页面看到的“输出”可能来自模型伪造，实际 HCI 执行的是伪造输出文本本身。

**根因**：把不可信自然语言展示层与受信控制面混为一体。Markdown 没有服务端签名、`exec_id`、工具定义、风险判定、授权事务和 trace 上下文，风险关键词推断无法把它升级为可信执行指令。

**修复**：普通 Markdown 命令块仅允许复制和用户显式发送到人工终端。Agent 自动执行只消费服务端产生的 `agent_exec_command` 结构化事件，并沿 `ssh_exec_process` 通道执行；禁止 Markdown 使用 `ssh_input` 自动执行，禁止把执行结果伪装为新的 `role=user` 消息回灌。

**预防**：

- 前端负向测试必须在 Aggressive、SSH connected、推进虚拟时钟的条件下断言 WebSocket/旧执行器调用次数为 0；
- S0 没有工具能力，必须在调用 LLM 前拒绝明确执行请求，并在输出前阻断 fenced shell、退出码和“执行结果”等无工具证据；
- 端到端验收同时检查 SSE 类型、WebSocket 消息类型、Bridge counter、Artifact 和 Trace；出现 `ssh_input` 不能算 Agent 工具链成功。
