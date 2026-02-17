#!/bin/bash

# 创建后端目录结构
mkdir -p backend/api-gateway/{app,tests}
mkdir -p backend/case-service/{app,tests}
mkdir -p backend/conversation-service/{app,tests}
mkdir -p backend/scheduler-service/{app,tests}
mkdir -p backend/shared/{models,utils,database}

# 创建前端目录结构
mkdir -p frontend/{src,public}
mkdir -p frontend/src/{components,views,stores,api,types,router}

# 创建部署配置目录
mkdir -p deploy/{docker,k8s,scripts}

# 创建数据库目录
mkdir -p database/{migrations,seeds}

# 创建文档目录 (已存在)
mkdir -p docs

# 创建测试目录
mkdir -p tests/{integration,e2e}

echo "Project structure created successfully"
