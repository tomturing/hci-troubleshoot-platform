# 组件索引

路径默认相对于 `<NODE_ROOT>`，仅用于定位候选资料。路径是否存在、组件运行在哪个节点以及时间是否覆盖，必须在当前日志包确认。

| 组件 | 职责 | 配置/状态候选 | 日志候选与变体 | 关键关联 | 验证级别 |
|---|---|---|---|---|---|
| vm-manager | VM 生命周期、快照、备份编排 | `cfs/vmstatus.json`、`cfs/nodes/*/qemu-server/` | `sf/log/<DAY>/vm-manager-api.log*`；其他版本可位于组件目录 | VMID、UPID、trace、HostID | 单包样本 |
| vtp_vm_control/QEMU | 下发 VM 控制并记录虚拟化进程行为 | VM `.conf`、VM 清单 | `sfvt_vtp_vm_control.log*`、`sfvt_qemu_<VMID>.log*` | VMID、控制请求、磁盘/网卡 | 单包样本 |
| HA/scheduler/DRS | 故障恢复、迁移约束、调度 | `cfs/drs.conf`、`cfs/sched_policy/`、`tmp/drs/` | `vt/ha.log*`、`vt/scheduler.log*` | VMID、源/目标节点、资源 | 单包样本 |
| volume/vmdisk | 卷、副本和虚拟磁盘 | `cfs/storage.cfg`、`cfs/disk.info`、VM `.conf` | `volumeapi.log*`、`volume-worker-*.log*`、实际存在的 `vmdisk*` | storage/volume ID、路径、VMID | 单包样本；文件名有变体 |
| VN/SDN/dataplane | 虚拟网络控制和数据转发 | `cfs/vs/hosts_network_info.json`、`sf/cfg/if.d.*/` | `vn/`、`sdn/`、`networkd.log*`、`dataplane.log*` | IP、MAC、接口、VLAN/VXLAN、trace | 跨包样本 |
| pmxcfs/cfgdbd | 集群共享配置与同步 | `cfs/.members`、`cfs/cfgmaster.ini` | `pmxcfs.log*`、`cfgdbd.log*` | 节点、仲裁、主节点、同步 | 单包样本 |
| ZooKeeper/MySQL | 控制面协调和数据库 | `sf/etc/zookeeper/`、`sf/cfg/mysql/` | `zookeeper/`、`mysqld.log*` | election、connection、lock | 单包样本 |
| containerd/kubelet | 容器运行时和 Pod 管理 | `sf/etc/runtime-pods-config/` | `sf/log/containerd/`、`sf/log/kubelet/` | Pod、容器、镜像、mount | 跨包样本 |
| sf-openapi/Web/WAF | 管理 API、Web 入口和防护 | `sf/etc/sf-openapi/`、Web 配置 | `sf/log/sf-openapi/`、`apache2/`、`sangfor_waf/` | trace、URI、HTTP 状态码 | 跨包样本 |
| host-agentd | 节点代理、管理协同 | 服务配置和进程快照 | `sf/log/host-agentd/` | 节点、API、服务启动 | 跨包样本 |
| 告警中心 | 检测、历史记录、推送、展示 | `cfs/alert.conf`、`cfs/alert/` | `sf/log/log_new.db` 或 `sf/data/local/log_new.db`；`*vtpalertd*`、`*vtplogd*`、`alert-pushd*` | 时间、type、object_id、log_id、UPID、trace | 跨包数据库路径样本 |
| hardware/kernel | 硬件、驱动、内核和崩溃 | `cmd-output/*dmidecode*`、`*lspci*` | `mcelog.log*`、`*kernel*`、`kdump/`、`pstore/` | 设备标识、BDF、时间 | 路径模式样本 |
| VTP tasks/backup | 备份、快照、归档和维护任务 | `cfs/backup/`、`cfs/backup.json` | `sf/log/vtp/tasks/`、`sfvt_qemu-backup*.log*` | UPID、VMID、资源 ID | 单包样本 |

## 标识关联优先级

1. 告警：时间＋告警文字 → `type/object_id/log_id` → 用户确认 → 业务组件。
2. VM：VMID → VM 配置/清单 → 所在节点 → vm-manager/control/QEMU → volume/VN。
3. API：trace/URI → Web access/error → OpenAPI → 后端组件。
4. 任务：UPID → task 文件 → VMID/资源 ID → 执行组件。
5. 网络：接口/IP/MAC → VN/SDN → dataplane → 网络黑盒。
6. 硬件：时间＋设备标识 → IPMI/MCE → kernel/driver。

## 路径变体处理

- 先按表中精确候选路径检查；不存在时，只在相关节点和合理深度内按组件名有限查找。
- 新发现的路径只能作为当前案件事实；未验证前不能反向写成产品固定路径。
- 同名日志存在多个节点时，必须保留节点来源，不得合并后丢失上下文。

