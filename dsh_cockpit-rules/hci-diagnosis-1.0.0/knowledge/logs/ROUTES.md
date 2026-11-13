# 故障域路由

路径默认相对于 `<NODE_ROOT>`。同一逻辑日志可能采用日期目录、组件目录或轮转文件等布局；先在当前包确认实际路径。每次检索至少限定故障时间和一个对象标识。

## alerts-events｜界面告警、报错与弹窗

| 字段 | 内容 |
|---|---|
| 组件 | vtpalertd、vtplogd、alert-pushd、OpenAPI/Web、业务组件 |
| 资料用途 | 先生成告警候选让用户确认，再验证检测、入库、推送、展示和真实业务异常 |
| 首查路径 | `sf/log/log_new.db`、`sf/data/local/log_new.db`；详见 [ALERTS.md](ALERTS.md) |
| 关联路径 | `sf/log/<DAY>/sfvt_vtpalertd.log*`、`sfvt_vtplogd.log*`、`alert-pushd.log*`、`sf/log/sf-openapi/`、`sf/log/apache2/`、`tmp/.alert.*` |
| 检索参数 | 告警时间、界面文字、对象名/IP、VMID、`type`、`object_id`、`log_id`、UPID、trace |
| 读取建议 | 优先查询数据库候选；数据库不存在时再查告警处理日志；用户确认后转入对应业务故障域 |
| 可信度 | 数据库结构和两类支持包路径经样本验证；具体路径需现场确认 |

## vm-lifecycle｜虚拟机生命周期

| 字段 | 内容 |
|---|---|
| 组件 | vm-manager、vtp_vm_control、QEMU |
| 资料用途 | VM 配置、当前状态、启停控制链和虚拟化进程行为 |
| 首查路径 | `cfs/vmstatus.json`；`cfs/.vmlist` 或 `sf/etc/.vmlist`；`cfs/nodes/*/qemu-server/`；`sf/log/<DAY>/vm-manager-api.log*` |
| 关联路径 | `sf/log/<DAY>/sfvt_vtp_vm_control.log*`；`sf/log/<DAY>/sfvt_qemu_<VMID>.log*`；任务、volume、VN 日志 |
| 检索参数 | VMID、VM 名称、宿主机、时间、UPID、trace、`start/stop/power/qemu/timeout/error` |
| 读取建议 | 先由清单/配置确定 VMID 和所在节点，再串联控制层、QEMU、存储和网络；不要跨节点混用同名日志 |
| 可信度 | 配置和日期目录布局经样本验证；组件目录变体需现场确认 |

## migration-ha-drs｜迁移、HA 与调度

| 字段 | 内容 |
|---|---|
| 组件 | HA、scheduler、DRS、vm-manager、集群控制面 |
| 资料用途 | 迁移请求、故障接管、调度建议、资源约束和目标节点选择 |
| 首查路径 | `sf/log/<DAY>/vt/ha.log*`、`sf/log/<DAY>/vt/scheduler.log*`、`tmp/drs/` |
| 关联路径 | `cfs/drs.conf`、`cfs/sched_policy/`、vm-manager、`sf/log/vtp/tasks/`、源/目标节点资源日志 |
| 检索参数 | VMID、源/目标 HostID、UPID、trace、`migrate/recovery/constraint/route/resource` |
| 读取建议 | 先确认策略和决策，再同时检查源节点、目标节点与控制节点的同一时间窗口 |
| 可信度 | 单包正文/样本验证；路径和角色需现场确认 |

## storage-volume｜存储、磁盘与卷

| 字段 | 内容 |
|---|---|
| 组件 | volume、vmdisk、存储策略、多路径、kernel |
| 资料用途 | 存储定义、卷副本、虚拟磁盘操作、共享盘和底层路径状态 |
| 首查路径 | `cfs/storage.cfg`、VM `.conf`、`sf/log/<DAY>/volumeapi.log*`、`volume-worker-*.log*` |
| 关联路径 | `sfvt_volume-worker-server.log*`、实际存在的 vmdisk 日志、`cmd-output/*multipath*`、`cfs/disk.info`、kernel |
| 检索参数 | VMID、storage ID、volume ID、磁盘路径、设备名、`replica/qcow2/I/O/timeout/error` |
| 读取建议 | 先从 VM 配置取得存储标识，再贯穿 volume/vmdisk/多路径/kernel；不存在的文件名不得当成固定路径 |
| 可信度 | 配置和 volume 路径经样本验证；vmdisk 文件名存在版本差异 |

## network-sdn｜物理、管理与虚拟网络

| 字段 | 内容 |
|---|---|
| 组件 | networkd、VN、SDN、dataplane、物理网卡 |
| 资料用途 | 管理/业务网络配置、虚拟网络控制面、转发面和网络快照 |
| 首查路径 | `sf/log/<DAY>/vn/`、`sf/log/sdn/`、`sf/cfg/if.d.*/` |
| 关联路径 | `networkd.log*`、`dataplane.log*`、`sf/log/vn-blackbox/`、`cfs/vs/hosts_network_info.json`、网卡命令输出 |
| 检索参数 | IP、MAC、接口名、VMID、trace、VLAN/VXLAN、`link/route/bridge/drop/timeout` |
| 读取建议 | 先判断物理、管理、业务或虚拟网络层级，再用接口/IP/MAC/trace 定向关联 |
| 可信度 | 日期目录和组件目录两种布局经样本验证 |

## cluster-control｜集群与控制面

| 字段 | 内容 |
|---|---|
| 组件 | pmxcfs、cfgdbd、ZooKeeper、MySQL、corosync |
| 资料用途 | 成员、仲裁、配置主节点、共享配置同步和控制面数据服务 |
| 首查路径 | `<CASE_ROOT>/_members`、`cfs/.members`、`cfs/cfgmaster.ini`、`pmxcfs.log*` |
| 关联路径 | `cfgdbd.log*`、`zookeeper/`、`mysqld.log*`、`sf/cfg/cluster/`、corosync 资料 |
| 检索参数 | 节点名、IP、HostID、`quorum/sync/master/election/connection/timeout` |
| 读取建议 | 先建立节点角色和采集来源，再核对故障时间的仲裁、同步、选主及数据库连接 |
| 可信度 | 成员配置经样本验证；日志布局随版本变化 |

## service-web-container｜服务、容器与管理面

| 字段 | 内容 |
|---|---|
| 组件 | containerd、kubelet、sf-openapi、Apache/其他 Web、host-agentd、WAF |
| 资料用途 | 服务启动、Pod/容器生命周期、API 调用、HTTP 错误和页面访问链 |
| 首查路径 | `sf/log/containerd/`、`sf/log/kubelet/`、`sf/log/sf-openapi/`、`sf/log/apache2/` |
| 关联路径 | `sf/log/host-agentd/`、`sf/log/sangfor_waf/`、`sf/etc/runtime-pods-config/`、服务初始化配置 |
| 检索参数 | 服务/Pod/容器名、trace、URI、HTTP 状态码、`failed/timeout/panic/restart` |
| 读取建议 | 页面问题先串联 Web access/error 与 API trace；容器问题按名称关联 kubelet、containerd 和业务日志 |
| 可信度 | 组件目录布局经跨包样本验证 |

## hardware-kernel｜硬件与内核

| 字段 | 内容 |
|---|---|
| 组件 | IPMI/BMC、MCE、kernel、kdump、PCI/GPU、驱动 |
| 资料用途 | 硬件事件、机器检查异常、内核启动/崩溃和设备状态 |
| 首查路径 | `cmd-output/*ipmitool*`、`sf/log/mcelog.log*`、`sf/log/<DAY>/kernel.log*` 或其他 kernel 日志 |
| 关联路径 | `sf/log/kdump/`、`pstore/`、`cmd-output/*lspci*`、`*dmidecode*`、`*gpu*`、告警库 |
| 检索参数 | 故障时间、传感器、DIMM/CPU/磁盘/PCI BDF、`MCE/EDAC/I/O error/Call Trace/panic` |
| 读取建议 | 先校准 BMC 时间，再以设备标识关联 MCE/kernel/驱动；历史 kdump 不等于本次故障 |
| 可信度 | 路径模式经样本验证；具体命令文件名由采集器决定 |

## performance-history｜性能与历史回溯

| 字段 | 内容 |
|---|---|
| 组件 | 系统负载、blackbox、内核、业务组件 |
| 资料用途 | 故障现场快照、性能趋势和跨组件异常时序 |
| 首查路径 | `cmd-output/*uptime*`、`sf/log/blackbox/<YYYYMMDD>/` 或同日归档 |
| 关联路径 | `sf/log/vn-blackbox/`、CPU/内存/磁盘/网络命令输出、对应组件日志 |
| 检索参数 | 故障时间、节点、VMID、CPU、load、memory、latency、iowait、drop |
| 读取建议 | 只读命中日期和指标；负载必须结合 CPU 核数、持续时间和业务基线解释 |
| 可信度 | 日期目录和归档模式经样本验证 |

## backup-snapshot｜备份、快照与归档

| 字段 | 内容 |
|---|---|
| 组件 | VTP task、vm-manager、QEMU backup、volume |
| 资料用途 | 任务状态、备份策略、快照/归档执行链和副本状态 |
| 首查路径 | `sf/log/vtp/tasks/`、`cfs/backup/`、`cfs/backup.json` |
| 关联路径 | vm-manager、`sfvt_qemu-backup*.log*`、volume、`cfs/sys_backup/` |
| 检索参数 | UPID、VMID、策略名、trace、`backup/snapshot/archive/replica/status/error` |
| 读取建议 | 先以 UPID/VMID 找任务结果，再沿 trace 或资源 ID 关联执行组件 |
| 可信度 | 任务和配置路径经样本验证；业务日志布局需现场确认 |

## collection-gaps｜采集完整性

| 字段 | 内容 |
|---|---|
| 组件 | 支持包采集器和各节点资料 |
| 资料用途 | 判断无证据是否源于节点选择、时间范围、采集失败、路径变体或压缩未读 |
| 首查路径 | `<CASE_ROOT>` 下实际存在的采集记录和节点目录 |
| 关联路径 | [COLLECTION_GAPS.md](COLLECTION_GAPS.md)、[TIME_RANGES.md](TIME_RANGES.md) |
| 检索参数 | 目标组件、节点、原始路径、命令、`error/failed/No such file/permission` |
| 读取建议 | 任何基于“未出现”的判断，都先完成可用性检查 |
| 可信度 | 判定规则；具体缺口必须由当前包验证 |

