# 前端避坑（pnpm / TypeScript / Vue）

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
