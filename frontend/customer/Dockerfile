# Customer前端 - 多阶段构建
FROM node:22-alpine AS build

WORKDIR /app

# 复制 monorepo 根文件
COPY package.json .npmrc ./
COPY shared/ shared/
COPY customer/ customer/

# 安装依赖并构建
RUN npm install && \
    cd customer && \
    node ../node_modules/vite/bin/vite.js build

# 生产镜像 - Nginx 静态服务
FROM nginx:alpine
COPY --from=build /app/customer/dist /usr/share/nginx/html
COPY customer/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
