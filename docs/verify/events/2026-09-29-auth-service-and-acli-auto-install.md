# auth-service 容器配置与 aCLI 自动安装功能

> **修复日期**: 2026-09-29  
> **PR**: #1104  
> **影响环境**: 本地开发环境 (docker-compose)

---

## 📋 问题描述

### 1. 管理控制台无法登录

**现象**：
- 本地环境启动后，访问管理控制台 http://localhost:3002/ 登录时报错：`认证服务暂时不可用`
- API 网关日志显示：`auth-service 请求失败 ConnectError`

**根因**：
- `deploy/docker/docker-compose.yml` 中缺少 `auth-service` 服务配置
- API 网关尝试连接 `http://auth-service:8007` 但容器不存在

### 2. 创建工单时 aCLI 未安装无法自动安装

**现象**：
- 创建工单连接设备时，如果目标主机没有安装 aCLI，系统只是跳过环境采集
- 用户需要手动安装 aCLI 后才能使用环境采集功能

**根因**：
- `frontend/customer/src/stores/chat.ts` 的 `connectSSHAndCreateCase()` 函数中，检测到 aCLI 未安装时只是设置标志并跳过
- 没有调用已有的 `ensureAcliReady()` 自动安装功能

---

## 🔧 修复方案

### 1. 添加 auth-service 容器配置

**文件**: `deploy/docker/docker-compose.yml`

在 `api-gateway` 和 `case-service` 之间添加 `auth-service` 服务：

```yaml
auth-service:
  build:
    context: ../..
    dockerfile: backend/auth-service/Dockerfile
  container_name: hci-auth-service
  environment:
    PYTHONPATH: /app
    DATABASE_URL: ${DATABASE_URL:-postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot}
    REDIS_URL: ${REDIS_URL:-redis://redis:6379/0}
    INTERNAL_API_TOKEN: ${INTERNAL_API_TOKEN:-hci-dev-internal-token}
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy
  volumes:
    - ../../backend/auth-service:/app
    - ../../backend/shared:/app/shared
  command: uvicorn app.main:app --host 0.0.0.0 --port 8007 --reload
  networks:
    - hci-troubleshoot-platform_default
```

**作用**：
- 提供管理控制台登录认证功能
- 签发 JWT token 供前端使用
- 依赖 postgres 和 redis 健康检查

### 2. 实现 aCLI 自动安装功能

**文件**: `frontend/customer/src/stores/chat.ts`

**修改内容**：

1. 更新 `sshCreationPhase` 类型定义（行 2584）：
   ```typescript
   const sshCreationPhase = ref<'idle' | 'connecting' | 'connected' | 'acli_check' | 'acli_installing' | 'collecting' | 'done' | 'error' | 'acli_not_found'>('idle')
   ```
   新增 `'acli_installing'` 状态

2. 在 `connectSSHAndCreateCase()` 函数中添加自动安装逻辑（行 2921-2976）：
   ```typescript
   if (acliCheckResult.output.includes('__HCI_ACLI_MISSING__')) {
     appendSshCreationLog('warn', 'acli', '目标主机未安装 acli，尝试自动安装...')
     sshCreationPhase.value = 'acli_installing'

     // 自动调用 acli_sync 安装 aCLI
     try {
       const installResult = await ensureAcliReady(
         socket,
         'ssh-create-temp',
         (progress) => {
           appendSshCreationLog('info', 'acli', progress.text, { status: progress.status })
         },
         { force: false },
       )

       if (installResult.status === 'installed' || installResult.status === 'up_to_date') {
         acliAvailable.value = true
         sshCreationPhase.value = 'collecting'
         appendSshCreationLog('info', 'acli', 'acli 安装成功，开始采集环境数据', {
           version: installResult.version,
           architecture: installResult.architecture,
         })

         // 安装成功后继续采集环境数据
         for (const command of COLLECT_COMMANDS) {
           const result = await runBridgeCommand(command.label, command.cmd)
           if (result.exitCode !== 0) {
             throw buildCommandError(command.label, result.output, result.exitCode)
           }
           collectBuffer[command.name] = result.output
         }

         await submitCollectedData(caseId, collectBuffer)
         await collectEnvironmentData(caseId)
       } else {
         acliAvailable.value = false
         sshCreationPhase.value = 'acli_not_found'
         appendSshCreationLog('error', 'acli', `acli 安装失败: ${installResult.message || installResult.status}`)
       }
     } catch (installError) {
       acliAvailable.value = false
       sshCreationPhase.value = 'acli_not_found'
       appendSshCreationLog('error', 'acli', `acli 自动安装异常: ${installError instanceof Error ? installError.message : String(installError)}`)
     }
   }
   ```

**架构说明**：

```
本地设备 (运行 terminal_bridge)
  ↓ ① 从 http://acli.sangfor.com.cn:1110 下载安装包
  ↓    - x86_64: ?architecture=x86_64
  ↓    - aarch64: ?architecture=aarch64
  ↓ ② 通过 SSH 上传到远程设备
  ↓    - 目标路径: /sf/data/local/
  ↓    - 权限: 0755
远程 HCI 设备 (不需要访问外网)
  ↓ ③ 接收安装包
  ↓ ④ 执行安装: ./xxx.acli --install --force
  ↓ ⑤ 验证安装: acli --version
```

**网络要求**：
- ✅ 本地设备（运行 terminal_bridge）需要能访问 `http://acli.sangfor.com.cn:1110`
- ✅ 远程 HCI 设备不需要能访问外网

---

## ✅ 验证步骤

### 1. 验证 auth-service 容器

```bash
# 启动本地环境
cd deploy/docker
docker-compose up -d

# 验证 auth-service 运行状态
docker ps | grep auth-service

# 访问管理控制台
open http://localhost:3002/

# 使用默认账号登录
# 账号: admin
# 密码: aihci@aclient2025
```

### 2. 验证 aCLI 自动安装

1. 准备一台未安装 aCLI 的 HCI 设备
2. 创建工单，填写 SSH 连接信息
3. 观察到 UI 显示 "acli_installing" 状态
4. 查看日志显示安装进度：
   - "正在检测 HCI 硬件架构与当前 acli 状态..."
   - "正在从官方源下载 acli 安装包..."
   - "正在流式上传安装包至 /sf/data/local/..."
   - "正在执行 acli 集群分发安装..."
5. 验证安装成功后自动采集环境数据
6. 验证工单创建流程正常完成

---

## 📊 影响范围

| 组件 | 影响 | 说明 |
|------|------|------|
| **本地开发环境** | ✅ 直接影响 | 需要重新启动 docker-compose |
| **管理控制台** | ✅ 直接影响 | 现在可以正常登录 |
| **工单创建流程** | ✅ 间接影响 | 未安装 aCLI 的设备会自动安装 |
| **terminal_bridge** | ✅ 已有功能 | 无需修改，已有完整的 acli_sync 实现 |

---

## 📝 相关文件

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `deploy/docker/docker-compose.yml` | 修改 | 添加 auth-service 服务配置 |
| `frontend/customer/src/stores/chat.ts` | 修改 | 实现 aCLI 自动安装逻辑，新增 'acli_installing' 状态 |
| `terminal_bridge/main.go` | 无修改 | 已有完整的 handleAcliSync 实现 |
| `frontend/customer/src/services/acliManager.ts` | 无修改 | 已有 ensureAcliReady() 封装 |

---

## 🔗 相关文档

- **aCLI 下载地址**（已配置在 terminal_bridge 代码中）：
  - x86_64: `http://acli.sangfor.com.cn:1110/api/public/download/4jotgyfk/latest?architecture=x86_64&api_key=QRlZXabZ8sOrFKtgs1VIfkHJZYQ3QNrcETXgsJaiX9Svz1pjFj`
  - aarch64: `http://acli.sangfor.com.cn:1110/api/public/download/4jotgyfk/latest?architecture=aarch64&api_key=QRlZXabZ8sOrFKtgs1VIfkHJZYQ3QNrcETXgsJaiX9Svz1pjFj`

- **默认管理员账号**（数据库种子数据）：
  - 账号: `admin`
  - 密码: `aihci@aclient2025`
  - 位置: `database/seeds/05_authn_default_admin.sql`

---

**修复完成时间**: 2026-09-29  
**PR**: #1104  
**状态**: ✅ 已完成并合并
