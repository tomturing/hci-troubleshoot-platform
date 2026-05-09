# Admin前端 - 多阶段构建
FROM node:22-alpine AS build

WORKDIR /app

# 安装 pnpm（使用国内镜像加速）
RUN npm install -g pnpm --registry https://registry.npmmirror.com

# 【关键】先只 COPY package.json + lock 文件，安装依赖（PIT-029）
# 源码变动不会使此缓存层失效，依赖不变时直接复用
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY shared/package.json shared/
COPY admin/package.json admin/

# --no-frozen-lockfile: lockfile 与 package.json 不一致时仍可安装（构建前 k3s-build.sh 会同步）
# --ignore-scripts=false: 允许 esbuild/vue-demi 等依赖运行构建脚本（pnpm v9 安全特性）
RUN pnpm install --no-frozen-lockfile --ignore-scripts=false

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
