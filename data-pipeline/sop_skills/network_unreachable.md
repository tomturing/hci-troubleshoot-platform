# 网络不通排障 SOP

**分类**: 网络  
**适用版本**: HCI 5.x / 6.x  
**关键词**: 网络不通, 网络不可达, 虚拟机无法 ping, 网络断开

---

## 1. 现象描述

虚拟机无法访问外部网络，或虚拟机间相互无法通信。

## 2. 快速排查步骤

### 步骤 1：确认虚拟机网卡状态

```bash
# 查看虚拟机网卡配置
hcicli vm show <vm-id> --network

# 进入虚拟机检查网卡状态
hcicli vm console <vm-id>
# 在 VM 内执行：
ip addr show
ip route show
```

### 步骤 2：检查虚拟交换机

```bash
# 查看 vSwitch 状态
hcicli network vswitch list

# 查看虚拟机所绑定的端口组
hcicli network portgroup show <portgroup-name>
```

### 步骤 3：检查物理网卡和上行链路

```bash
# 查看节点物理网卡
hcicli node network show <node-name>

# 检查上行链路状态
ethtool <bond-interface>
```

### 步骤 4：检查 VLAN 配置

```bash
# 确认 VLAN Tag 配置
hcicli network vlan list

# 验证端口组 VLAN
bridge vlan show
```

## 3. 常见原因及解决方案

| 原因 | 检查 | 解决方案 |
|------|------|----------|
| 网卡驱动问题 | `dmesg \| grep eth` | 重载网卡驱动 |
| VLAN 配置错误 | `bridge vlan show` | 修正 VLAN Tag |
| vSwitch 异常 | `hcicli network vswitch list` | 重建 vSwitch |
| 物理链路故障 | `ethtool bond0` | 检查交换机端口 |
| 防火墙/安全组 | iptables 规则 | 检查并放行规则 |
