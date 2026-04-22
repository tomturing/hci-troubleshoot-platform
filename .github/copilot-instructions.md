# GitHub Copilot 指令

本文件为 GitHub Copilot CLI 和 VS Code Copilot 提供项目级指令。

---

## Git Commit/PR 标识规则（强制）

**所有 commit 消息末尾必须追加 `[env:<环境>:<hostname>][agent:copilot]` 标识。**

**所有 PR 必须添加对应的 labels：`env:<环境>:<hostname>` 和 `agent:copilot`。**

格式：
```
<commit message>

[env:<环境>:<hostname>][agent:copilot]
```

示例：
```
fix: 修复 ArgoCD 升级脚本

[env:dev:gs][agent:copilot]
```

### 强制执行流程

填写 `[env:<环境>:<hostname>]` 前，**必须先执行**以下命令获取当前值：

```bash
# 步骤 1：获取环境（dev/staging/prod）
kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'

# 步骤 2：获取 hostname
hostname | tr '[:upper:]' '[:lower:]'
```

**禁止使用记忆中的值、假设值或旧对话中的值**。每次都必须重新执行命令验证。

### 实现方式

使用 `gcm` 和 `gpr` 函数（已配置在 `~/.my_custom_configs`）：

```bash
# GitHub Copilot 提交 commit（必须显式指定 AGENT）
AGENT=copilot gcm "feat: 新功能"

# GitHub Copilot 创建 PR（必须显式指定 AGENT）
AGENT=copilot gpr "feat: 新功能"
```

⚠️ **注意**：`gpr` 默认 `AGENT=claude`，Copilot 必须显式加 `AGENT=copilot` 前缀，否则标签打错。

---

## 编码规范

- 代码注释**必须使用**中文
- Git commit 消息**必须使用**中文
- Python 环境管理: **必须使用** `uv`
- 前端包管理: **必须使用** `pnpm`

---

## 避坑指南

在编写或审查代码前，必须先读取相关避坑指南：
- 首先读取 `docs/pitfalls/_index.md`
- 按场景选择具体文件（shell/python/frontend/k8s 等）

---

## 更多规范

详见 `AGENTS.md`（项目级完整规范）和 `~/.claude/CLAUDE.md`（全局规范）。
