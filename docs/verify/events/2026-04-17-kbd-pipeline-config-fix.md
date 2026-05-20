# KBD Pipeline 配置修复 + 自动 Port-forward

**日期**: 2026-04-17
**类型**: fix
**PR**: #173

## 问题

运行 KBD Pipeline (`PYTHONPATH=data-pipeline python -m kbd.run pipeline --ids 35694`) 时发现三个问题导致流水线失败：

1. **Vision OCR HTTP 400**
   - `.env` 配置未随 PR #143 更新，仍使用智谱 AI API
   - 代码已改为 DashScope（阿里云 Qwen），导致 API 参数不兼容
   - 错误码 1210：API 调用参数有误

2. **Pillow 未安装**
   - `kbd` extras 依赖需手动安装（`uv pip install -e ".[kbd]"`）
   - 运行时缺少背景色检测功能，全部返回"其他"

3. **kb-service 不可达**
   - k3s 集群内 ClusterIP 服务本地脚本无法直接访问
   - 需手动启动 `kubectl port-forward`

## 修复

### 1. `.env.example` 配置更新（PR #143 对齐）

```diff
- ZAI_BASE_URL=https://api.z.ai/v1
- VISION_MODEL=gpt-4o
+ ZAI_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
+ VISION_MODEL=qwen3.5-plus
+ ANALYSIS_MODEL=qwen3-max-2026-01-23
+ CLASSIFY_MODEL=qwen3.5-plus
```

### 2. Pillow 移入主依赖

从 `[project.optional-dependencies] kbd` 移到主 `dependencies`，`uv sync` 自动安装。

### 3. importer.py 添加自动 port-forward 检测

新增函数：
- `ensure_kb_service_reachable()` — 主入口，自动检测并启动 port-forward
- `_check_kb_service_reachable()` — 快速检测服务可达性
- `_start_port_forward()` / `_stop_port_forward()` — 进程管理

工作流程：
1. 检测 kb-service 是否已可达（可能是已有 port-forward）
2. 检查残留 PID 文件，复用或清理僵死进程
3. 必要时启动 `kubectl port-forward svc/kb-service -n hci-dev 8004:8004`
4. PID 记录到 `.kb-service-portforward.pid` 文件

## 影响文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `pyproject.toml` | 修改 | Pillow 移入主依赖 |
| `data-pipeline/kbd/.env.example` | 修改 | 更新 DashScope 配置示例 |
| `data-pipeline/kbd/importer.py` | 新增功能 | 自动 port-forward 检测 |
| `uv.lock` | 自动更新 | 依赖锁定 |

## 后续清理

PR 合并后需清理：
- 残留的 `.kb-service-portforward.pid` 文件
- 临时分支 `fix/kbd-pipeline-config-and-portforward`