# acli 知识库与采集器目录设计（基于 acli 全量文档）

> 版本：v1.0
> 日期：2026-07-16
> 文档来源：http://acli.sangfor.com.cn:6888/ （全量抓取 336 条命令）
> 关联文档：[关键信号架构演进-从两方案批判到分层修正.md](./关键信号架构演进-从两方案批判到分层修正.md)、[方案对比-基于5个真实案例的完整分析.md](./方案对比-基于5个真实案例的完整分析.md)
> 关联代码：`backend/agent-service/app/tools/qkv/`、`backend/agent-service/app/tools/qfk/`、`backend/kb-service/app/routes/extract_signals.py`

---

## 〇、背景纠正

### 0.1 关键事实

1. **agent 禁止访问 host 后台**（禁止 `host.shell`），全部通过 acli（封装在 acli 容器中）作为唯一对 agent 暴露的方式来有限访问后台和兜底。这是为了防止 agent 产生幻觉对 HCI 底层做出错误操作。
2. **KBD 中写 shell 命令是历史遗留**：a) KBD 最初是给人看的；b) acli 是新增的（晚于大部分 KBD），目前还不全，在持续完善中。
3. **acli 远比想象中强大**：它不仅封装了 `alert/task/log/service/vm/storage/network` 等平台命令，还在 `system` 分类下封装了 `lsof/ps/kill/lsblk/iostat/smartctl/dmidecode/df/free/top/netstat/ping` 等 **37 个主机级命令**。之前认为"缺失 host.shell"是错误判断--acli 的 `system` 命名空间就是受限的 host.shell。

### 0.2 原设计的错误

原 `ACQUIRER_CATALOG` 把 QFK 设计成 8 个**信号类型**（`log_keyword/service_status/vm_state/network_check/storage_state/hardware_state/platform_state/system_metric`），每个类型对应一个固定 Handler，用 `signal_type` 枚举做路由。这有两个问题：

1. **粒度错了**：把 acli 的 **namespace**（`log/service/system/vm/...`）当成了**信号类型**，但一个 namespace 下有几十个子命令（如 `system` 下有 37 个子命令），全塞进一个 `GenericSubCommandHandler` 等于没有区分度。
2. **扩展性差**：新增子命令需要改枚举 + Handler + Prompt 三处。

### 0.3 修正后的设计原则

```
QKV (生产者)：结果多样性复杂 -> 产出字段可自定义增删改，输出到变量池
QFK (消费者)：方法多样性复杂 -> 子命令+参数可自定义增删改，从变量池获取
两者统一考虑：扩展性、易用性
```

---

## 一、acli 全量命令目录

### 1.1 总览

| Namespace | 命令数 | 用途 | 角色 |
|-----------|-------|------|------|
| `alert` | 2 | 告警查询 | **QKV 生产者** |
| `task` | 2 | 任务查询 | **QKV 生产者** |
| `log` | 4 | 日志检索 | **QKV 生产者**（dialog）+ **QFK 消费者**（log_keyword） |
| `system` | 46 | 系统诊断（lsof/ps/iostat/smartctl...） | **QFK 消费者** |
| `vm` | 33 | 虚拟机操作 | **QFK 消费者** |
| `storage` | 19 | 存储检查 | **QFK 消费者** |
| `network` | 50 | 网络检查 | **QFK 消费者** |
| `service` | 115 | 服务管理 | **QFK 消费者** |
| `platform` | 28 | 平台信息 | **QFK 消费者** |
| `hardware` | 4 | 硬件检查 | **QFK 消费者** |
| `plugins` | 25 | 专用排障插件 | **QFK 消费者**（高级） |
| **合计** | **336** | | |

### 1.2 各 Namespace 子命令清单

#### alert（2 条）-- QKV 生产者

| 命令 | 说明 | 关键参数 |
|------|------|---------|
| `acli alert get` | 查询告警信息 | `-k` 关键词, `-o` 对象, `-T` 对象类型, `-t` 时间, `-L` 级别, `-l` 数量 |
| `acli alert list` | 展示当天去重告警 | 无 |

**产出字段**：`alert_type, process, object_type, vm, end, hostname, object_name, type, status, description, host, hostid, target, urgent_type, otype`

#### task（2 条）-- QKV 生产者

| 命令 | 说明 | 关键参数 |
|------|------|---------|
| `acli task get` | 查询操作任务信息 | `-k` 关键词, `-c` 错误码, `-v` VM ID, `-t` 时间, `-H` 主机, `-s` 状态(failed/completed/...), `-u` UPID, `-l` 数量 |
| `acli task list` | 展示当天操作任务 | 无 |

**产出字段**：`alert_type, process, object_type, pid, vm, end, hostname, object_name, type, status, description, host, upid, user, risk_level, errcode_tracing, request_id`

#### log（4 条）-- QKV 生产者（dialog）+ QFK 消费者

| 命令 | 说明 | 关键参数 |
|------|------|---------|
| `acli log get` | 获取平台日志 | `-k` 关键词, `-i` request_id, `-t` 时间, `-f` 文件名, `-c` 上下文行数, `-E` 扩展正则, `-p` 路径(限/sf/log和/sf/data/local), `-g` 搜索.gz |
| `acli log switch` | 日志轮转 | 无 |
| `acli log cgroup parse` | 解析cgroup黑盒日志 | - |

**QKV 产出字段**（dialog 模式）：`request_id, time`
**QFK 判定**：keyword 匹配日志内容

**注意**：`-f` 文件名不能包含路径；`-p` 只允许 `/sf/log` 和 `/sf/data/local` 目录

#### system（46 条）-- QFK 消费者

这是最重要的 QFK namespace，封装了 37 个主机级命令：

| 子命令 | 对应原生命令 | 排障用途 |
|--------|------------|---------|
| `lsof` | lsof | 查文件/进程占用（KBD 27123 Step2） |
| `ps` | ps | 查进程信息（KBD 27123 Step3） |
| `kill` | kill | 终止进程（KBD 27123 解决方案） |
| `lsblk` | lsblk | 查磁盘块设备（KBD 40680 Step1） |
| `iostat` | iostat | 查 IO 时延（KBD 41570 Step2） |
| `smartctl` | smartctl | 查 SMART 值（KBD 40652 Step1） |
| `dmidecode` | dmidecode | 查硬件信息（KBD 40680 Step2） |
| `df` | df | 查磁盘空间 |
| `free` | free | 查内存 |
| `top` | top | 查进程实时信息 |
| `netstat` | netstat | 查网络连接 |
| `ping` | ping | 网络连通性 |
| `uname` | uname | 系统信息 |
| `ls` | ls（限制目录） | 查文件列表（KBD 40652 Step3） |
| `ipmitool` | ipmitool | BMC 管理（KBD 40680 Step3 部分） |
| `sensors` | sensors | 传感器温度 |
| `multipath` | multipath | 多路径 |
| `ethtool` | ethtool | 网卡信息 |
| `mpstat` | mpstat | CPU 统计 |
| `pidstat` | pidstat | 进程资源 |
| `stat` | stat | 文件状态 |
| `tcpdump` | tcpdump | 抓包 |
| `perf` | perf | 性能分析 |
| `fio` | fio | IO 基准测试 |
| `date` | date | 系统时间 |
| `du` | du | 目录大小 |
| `lsmod` | lsmod | 内核模块 |
| `lldptool` | lldptool | LLDP |
| `realethtool` | realethtool | 真实网卡 |
| `rm` | rm（限制目录） | 删除文件 |
| `chping` | chping | 自研 ping |
| `cpu info` | CPU 信息 | CPU 详情 |
| `memory dump/info/usage` | 内存信息 | 内存详情 |
| `cgroup get/list/tree` | cgroup | CGroup 信息 |
| `hosts get` | 主机信息 | hosts 文件 |
| `mounts get` | 挂载信息 | mount 状态 |
| `proc cgroup/stack get` | 进程信息 | 进程栈/cgroup |
| `smartpqi version get` | smartpqi 版本 | RAID 驱动版本 |

**关键发现**：acli `system` namespace = **受限的 host.shell**。KBD 中写的 `lsof | grep <vmid>`、`ps auxf | grep 240132`、`lsblk`、`iostat`、`smartctl` 全部可通过 `acli system lsof/ps/lsblk/iostat/smartctl` 执行。

**注意**：部分 system 命令显示"无参数"，但实际是原生命令的 wrapper（如 `acli system lsof` 实际执行 `lsof`，`acli system smartctl` 实际执行 `smartctl`）。参数透传方式需确认（是否支持 `acli system lsof -p <pid>` 这种追加参数）。

#### vm（33 条）-- QFK 消费者

| 子命令组 | 命令示例 | 说明 |
|---------|---------|------|
| `list` | `acli vm list` | 获取所有虚拟机信息 |
| `start` | `acli vm start` | 开启虚拟机 |
| `shutdown` | `acli vm shutdown` | 关闭虚拟机 |
| `delete` | `acli vm delete` | 删除虚拟机 |
| `status get/set` | `acli vm status get -v <vmid>` | 虚拟机状态 |
| `config get/pending` | `acli vm config get -v <vmid>` | 虚拟机配置 |
| `disk list/check/path/aio/io_uring` | `acli vm disk list -v <vmid>` | 磁盘镜像 |
| `memory dump` | `acli vm memory dump create` | 内存 dump |
| `nic queuecount` | `acli vm nic queuecount get` | 网卡队列 |
| `lock list` | `acli vm lock list` | 虚拟机锁 |
| `sagaflow get/rollback` | `acli vm sagaflow get` | SagaFlow 信息 |
| `int3` | `acli vm int3` | int3 dump 内存 |

#### storage（19 条）-- QFK 消费者

| 子命令组 | 命令示例 | 说明 |
|---------|---------|------|
| `asan disk list` | `acli storage asan disk list` | 磁盘信息列表（KBD 40652/40750） |
| `asan volume list/iostat` | `acli storage asan volume list` | 存储卷信息 |
| `asan brick iostat` | `acli storage asan brick iostat` | brick iostat |
| `asan version get` | `acli storage asan version get` | VS 版本 |
| `mount` | `acli storage mount -s <sid>` | 查看挂载信息 |
| `umount` | `acli storage umount` | 卸载挂载 |
| `sffsck` | `acli storage sffsck` | sffsck 命令 |
| `other list` | `acli storage other list` | 其他存储信息 |
| `fc host list/statistics` | `acli storage fc host list` | FC 存储 |

**关键**：`acli storage asan disk list` 输出包含 `status, fault, major_fault, disk_sn, dev, disk_type, disk_group_id` 等字段，可直接用于 KBD 40652/40750 的判定。

#### network（50 条）-- QFK 消费者

| 子命令组 | 命令示例 | 说明 |
|---------|---------|------|
| `bond list/get/set/delete` | `acli network bond list` | 聚合口 |
| `nic list/get/set/up/down` | `acli network nic list` | 网口 |
| `nic mtu/queue/ring/lldp/rdma` | `acli network nic mtu get` | 网口配置 |
| `anet config/forwarding/mirror/session/vrouter` | `acli network anet vrouter list` | anet 数据面 |
| `vxlan list/set` | `acli network vxlan list` | VXLAN |
| `port check` | `acli network port check` | 端口状态 |
| `vlink check` | `acli network vlink check` | vlink 状态 |
| `mgmt switch` | `acli network mgmt switch` | 管理口切换 |
| `sfd_bytools` | `acli network sfd_bytools` | 数据面脚本 |

#### service（115 条）-- QFK 消费者

| 容器 | 子命令模式 | 命令示例 |
|------|-----------|---------|
| `asv` | `<service> start/stop/restart/status` | `acli service asv apache2 status` |
| `anet` | `<service> start/stop/restart/status` | `acli service anet vn-manager-service-api status` |
| `host` | `<service> start/stop/restart/status` | `acli service host hostd status` |
| `asv` 特殊 | `loadman config/maintenance get/set` | `acli service asv loadman config get` |
| `asv` 特殊 | `vtpdaemon opqueue config get/set` | `acli service asv vtpdaemon opqueue config get` |
| `asv` 特殊 | `mgmt-node-agent-api get` | `acli service asv mgmt-node-agent-api get` |

**asv 服务列表**：apache2, authorize_client, authorize_server, corosync, exporter, loadman, mgmt-node-agent-api, mgmt-node-agent-periodic, mysql, mysql-managerd, perl-services, pmxcfs, redis, rrdcached, rsyncd, sangfor_waf, vtpalertd, vtpcron, vtpdaemon, vtplogd, vtpperlproxy, vtpstatd, zk

**anet 服务列表**：vn-cluster-service-api, vn-manager-service-api, vn-node-agent-api

**host 服务列表**：hostd, lsud

#### platform（28 条）-- QFK 消费者

| 子命令组 | 命令示例 | 说明 |
|---------|---------|------|
| `info get` | `acli platform info get` | 系统信息 |
| `node get/list` | `acli platform node get --node-name <ip>` | 节点信息 |
| `version get` | `acli platform version get` | 平台版本（KBD 40680） |
| `mysql config get` | `acli platform mysql config get` | MySQL 配置 |
| `mysql-manager-cli` | `acli platform mysql-manager-cli` | MySQL 管理工具 |
| `redis redis-cli` | `acli platform redis redis-cli` | Redis 工具 |
| `zookeeper zkcli` | `acli platform zookeeper zkcli` | ZK 工具 |
| `snapshot mysql get` | `acli platform snapshot mysql get` | 快照数据库 |
| `backup config/get/set` | `acli platform backup config get` | 备份配置 |
| `cfs status` | `acli platform cfs status` | CFS 写入能力 |
| `scheduler quota get/set` | `acli platform scheduler quota get` | 调度器配额 |
| `node cert get/list` | `acli platform node cert list` | 节点证书 |

#### hardware（4 条）-- QFK 消费者

| 命令 | 说明 |
|------|------|
| `acli hardware cpu microcode file list` | CPU 微码文件 |
| `acli hardware gpu config get/list` | GPU 配置 |
| `acli hardware hostcli hostcli` | 主机硬件管理 |

#### plugins（25 条）-- QFK 消费者（高级排障插件）

| 插件 | 命令示例 | 说明 |
|------|---------|------|
| `vm_start` | `acli plugins vm_start vm_start` | **虚拟机开机失败检测**（直接覆盖 KBD 27123） |
| `vm_suspend` | `acli plugins vm_suspend vm_suspend` | 虚拟机挂起检测 |
| `asan_ops` | `acli plugins asan_ops asan_ops` | 虚拟存储检测工具 |
| `asys` | `acli plugins asys asys` | 系统检查工具 |
| `netdoctor` | `acli plugins netdoctor netdoctor` | 网络排障（多场景诊断箱+工具箱） |
| `performance_tools` | `acli plugins performance_tools check_vm <vmid>` | 性能分析（check_vm/check_host/monitor/analyze） |
| `log_collect` | `acli plugins log_collect create` | 日志收集任务 |

**关键发现**：`acli plugins vm_start vm_start` 是专门的虚拟机开机失败检测插件，**直接覆盖 KBD 27123 的全部排查逻辑**。这类插件是 acli 的高级封装，把多步排查打包成一条命令。

---

## 二、修正后的采集器目录设计

### 2.1 设计原则

```
QKV (生产者)：
  - 复杂在结果多样性（解析的字段多）
  - 产出字段支持自定义增删改
  - 输出到变量池

QFK (消费者)：
  - 复杂在方法多样性（命令和参数多）
  - 子命令支持自定义增删改
  - 参数从变量池获取

两者统一：扩展性（新增命令不改框架）+ 易用性（LLM 能准确生成）
```

### 2.2 QKV 采集器（生产者，3 类）

QKV 的职责是**取数并产出变量**。每类 QKV 支持自定义产出字段。

```python
QKV_CATALOG = {
    "qkv.alert": {
        "command": "acli alert get",
        "description": "查询告警信息",
        "params": {
            "keyword": "-k {value}",        # 搜索关键词
            "object_name": "-o {value}",     # 告警对象
            "object_type": "-T {value}",     # 对象类型: host/storage_obj/vm/vnet/sn/others
            "time": "-t {value}",            # 时间
            "level": "-L {value}",           # 级别: 1紧急/0普通
            "limit": "-l {value}",           # 数量
        },
        "output_fields": [                    # 可自定义增删改的产出字段
            "alert_type", "end", "target", "type", "description",
            "host", "vm", "hostname", "hostid", "object_name",
            "object_type", "status", "urgent_type"
        ],
        "default_produces": ["HOST", "VM", "TARGET", "END", "ALERT_TYPE"],
    },
    "qkv.task": {
        "command": "acli task get",
        "description": "查询操作任务信息",
        "params": {
            "keyword": "-k {value}",
            "code": "-c {value}",            # 错误码
            "vm_id": "-v {value}",
            "time": "-t {value}",
            "host": "-H {value}",
            "status": "-s {value}",          # progress/completed/failed/queued
            "upid": "-u {value}",
            "limit": "-l {value}",
        },
        "output_fields": [
            "status", "type", "end", "host", "vm", "target", "description",
            "errcode_tracing", "request_id", "hostname", "upid", "user",
            "risk_level", "pid", "object_name", "object_type"
        ],
        "default_produces": ["HOST", "VM", "TARGET", "END", "ERRCODE_TRACING", "REQUEST_ID", "STATUS"],
    },
    "qkv.dialog": {
        "command": "acli log get",
        "description": "弹框/对话日志查询",
        "params": {
            "keyword": "-k {value}",
            "request_id": "-i {value}",
            "time": "-t {value}",
            "file": "-f {value}",            # 文件名，不含路径
            "context": "-c {value}",         # 上下文行数
            "path": "-p {value}",            # 限/sf/log和/sf/data/local
            "extend_regex": "-E",            # 扩展正则
            "gzip": "-g",                    # 搜索.gz
        },
        "output_fields": ["request_id", "time"],
        "default_produces": ["REQUEST_ID", "TIME"],
    },
}
```

### 2.3 QFK 采集器（消费者，8 类）

QFK 的职责是**执行子命令并判定**。每类 QFK 对应一个 acli namespace，支持子命令 + 参数自定义。

```python
QFK_CATALOG = {
    "qfk.log": {
        "command": "acli log",
        "description": "日志检查和相关操作",
        "subcommands": {
            "get":    "获取平台日志: acli log get -k '{keyword}' -f {file} [-p {path}] [-t {time}]",
            "switch": "日志轮转: acli log switch",
        },
        "params_from_pool": ["HOST", "TIME", "FILE"],  # 参数可从变量池获取
        "example": "acli log get -k 'No space left on device' -f {{FNAME}}",
    },
    "qfk.service": {
        "command": "acli service",
        "description": "服务检查和相关操作",
        "subcommands": "{container} {service_name} {action}",
        "containers": ["asv", "anet", "host"],
        "actions": ["status", "start", "stop", "restart"],
        "params_from_pool": [],
        "example": "acli service asv apache2 status  或  acli service anet vn-manager-service-api restart",
    },
    "qfk.system": {
        "command": "acli system",
        "description": "系统检查和相关操作（封装37个主机级命令）",
        "subcommands": [
            "lsof", "ps", "kill", "lsblk", "iostat", "smartctl", "dmidecode",
            "df", "free", "top", "netstat", "ping", "uname", "ls", "ipmitool",
            "sensors", "multipath", "ethtool", "mpstat", "pidstat", "stat",
            "tcpdump", "perf", "fio", "date", "du", "lsmod", "cpu info",
            "memory dump/info/usage", "cgroup get/list/tree",
            "hosts get", "mounts get", "proc cgroup/stack get",
        ],
        "params_from_pool": ["HOST", "VM", "PID", "DISK_SN"],
        "example": "acli system lsof  或  acli system ps  或  acli system kill -p {{PID}}",
    },
    "qfk.vm": {
        "command": "acli vm",
        "description": "虚拟机检查和相关操作",
        "subcommands": [
            "list", "start", "shutdown", "delete", "status get", "config get",
            "disk list", "disk check", "disk path get", "disk aio get",
            "memory dump", "nic queuecount get", "lock list", "sagaflow get",
        ],
        "params_from_pool": ["VM", "VMID"],
        "example": "acli vm list  或  acli vm config get -v {{VMID}}",
    },
    "qfk.network": {
        "command": "acli network",
        "description": "网络检查相关操作",
        "subcommands": [
            "bond list/get", "nic list/get", "nic mtu get", "nic queue get",
            "anet vrouter list/get", "anet session get", "anet forwarding get",
            "vxlan list", "port check", "vlink check",
        ],
        "params_from_pool": ["HOST", "BNAME", "NNAME"],
        "example": "acli network bond list  或  acli network bond get --bond-name {{BNAME}}",
    },
    "qfk.storage": {
        "command": "acli storage",
        "description": "存储检查相关操作",
        "subcommands": [
            "asan disk list", "asan volume list", "asan volume iostat",
            "asan brick iostat", "asan version get",
            "mount", "umount", "sffsck", "other list",
            "fc host list",
        ],
        "params_from_pool": ["HOST", "SID", "DISK_SN"],
        "example": "acli storage sffsck  或  acli storage mount -s {{SID}}",
    },
    "qfk.hardware": {
        "command": "acli hardware",
        "description": "硬件检查相关操作",
        "subcommands": [
            "cpu microcode file list", "gpu config list", "gpu config get",
            "hostcli hostcli",
        ],
        "params_from_pool": ["HOST", "FNAME"],
        "example": "acli hardware gpu config list  或  acli hardware gpu config get -n {{FNAME}}",
    },
    "qfk.platform": {
        "command": "acli platform",
        "description": "平台检查相关操作",
        "subcommands": [
            "info get", "node get", "node list", "version get",
            "mysql config get", "mysql-manager-cli",
            "redis redis-cli", "zookeeper zkcli",
            "snapshot mysql get", "backup config get",
            "cfs status", "scheduler quota get",
            "node cert list",
        ],
        "params_from_pool": ["HOST", "NNAME"],
        "example": "acli platform info get  或  acli platform node get --node-name {{NNAME}}",
    },
}
```

### 2.4 与原设计的对比

| 维度 | 原设计（错误） | 修正后设计 |
|------|--------------|-----------|
| QFK 类型 | 8 个信号类型枚举（`log_keyword/service_status/vm_state/...`） | 8 个 acli namespace（`log/service/system/vm/network/storage/hardware/platform`） |
| QFK 路由 | `signal_type` 枚举 -> `HandlerRegistry` | `acquirer` 字符串 -> 命令模板构建 |
| QFK 子命令 | `GenericSubCommandHandler` 用 `sub_command` 拼接 | 每类有明确的子命令目录 |
| QFK 参数 | `target` 固定结构（scope/resource/path） | 参数模板，支持 `{{VAR}}` 从变量池获取 |
| QKV 产出 | 固定取 `values[0]` 的固定字段 | `produces` 自定义字段列表 |
| 命名 | `qfk.log_keyword`（类型名） | `qfk.log`（namespace 名） |
| 扩展性 | 新增子命令改枚举+Handler | 新增子命令只改目录 |

---

## 三、5 个真实案例验证

### 3.1 覆盖率对比

| | 原设计 | 修正后设计 |
|---|---|---|
| 17 步中可自动化 | 2 (11%) | **15 (88%)** |
| 17 步中需人工/第三方 | 15 (88%) | **2 (11%)** |

### 3.2 逐案例验证

#### KBD 27123（虚拟机镜像忙）

| 步骤 | 原设计 | 修正后 | acli 命令 |
|------|--------|--------|----------|
| Step1: 查任务 | ✅ qkv.task | ✅ qkv.task | `acli task get -k '镜像忙' -s failed` |
| Step2: lsof查占用 | ❌ 无host.shell | ✅ **qfk.system** | `acli system lsof` |
| Step3: ps查进程 | ❌ 无host.shell | ✅ **qfk.system** | `acli system ps` |
| (可选) 开机检测 | ❌ 无 | ✅ **qfk.plugins** | `acli plugins vm_start vm_start` |

#### KBD 41570（iSCSI scrub IO卡顿）

| 步骤 | 原设计 | 修正后 | acli 命令 |
|------|--------|--------|----------|
| Step1: 查qemu日志 | ⚠️ 部分覆盖 | ✅ **qfk.log** | `acli log get -k 'iotimeout' -f sfvt_qemu_*.log` |
| Step2: 查iostat | ❌ 无threshold | ✅ **qfk.system** + threshold | `acli system iostat` |
| Step3: 存储侧确认 | ❌ | ❌ 人工/第三方 | needs_review |

#### KBD 40652（SMART坏道）

| 步骤 | 原设计 | 修正后 | acli 命令 |
|------|--------|--------|----------|
| Step1: 查SMART | ❌ 无shell | ✅ **qfk.system** + threshold | `acli system smartctl` |
| Step2: 查硬盘状态 | ⚠️ 间接 | ✅ **qfk.storage** | `acli storage asan disk list` |
| Step3: 定位设备文件 | ❌ 无shell | ✅ **qfk.system** | `acli system ls /sf/cfg/vs/disk/` |

#### KBD 40680（RAID卡不识别）

| 步骤 | 原设计 | 修正后 | acli 命令 |
|------|--------|--------|----------|
| Step1: 查磁盘数 | ❌ 无shell | ✅ **qfk.system** + threshold | `acli system lsblk` |
| Step2: 查RAID卡 | ❌ 无shell | ✅ **qfk.system** + keyword | `acli system dmidecode` |
| Step3: BMC查磁盘 | ❌ 第三方 | ⚠️ **qfk.system** (ipmitool部分) | `acli system ipmitool` |

#### KBD 40750（磁盘组配比误报）

| 步骤 | 原设计 | 修正后 | acli 命令 |
|------|--------|--------|----------|
| Step1: 查告警 | ✅ qkv.alert | ✅ qkv.alert | `acli alert get -k '磁盘被拔出'` |
| Step2: 查磁盘状态 | ⚠️ 间接 | ✅ **qfk.storage** | `acli storage asan disk list` |
| Step3: 查内核日志 | ⚠️ 部分 | ✅ **qfk.log** | `acli log get -k 'disk' -f kernel.log` |
| Step4: 查任务 | ⚠️ 间接 | ✅ qkv.task | `acli task get -k '数据同步'` |
| Step5: 对比配比 | ❌ | ✅ **qfk.storage** (逐主机) | `acli storage asan disk list` (多节点) |

---

## 四、QKV 与 QFK 的统一设计

### 4.1 核心差异

| 维度 | QKV（生产者） | QFK（消费者） |
|------|-------------|-------------|
| **复杂点** | 结果多样性（解析字段多） | 方法多样性（命令和参数多） |
| **输入** | 用户告警/任务关键词 | 变量池中的变量 + 子命令 |
| **输出** | 结构化变量 -> 变量池 | 布尔判定 + 证据链 |
| **acli 命令** | `alert get` / `task get` / `log get` | `<namespace> <subcommand> [args]` |
| **参数来源** | 信号 acquirer_args | acquirer_args + 变量池 `{{VAR}}` |
| **matcher** | 无（只取数） | 有（keyword/state/threshold/json_path/exists） |

### 4.2 统一的信号模型

```python
class KeySignal(BaseModel):
    id: str
    signal_category: str          # "frontend"(producer/QKV) 或 "backend"(consumer/QFK)
    keyword: str                  # 核心检索词
    description: str | None
    
    # 采集器绑定（统一字段）
    acquirer: str                 # "qkv.alert" / "qfk.system" / ...
    acquirer_args: dict           # 参数模板，含 {{VAR}} 占位符
    
    # 变量契约（统一字段）
    produces: list[dict] | None   # QKV: [{name, path}] 产出变量
    requires: list[str] | None    # QFK: 依赖的变量名
    
    # 判定器（仅 QFK）
    matcher: dict | None          # {type, pattern, mode, expected}
```

### 4.3 扩展性设计

**新增 acli 子命令只需改目录，不改代码**：

```python
# 在 QFK_CATALOG["qfk.system"]["subcommands"] 中新增一项即可
"subcommands": [
    ..., "新命令名", ...
]
```

**新增 acli namespace（如未来 acli 新增 `acli database` 命令）只需加一个目录项**：

```python
QFK_CATALOG["qfk.database"] = {
    "command": "acli database",
    "description": "数据库检查和相关操作",
    "subcommands": {...},
    "params_from_pool": [...],
}
```

**QKV 产出字段自定义**：

```python
# 信号抽取时，LLM 根据 KBD 内容决定产出哪些字段
{
    "acquirer": "qkv.task",
    "acquirer_args": {"keyword": "镜像忙", "status": "failed"},
    "produces": [
        {"name": "HOST", "path": "host"},
        {"name": "VM", "path": "vm"},
        {"name": "PID", "path": "pid"},  # 自定义新增
    ]
}
```

### 4.4 易用性设计

LLM 抽取信号时拿到的 Prompt 上下文：

```
## 采集器目录（封闭词表）

> display_name 标准命名（勿擅自修改以免造成误解）：
> qkv.alert - 前端信号-告警查询 | qkv.task - 前端信号-任务查询 | qkv.dialog - 前端信号-弹框查询
> qfk.log - 后端信号-日志检查和操作 | qfk.service - 后端信号-服务检查和操作 | qfk.system - 后端信号-系统检查和操作
> qfk.vm - 后端信号-虚拟机相关操作 | qfk.network - 后端信号-网络相关操作 | qfk.storage - 后端信号-存储相关操作
> qfk.hardware - 后端信号-硬件相关操作 | qfk.platform - 后端信号-平台相关操作

### QKV 生产者（取数，产出变量）
- qkv.alert: acli alert get，产出 alert_type/end/target/host/vm/...
  参数: keyword(-k), object_name(-o), object_type(-T), time(-t), level(-L), limit(-l)
- qkv.task: acli task get，产出 status/host/vm/errcode_tracing/request_id/...
  参数: keyword(-k), code(-c), vm_id(-v), time(-t), host(-H), status(-s), limit(-l)
- qkv.dialog: acli log get，产出 request_id/time
  参数: keyword(-k), request_id(-i), time(-t), file(-f), path(-p), context(-c)

### QFK 消费者（执行子命令，判定结果）
- qfk.log: acli log <subcommand>
  子命令: get(获取日志), switch(日志轮转)
  参数: keyword(-k), file(-f), path(-p), time(-t)
- qfk.service: acli service <container> <service> <action>
  容器: asv/anet/host; 动作: status/start/stop/restart
- qfk.system: acli system <subcommand>
  子命令: lsof/ps/kill/lsblk/iostat/smartctl/dmidecode/df/free/top/netstat/ping/...
- qfk.vm: acli vm <subcommand>
  子命令: list/start/shutdown/status get/config get/disk list/disk check/...
- qfk.network: acli network <subcommand>
  子命令: bond list/nic list/anet vrouter list/port check/vlink check/...
- qfk.storage: acli storage <subcommand>
  子命令: asan disk list/asan volume list/mount/sffsck/other list/...
- qfk.hardware: acli hardware <subcommand>
  子命令: cpu microcode file list/gpu config list/gpu config get/...
- qfk.platform: acli platform <subcommand>
  子命令: info get/node get/node list/version get/mysql config get/...

### 变量池 schema
可用变量名: HOST, VM, VMID, NODE_IP, TARGET, END, ALERT_TYPE, STATUS,
            ERRCODE_TRACING, REQUEST_ID, PID, DISK_SN, FILE, TIME

### 占位符
参数中引用变量池: {{HOST}}, {{VM}}, {{PID}} ...
```

---

## 五、代码改造方向

### 5.1 QFK HandlerRegistry 改造

**现状**：8 个 `BackendSignalType` 枚举 + 3 个 Handler 类 + `NAMESPACE_MAP` 映射

**改造**：去掉枚举，改为基于 `acquirer` 字符串的命令模板构建

```python
# 改造前
class BackendSignalType(StrEnum):
    LOG_KEYWORD = "log_keyword"
    SERVICE_STATUS = "service_status"
    # ... 8个枚举

class HandlerRegistry:
    _registry = {
        BackendSignalType.LOG_KEYWORD: LogKeywordHandler(),
        # ...
    }

# 改造后
class QFKExecutor:
    """基于 acquirer namespace 的命令构建器"""
    
    CATALOG = QFK_CATALOG  # 上述定义的目录
    
    def build_command(self, acquirer: str, acquirer_args: dict, variable_pool: dict) -> str:
        """根据 acquirer 和参数构建 acli 命令"""
        ns = acquirer.split(".", 1)[1]  # "qfk.system" -> "system"
        spec = self.CATALOG[f"qfk.{ns}"]
        
        # 渲染参数中的 {{VAR}} 占位符
        rendered_args = self._render_args(acquirer_args, variable_pool)
        
        # 构建命令
        if ns == "log":
            return self._build_log_command(rendered_args)
        elif ns == "service":
            return self._build_service_command(rendered_args)
        elif ns == "system":
            return self._build_system_command(rendered_args)
        # ... 每个 namespace 一个 builder
```

### 5.2 QKV 产出字段改造

**现状**：`VariablePool.register_from_frontend_result` 硬编码取固定字段

**改造**：根据信号的 `produces` 列表动态提取

```python
# 改造前
def register_from_frontend_result(self, result):
    first_val = result.values[0]
    for key in ["host", "vm", "end", "target", "trace_id", "errcode_tracing"]:
        if first_val.get(key):
            self._variables[key] = first_val[key]

# 改造后
def register_from_frontend_signal(self, signal: dict, result):
    produces = signal.get("produces") or []
    first_val = result.values[0] if result.values else {}
    for spec in produces:
        name = spec["name"]                    # "HOST"
        path = spec.get("path", name.lower())  # "host"
        val = first_val.get(path)
        if val is not None:
            self._variables[name] = val
```

### 5.3 ACQUIRER_CATALOG 改造

```python
# extract_signals.py 中
# 改造前
ACQUIRER_CATALOG = {
    "qkv.alert": "...", "qkv.task": "...", "qkv.dialog": "...",
    "qfk.log_keyword": "...", "qfk.service_status": "...",
    "qfk.vm_state": "...", "qfk.network_check": "...",
    "qfk.storage_state": "...", "qfk.hardware_state": "...",
    "qfk.platform_state": "...", "qfk.system_metric": "...",
}

# 改造后
# display_name 标准命名（勿擅自修改以免造成误解）：
#   qkv.alert    - 前端信号-告警查询
#   qkv.task     - 前端信号-任务查询
#   qkv.dialog   - 前端信号-弹框查询
#   qfk.log      - 后端信号-日志检查和操作
#   qfk.service  - 后端信号-服务检查和操作
#   qfk.system   - 后端信号-系统检查和操作
#   qfk.vm       - 后端信号-虚拟机相关操作
#   qfk.network  - 后端信号-网络相关操作
#   qfk.storage  - 后端信号-存储相关操作
#   qfk.hardware - 后端信号-硬件相关操作
#   qfk.platform - 后端信号-平台相关操作
ACQUIRER_CATALOG = {
    "qkv.alert": "前端信号-告警查询: acli alert get",
    "qkv.task": "前端信号-任务查询: acli task get",
    "qkv.dialog": "前端信号-弹框查询: acli log get",
    "qfk.log": "后端信号-日志检查和操作: acli log get/switch",
    "qfk.service": "后端信号-服务检查和操作: acli service {asv|anet|host} <name> {status|start|stop|restart}",
    "qfk.system": "后端信号-系统检查和操作: acli system {lsof|ps|kill|lsblk|iostat|smartctl|dmidecode|df|free|top|netstat|ping|uname|ls|...}",
    "qfk.vm": "后端信号-虚拟机相关操作: acli vm {list|start|shutdown|status get|config get|disk list|...}",
    "qfk.network": "后端信号-网络相关操作: acli network {bond list|nic list|anet vrouter list|port check|...}",
    "qfk.storage": "后端信号-存储相关操作: acli storage {asan disk list|asan volume list|mount|sffsck|...}",
    "qfk.hardware": "后端信号-硬件相关操作: acli hardware {cpu microcode file list|gpu config list|...}",
    "qfk.platform": "后端信号-平台相关操作: acli platform {info get|node get|version get|mysql config get|...}",
}
```

---

## 六、结论

### 6.1 核心纠正

1. **"缺 host.shell"是错误判断**：acli 的 `system` namespace 就是受限的 host.shell，封装了 37 个主机级命令。KBD 中写的 `lsof/ps/lsblk/iostat/smartctl` 全部可通过 `acli system lsof/ps/lsblk/iostat/smartctl` 执行。
2. **原 QFK 设计粒度错误**：把 acli namespace 当信号类型枚举，丢失了子命令的多样性。修正后以 namespace 为采集器，子命令为参数。
3. **覆盖率从 11% 跃升到 88%**：修正后仅 2 个步骤（第三方操作）无法自动化。

### 6.2 QKV/QFK 的本质差异

```
QKV 复杂在结果（解析字段多） -> produces 自定义
QFK 复杂在方法（命令参数多） -> subcommands + params 自定义
两者统一：acquirer + acquirer_args + {{VAR}} 变量池
```

### 6.3 下一步

1. **改造 QFK HandlerRegistry**：去掉枚举，改为基于 `acquirer` namespace 的命令模板构建
2. **改造 QKV 产出**：根据 `produces` 列表动态提取字段
3. **更新 ACQUIRER_CATALOG**：从 11 项改为 11 项（3 QKV + 8 QFK），但 QFK 的 8 项从"信号类型"改为"namespace"
4. **更新抽取 Prompt**：注入新的采集器目录 + 子命令清单 + 参数 schema
5. **验证**：对 5 个真实案例重新抽取信号，验证覆盖率

---

## 附录：acli 完整命令列表（336 条）

> 见 `/tmp/acli_catalog.json`（已抓取保存），按 14 个 namespace 分组：
> alert(2), command(1), hardware(4), log(4), network(50), platform(28), plugin(6), plugins(25), service(115), storage(19), sync(1), system(46), task(2), vm(33)
