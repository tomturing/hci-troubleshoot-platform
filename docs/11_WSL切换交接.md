# WSL 切换交接（2026-02-28）

## 1. 已推送的代码（可直接 pull）

- 分支：`master`
- 最新已推送 commit：`114d069`
- 提交说明：`fix: repair multi-assistant routing and scheduler endpoint flow`
- 这笔提交已包含：多助手路由主链路修复（conversation/scheduler/api-gateway/frontend 联动）

## 2. 当前工作区未提交内容（本地保留）

```text
 M .env.example
 M README.md
 M docs/07_测试指南.md
?? docs/10_多助手接入评估.md
?? scripts/wsl_quickstart.sh
?? test_multi_assistant.sh
```

说明：
- 这些是“文档 + 联调脚本 + WSL 快启脚本”改动，尚未提交。
- WSL 访问同一份文件时会看到相同状态。

## 3. 多助手链路当前状态

- 已完成：`openclaw + zeroclaw` 的代码接入主链路（已在 commit `114d069`）。
- 待完成：真实运行态联调（依赖 OpenClaw/ZeroClaw 进程可用 + 配置 token）。

## 4. 进入 WSL 后建议第一步

```bash
cd /mnt/d/AIBot/hci-troubleshoot-platform
git status -sb
```

如果要走一键初始化：

```bash
bash scripts/wsl_quickstart.sh
```

## 5. 进入 Codex 后建议直接给的第一句

```text
请先阅读 docs/11_WSL切换交接.md，然后继续完成 openclaw+zeroclaw 真实链路联调，优先把 .env 的 ASSISTANT_REGISTRY_JSON 和 zeroclaw pairing token 配好，再跑 test_multi_assistant.sh。
```

