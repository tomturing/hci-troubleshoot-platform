#!/bin/bash

# 这个脚本创建项目的所有必要文件

echo "Creating project structure and files..."

# 创建 .gitignore
cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
.venv/
venv/
ENV/
env/

# Node
node_modules/
dist/
.pnpm-store/
.DS_Store

# IDE
.vscode/
.idea/
*.swp
*.swo
*.swn

# Env
.env
.env.local
.env.*.local

# Logs
*.log
logs/

# Database
*.db
*.sqlite

# OS
.DS_Store
Thumbs.db

# Coverage
.coverage
htmlcov/
.pytest_cache/
EOF

# 创建环境变量模板
cat > .env.example << 'EOF'
# Database
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=hci_troubleshoot
POSTGRES_USER=hci_admin
POSTGRES_PASSWORD=change_me_in_production

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# Services
API_GATEWAY_PORT=8000
CASE_SERVICE_PORT=8001
CONVERSATION_SERVICE_PORT=8002
SCHEDULER_SERVICE_PORT=8003

# OpenClaw
OPENCLAW_POD_IMAGE=openclaw:latest
OPENCLAW_POD_PORT=8080
OPENCLAW_WARM_POOL_SIZE=3
OPENCLAW_MAX_POOL_SIZE=10

# Zhipu AI
ZHIPU_API_KEY=your_zhipu_api_key_here
ZHIPU_MODEL=glm-4

# Kubernetes
K8S_NAMESPACE=hci-troubleshoot
K8S_CONTEXT=default

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json

# CORS
CORS_ORIGINS=http://localhost:5173,http://localhost:3000

# Session
SESSION_EXPIRE_HOURS=24
EOF

# 创建 Docker Compose
cat > docker-compose.yml << 'EOF'
version: '3.8'

services:
  postgres:
    image: postgres:15
    container_name: hci-postgres
    environment:
      POSTGRES_DB: hci_troubleshoot
      POSTGRES_USER: hci_admin
      POSTGRES_PASSWORD: dev_password_123
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./database/init_schema.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U hci_admin -d hci_troubleshoot"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7
    container_name: hci-redis
    command: redis-server --appendonly yes
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  api-gateway:
    build:
      context: ./backend/api-gateway
      dockerfile: Dockerfile
    container_name: hci-api-gateway
    ports:
      - "8000:8000"
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      POSTGRES_DB: hci_troubleshoot
      POSTGRES_USER: hci_admin
      POSTGRES_PASSWORD: dev_password_123
      REDIS_HOST: redis
      REDIS_PORT: 6379
      CASE_SERVICE_URL: http://case-service:8001
      CONVERSATION_SERVICE_URL: http://conversation-service:8002
      SCHEDULER_SERVICE_URL: http://scheduler-service:8003
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - ./backend/api-gateway:/app
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  case-service:
    build:
      context: ./backend/case-service
      dockerfile: Dockerfile
    container_name: hci-case-service
    ports:
      - "8001:8001"
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      POSTGRES_DB: hci_troubleshoot
      POSTGRES_USER: hci_admin
      POSTGRES_PASSWORD: dev_password_123
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./backend/case-service:/app
    command: uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

  conversation-service:
    build:
      context: ./backend/conversation-service
      dockerfile: Dockerfile
    container_name: hci-conversation-service
    ports:
      - "8002:8002"
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      POSTGRES_DB: hci_troubleshoot
      POSTGRES_USER: hci_admin
      POSTGRES_PASSWORD: dev_password_123
      REDIS_HOST: redis
      REDIS_PORT: 6379
      ZHIPU_API_KEY: ${ZHIPU_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - ./backend/conversation-service:/app
    command: uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload

  scheduler-service:
    build:
      context: ./backend/scheduler-service
      dockerfile: Dockerfile
    container_name: hci-scheduler-service
    ports:
      - "8003:8003"
    environment:
      POSTGRES_HOST: postgres
      REDIS_HOST: redis
      K8S_NAMESPACE: hci-troubleshoot
    depends_on:
      redis:
        condition: service_healthy
    volumes:
      - ./backend/scheduler-service:/app
      - /var/run/docker.sock:/var/run/docker.sock  # For local dev
    command: uvicorn app.main:app --host 0.0.0.0 --port 8003 --reload

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile.dev
    container_name: hci-frontend
    ports:
      - "5173:5173"
    volumes:
      - ./frontend:/app
      - /app/node_modules
    command: pnpm dev --host

volumes:
  postgres_data:
  redis_data:
EOF

echo "Project files created successfully!"
echo ""
echo "Next steps:"
echo "1. Copy .env.example to .env and update values"
echo "2. Run: docker-compose up -d"
echo "3. Access frontend at: http://localhost:5173"
echo "4. Access API docs at: http://localhost:8000/docs"

