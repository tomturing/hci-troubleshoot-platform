---
status: completed
category: solution
audience: developer
last_updated: 2026-07-13
owner: team
---

# terminal_bridge.exe 下载失败修复方案

## 背景

同步上游 main 最新代码（b01d266 -> 4c2d507）后，前端客户侧 `terminal_bridge.exe` 下载功能失效：浏览器访问 `/downloads/terminal_bridge.exe` 返回 404，`frontend/customer/public/downloads/` 目录下仅剩 `.gitkeep` 和 `index.html`，二进制可执行文件缺失。

terminal_bridge 是 HCI 排障助手的本地 SSH Bridge（Go 编译，无运行时依赖），客户浏览器通过 `ws://localhost:9999` 与本地 terminal_bridge.exe 通信，公网服务器不参与 SSH 流量。该文件缺失会直接阻断客户侧终端功能。

## 根因（两层问题）

### 1. PR #509 误删二进制文件
commit `bf808c0`（PR #509 "feat: KBD分类与识图功能及UI布局修复"）删除了以下文件：
- `frontend/customer/public/downloads/terminal_bridge.exe`（2191872 bytes）
- `terminal_bridge/terminal_bridge.exe`
- `terminal_bridge/terminal_bridge_darwin_amd64`

而 `terminal_bridge/` 目录下的产物已重命名为按架构区分：
- `terminal_bridge_x86.exe`（2191872 bytes，Windows amd64，与被删文件 md5 一致）
- `terminal_bridge_arm64.exe`（Windows ARM64）
- `terminal_bridge_darwin_arm64`（macOS ARM64）

但未将新文件复制到 `frontend/customer/public/downloads/`，且无 CI 流程注入 exe，导致下载路径空缺。

### 2. 根目录 .gitignore 规则误伤（长期隐患）
根目录 `.gitignore:10` 有 Python 构建产物忽略规则 `downloads/`，该规则匹配**任意路径下的 `downloads/` 目录**，包括 `frontend/customer/public/downloads/`。

git 的行为是：**若整个目录被忽略，git 不会进入该目录，目录内文件的白名单无效**。因此 `frontend/customer/.gitignore` 中的白名单 `!public/downloads/terminal_bridge.exe` **从未生效**，这是 PR #509 之前就存在的隐患。

## 修复方案

### 1. 恢复二进制文件
将 `terminal_bridge/terminal_bridge_x86.exe`（Windows amd64，主流架构）复制为 `frontend/customer/public/downloads/terminal_bridge.exe`。前端 3 处引用（`App.vue`、`CaseCreateDialog.vue`、`SshConnectDialog.vue`）均通过 `/downloads/terminal_bridge.exe` 下载，恢复后立即生效。Docker 构建时 `COPY frontend/customer/` 会自动包含该文件。

### 2. 修复 .gitignore 白名单失效
参照根目录 `.gitignore` 中 `deploy/env/` 的既有例外模式（第 26-28 行），在 `downloads/` 规则后添加目录级例外：

```gitignore
downloads/
# 例外：frontend/customer/public/downloads/ 是 terminal_bridge 客户端分发目录，需提交
!frontend/customer/public/downloads/
```

这样 git 会进入该目录，`frontend/customer/.gitignore` 的白名单 `!public/downloads/terminal_bridge.exe` 才能正常生效，确保 exe 文件可被 Git 跟踪。

## 影响文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `.gitignore` | 修改 | 添加 `!frontend/customer/public/downloads/` 例外 |
| `frontend/customer/public/downloads/terminal_bridge.exe` | 新增 | 从 `terminal_bridge/terminal_bridge_x86.exe` 复制，Windows amd64 |

## 验证

- `git check-ignore -v` 确认文件不再被忽略（匹配白名单 `!public/downloads/terminal_bridge.exe`）
- `git status` 确认文件显示为未跟踪（可 add）
- md5 校验：`91f1c99c67da43682fc1b8c2d29ecd98`（与源文件一致）
- 文件类型：PE32 executable (GUI) Intel 80386, for MS Windows, UPX compressed
