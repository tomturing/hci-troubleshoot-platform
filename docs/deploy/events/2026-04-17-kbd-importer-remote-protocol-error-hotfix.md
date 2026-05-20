# KBD Importer RemoteProtocolError Hotfix

## 问题背景

运行 `uv run PYTHONPATH=data-pipeline python -m kbd.run pipeline --ids 35694 --override` 时，
`_check_kb_service_reachable()` 函数抛出 `httpx.RemoteProtocolError: Server disconnected without sending a response` 异常。

## 根因分析

`_check_kb_service_reachable()` 函数捕获的异常类型不完整：

```python
# 原代码
except (httpx.ConnectError, httpx.TimeoutException, OSError):
    return False
```

缺少对 `httpx.RemoteProtocolError` 的捕获。当服务端断开连接（如 port-forward 未启动时连接 ClusterIP 服务）会抛出此异常。

## 修复方案

在异常捕获列表中添加 `httpx.RemoteProtocolError`：

```python
except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError):
    return False
```

## 影响文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `data-pipeline/kbd/importer.py` | Bug fix | 添加 RemoteProtocolError 捕获 |

## 关联功能

此 hotfix 修复的是 v4.10 引入的自动 port-forward 检测功能中的异常处理遗漏。

---

[env:dev:gs][agent:claude]