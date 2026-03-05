# 存储离线排障 SOP

**分类**: 存储  
**适用版本**: HCI 5.x / 6.x  
**关键词**: 存储离线, 存储不可用, 磁盘离线, 存储池离线

---

## 1. 现象描述

管理界面显示存储池或磁盘状态为"离线"或"错误"，虚拟机读写 I/O 异常。

## 2. 快速排查步骤

### 步骤 1：确认存储状态

```bash
# 查看所有存储池
hcicli storage pool list

# 查看异常存储池详情
hcicli storage pool show <pool-name> --detail

# 查看磁盘状态
hcicli disk list --node <node-name>
```

### 步骤 2：检查存储服务进程

```bash
# 检查存储服务状态
systemctl status hci-storage
journalctl -u hci-storage --since "30 min ago"

# 检查 Ceph 集群状态（若使用 Ceph 后端）
ceph status
ceph health detail
```

### 步骤 3：检查磁盘健康

```bash
# SMART 检测（替换 /dev/sdX）
smartctl -a /dev/sdX

# 检查磁盘 I/O 错误
dmesg | grep -i "error\|failure\|failed" | tail -30
```

### 步骤 4：重新挂载存储池

```bash
# 尝试重新激活存储池
hcicli storage pool activate <pool-name>

# 若 Ceph OSD 离线，尝试重启
systemctl restart ceph-osd@<osd-id>
```

## 3. 常见原因及解决方案

| 原因 | 检查命令 | 解决方案 |
|------|---------|----------|
| 磁盘 I/O 错误 | `dmesg \| grep error` | 替换磁盘 |
| 存储服务崩溃 | `systemctl status hci-storage` | 重启存储服务 |
| Ceph OSD 离线 | `ceph osd stat` | 重启相应 OSD |
| 网络存储断连 | `ping <storage-ip>` | 检查网络连通性 |
| 存储空间耗尽 | `df -h` | 扩容或清理空间 |

## 4. 数据安全提示

⚠️ **高风险操作提醒**: 在存储异常时，请勿强制删除存储池或重格式化磁盘，先联系技术支持评估数据完整性。
