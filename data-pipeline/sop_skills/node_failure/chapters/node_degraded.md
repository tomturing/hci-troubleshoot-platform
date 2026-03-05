# 节点故障排障 SOP

**分类**: 节点  
**适用版本**: HCI 5.x / 6.x  
**关键词**: 节点故障, 节点降级, 主机离线, 节点不可用

---

## 1. 现象描述

集群中某节点状态变为 `degraded` 或 `offline`，该节点上运行的虚拟机可能迁移或停机。

## 2. 快速排查步骤

### 步骤 1：确认节点状态

```bash
# 查看集群节点总览
hcicli node list

# 查看故障节点详情
hcicli node show <node-name> --detail
```

### 步骤 2：尝试 SSH 连接节点

```bash
ssh root@<node-ip>
# 若无法连接，检查网络/电源
```

### 步骤 3：检查节点内部服务

```bash
# 检查 HCI Agent 状态
systemctl status hci-agent

# 查看系统资源
top -bn1 | head -20
free -h
df -h
```

### 步骤 4：将节点标记为维护模式

在直接修复前，先设置维护模式防止新业务分配：

```bash
hcicli node maintenance enable <node-name>
```

### 步骤 5：迁移节点上的虚拟机

```bash
# 批量迁移该节点所有 VM
hcicli node evacuate <node-name> --live-migrate
```

## 3. 常见原因及解决方案

| 原因 | 症状 | 解决方案 |
|------|------|----------|
| 物理机宕机 | 无法 ping | 检查电源/IPMI 重启 |
| 网络隔离 | 集群内不可达 | 检查集群网络 |
| HCI Agent 崩溃 | 服务未响应 | `systemctl restart hci-agent` |
| 磁盘故障 | I/O 错误导致 hang | 参考存储离线 SOP |
| 资源耗尽 | CPU/MEM 100% | 迁移 VM 降低负载 |
