# 虚拟机无法开机排障 SOP

**分类**: 虚拟机  
**适用版本**: HCI 5.x / 6.x  
**关键词**: 虚拟机无法开机, VM 开机失败, 虚拟机不能启动, 开机报错

---

## 1. 现象描述

用户尝试启动虚拟机，管理界面显示"开机失败"或虚拟机状态停留在"启动中"超过 5 分钟。

## 2. 快速排查步骤

### 步骤 1：确认虚拟机所在节点状态

```bash
# 查看所有节点状态
hcicli node list

# 查看目标节点详情（替换 <node-name>）
hcicli node show <node-name>
```

**预期输出**: 节点状态为 `online`，若为 `degraded` 或 `offline` 请先处理节点故障。

### 步骤 2：检查存储资源

```bash
# 查看存储池状态
hcicli storage pool list

# 检查虚拟机所在存储池空间
hcicli storage pool show <pool-name> --detail
```

**注意**: 若存储空间不足（可用 < 20%），虚拟机开机可能失败。

### 步骤 3：检查虚拟机事件日志

```bash
# 查看虚拟机事件（替换 <vm-id>）
hcicli vm events <vm-id> --tail 50

# 查看节点上的 libvirt 日志
journalctl -u libvirtd --since "1 hour ago" | grep <vm-id>
```

### 步骤 4：检查内存资源

```bash
# 查看集群内存概览
hcicli cluster resources --type memory

# 检查节点可用内存
hcicli node show <node-name> --resources
```

### 步骤 5：尝试强制迁移开机

若当前节点资源不足，尝试将虚拟机迁移到其他节点：

```bash
hcicli vm migrate <vm-id> --target-node <other-node>
hcicli vm start <vm-id>
```

## 3. 常见原因及解决方案

| 原因 | 表现 | 解决方案 |
|------|------|----------|
| 所在节点离线 | 节点状态 `offline` | 先修复节点，或迁移到其他节点 |
| 存储空间不足 | 存储池使用率 > 85% | 扩容存储或迁移业务 |
| 内存超配 | 节点内存使用率 > 90% | 迁移到内存充裕的节点 |
| 配置文件损坏 | libvirt 日志报 XML 错误 | 导出 VM 配置后重新导入 |
| 快照链过长 | 操作超时 | 合并快照后重试 |

## 4. 升级处理

若以上步骤无法解决，请收集以下信息并联系技术支持：

```bash
# 收集诊断包
hcicli diagnostic collect --vm-id <vm-id> --output /tmp/diag_<vm-id>.tar.gz
```
