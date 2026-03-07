# Customer前端 - 多阶段构建
FROM node:22-alpine AS build

WORKDIR /app

# 【关键】先只 COPY package.json + lock 文件，安装依赖
# 源码变动不会使此缓存层失效，依赖不变时直接复用
COPY package.json .npmrc ./
COPY shared/package.json shared/
COPY customer/package.json customer/

RUN npm install

# 再 COPY 源码并构建
COPY shared/ shared/
COPY customer/ customer/

RUN cd customer && node ../node_modules/vite/bin/vite.js build

# 生产镜像 - Nginx 静态服务
FROM nginx:alpine
COPY --from=build /app/customer/dist /usr/share/nginx/html
COPY customer/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
