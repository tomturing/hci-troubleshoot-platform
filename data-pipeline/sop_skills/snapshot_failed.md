# 快照失败排障 SOP

**分类**: 存储  
**适用版本**: HCI 5.x / 6.x  
**关键词**: 快照失败, 快照创建失败, 快照错误, 备份失败

---

## 1. 现象描述

执行虚拟机快照操作时返回失败，或快照任务超时。

## 2. 快速排查步骤

### 步骤 1：查看快照任务日志

```bash
# 查看任务详情
hcicli task list --vm-id <vm-id> --type snapshot

# 查看具体错误
hcicli task show <task-id> --verbose
```

### 步骤 2：检查存储空间

```bash
# 快照需要额外存储空间
hcicli storage pool show <pool-name> | grep -i free
```

**要求**: 快照空间需预留虚拟磁盘大小的至少 20%。

### 步骤 3：检查快照链深度

```bash
# 查看 VM 现有快照数量
hcicli vm snapshot list <vm-id>
```

**建议**: 快照链深度 > 10 时合并快照后再创建。

### 步骤 4：合并快照链

```bash
# 合并所有旧快照（保留最新一个）
hcicli vm snapshot consolidate <vm-id>
```

### 步骤 5：重试快照操作

```bash
hcicli vm snapshot create <vm-id> --name "manual-$(date +%Y%m%d%H%M%S)"
```

## 3. 常见原因及解决方案

| 原因 | 检查 | 解决方案 |
|------|------|----------|
| 存储空间不足 | 存储池可用率 | 扩容或清理旧快照 |
| 快照链过长 | 快照列表 > 10 | 执行 consolidate |
| VM I/O 繁忙 | iostat 监控 | 业务低峰期重试 |
| 存储驱动异常 | qemu-img 报错 | 重启存储服务 |
