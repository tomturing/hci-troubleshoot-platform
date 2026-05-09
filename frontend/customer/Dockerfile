# Customer前端 - 多阶段构建
FROM node:22-alpine AS build

WORKDIR /app

# 安装 pnpm 9.x（固定版本，避免未来大版本变化导致行为漂移）
RUN npm install -g pnpm@9 --registry https://registry.npmmirror.com

# 【关键】先只 COPY package.json + lock 文件，安装依赖（PIT-029）
# 源码变动不会使此缓存层失效，依赖不变时直接复用
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY shared/package.json shared/
COPY customer/package.json customer/

# --no-frozen-lockfile: lockfile 与 package.json 不一致时仍可安装（构建前 k3s-build.sh 会同步）
# 仅对白名单依赖放行 build scripts，避免 --ignore-scripts=false 全量执行生命周期脚本（供应链安全）
RUN pnpm config set onlyBuiltDependencies[0] esbuild \
    && pnpm config set onlyBuiltDependencies[1] vue-demi \
    && pnpm install --no-frozen-lockfile

# 再 COPY 源码并构建
COPY shared/ shared/
COPY customer/ customer/

# 仅运行 vite build（跳过 vue-tsc 类型检查），与原 Dockerfile 行为一致
# 类型检查在开发阶段进行，不阻塞 CI 构建
RUN pnpm --filter customer exec vite build

# 生产镜像 - Nginx 静态服务
FROM nginx:alpine
COPY --from=build /app/customer/dist /usr/share/nginx/html
COPY customer/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
