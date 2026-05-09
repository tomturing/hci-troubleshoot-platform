# Admin前端 - 多阶段构建
FROM node:22-alpine AS build

WORKDIR /app

# 安装 pnpm 9.x（固定版本，避免未来大版本变化导致行为漂移）
RUN npm install -g pnpm@9 --registry https://registry.npmmirror.com

# 【关键】先只 COPY package.json + lock 文件，安装依赖（PIT-029）
# 源码变动不会使此缓存层失效，依赖不变时直接复用
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY shared/package.json shared/
COPY admin/package.json admin/

# --no-frozen-lockfile: lockfile 与 package.json 不一致时仍可安装（构建前 k3s-build.sh 会同步）
# 仅对白名单依赖放行 build scripts，避免 --ignore-scripts=false 全量执行生命周期脚本（供应链安全）
RUN pnpm config set onlyBuiltDependencies[0] esbuild \
    && pnpm config set onlyBuiltDependencies[1] vue-demi \
    && pnpm install --no-frozen-lockfile

# 再 COPY 源码并构建
COPY shared/ shared/
COPY admin/ admin/

RUN pnpm --filter admin build

# 生产镜像 - Nginx 静态服务
FROM nginx:alpine
COPY --from=build /app/admin/dist /usr/share/nginx/html
COPY admin/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
