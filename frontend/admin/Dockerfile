# Admin前端 - 多阶段构建
FROM node:22-alpine AS build

WORKDIR /app

# 安装 pnpm 9.x（固定版本，避免未来大版本变化导致行为漂移）
RUN npm install -g pnpm@9 --registry https://registry.npmmirror.com

# 【关键】先只 COPY package.json + lock 文件，安装依赖（PIT-029）
# 源码变动不会使此缓存层失效，依赖不变时直接复用
COPY frontend/package.json frontend/pnpm-workspace.yaml frontend/pnpm-lock.yaml frontend/.npmrc ./
COPY frontend/shared/package.json shared/
COPY frontend/admin/package.json admin/

# --no-frozen-lockfile: lockfile 与 package.json 不一致时仍可安装（构建前 k3s-build.sh 会同步）
# 仅对白名单依赖放行 build scripts，避免 --ignore-scripts=false 全量执行生命周期脚本（供应链安全）
RUN pnpm config set onlyBuiltDependencies[0] esbuild \
    && pnpm config set onlyBuiltDependencies[1] vue-demi \
    && pnpm install --no-frozen-lockfile

# 再 COPY 源码并构建
COPY frontend/shared/ shared/
COPY frontend/admin/ admin/

RUN pnpm --filter admin build

# 生产镜像 - Nginx 静态服务
FROM nginx:alpine

# 默认保持原始浏览器身份头；Docker Compose（本地容器编排）可在服务端覆盖为开发身份。
# 值中的 Nginx 变量经过 envsubst 单次替换后保留给 Nginx 请求阶段解析。
ENV ADMIN_API_AUTHORIZATION=\$http_authorization \
    ADMIN_API_TENANT_ID=\$http_x_tenant_id \
    ADMIN_API_ACTOR_ID=\$http_x_actor_id

COPY --from=build /app/admin/dist /usr/share/nginx/html
COPY frontend/admin/nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
