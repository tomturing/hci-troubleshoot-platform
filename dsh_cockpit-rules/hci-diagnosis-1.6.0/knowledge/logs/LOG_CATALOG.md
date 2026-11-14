# 产品日志参考目录

本页是“参考书目录”，用于从逻辑资料定位当前支持包中的候选文件。它不是逐文件清单，不保存客户现场的文件数量、大小、主机、日期或时间范围。

## 阅读方法

1. 路径默认相对于 `<NODE_ROOT>`，案件级元数据会显式标记 `<CASE_ROOT>`。
2. `路径模式` 是候选，不保证每个版本、节点或采集包都存在。
3. 先由 [ROUTES.md](ROUTES.md) 选章节，再在当前包做有限路径确认。
4. 日期目录、组件目录和滚动文件可能并存，时间处理见 [TIME_RANGES.md](TIME_RANGES.md)。
5. 发现新路径时先记录为当前案件事实；只有跨版本验证后才能升级为稳定知识。

## 第 1 章：案件和节点元数据

| 路径模式 | 内容 | 用途 | 读取建议 |
|---|---|---|---|
| `<CASE_ROOT>/_members` | 节点、成员或采集对象信息 | 建立节点候选和角色关系 | 小文件可读正文；字段按实际格式解释 |
| `<CASE_ROOT>/cfgmaster_ini`、节点内 `cfs/cfgmaster.ini` | 配置主节点线索 | 定位控制面相关节点 | 与成员、仲裁日志交叉验证 |
| `<CASE_ROOT>/collect_record.txt` | 采集命令、成功/失败信息 | 判断资料是否采集成功 | 按目标路径或命令检索 |
| `<CASE_ROOT>/log_list.json` | 采集器记录的文件/项目元数据 | 辅助判断收录范围 | 只在实际存在时使用，不当作产品日志正文 |
| `<NODE_BUNDLE>` | 单节点解压资料 | 保留证据来源节点 | 多节点案件不得先合并再分析 |
| `<CASE_ROOT>/metadata/`、`nodes/<节点>/metadata/` | 新布局中的采集范围、版本与执行记录 | 识别实际已收集/失败/排除项 | 按本包 summary/files/commands 的实际字段解释 |
| `nodes/<节点>/`、未展开节点归档、重复嵌套节点目录 | 节点原文候选布局 | 建立当前节点到实际成员映射 | 名称或有 sf/log 目录不能单独证明权威副本/完整性 |

## 第 2 章：`cfs/` 集群配置与业务对象

| 路径模式 | 内容 | 关键字段/对象 | 主要用途 |
|---|---|---|---|
| `cfs/.members`、`cfs/cfgmaster.ini` | 成员、仲裁和配置主节点 | node、IP、HostID、quorum/master | 集群、配置同步、节点映射 |
| `cfs/.vmlist` | VM 清单（部分版本） | VMID、名称、节点 | 生成 VM 候选 |
| `sf/etc/.vmlist` | VM 清单路径变体 | VMID、名称、节点 | 与 `cfs/.vmlist` 二选一或并存 |
| `cfs/vmstatus.json` | VM 采集时状态 | VMID、状态、节点 | 状态不一致、节点归属 |
| `cfs/nodes/*/qemu-server/*.conf` | VM 定义 | VMID、CPU、内存、磁盘、网卡、存储 ID | 生命周期、存储、网络关联 |
| `cfs/storage.cfg`、`cfs/disk.info` | 存储和磁盘定义 | storage/volume/device ID | 存储、卷、多路径 |
| `cfs/drs.conf`、`cfs/sched_policy/` | 调度策略与约束 | 策略、节点、资源 | 迁移、HA、DRS |
| `cfs/vs/hosts_network_info.json` | 主机网络信息 | 节点、接口、IP、网络 | 管理网和虚拟网络 |
| `cfs/backup/`、`cfs/backup.json`、`cfs/sys_backup/` | 备份策略和状态 | 策略、VMID、目标、周期 | 备份、快照、归档 |
| `cfs/alert.conf`、`cfs/alert/` | 告警阈值、屏蔽和通知配置 | 类型、阈值、通知目标 | 解释告警策略，不代表告警历史 |
| `cfs/priv/`、`cfs/certs/`、用户权限配置 | 密钥、证书、用户、ACL | 敏感 | 仅验证存在性/配置项，遵循脱敏要求 |

## 第 3 章：`sf/log/` 平台活动日志

平台日志常见两类布局：

- 日期目录：`sf/log/<DAY>/<file>`；
- 组件目录：`sf/log/<component>/<active-or-rotated-file>`。

| 日志族/模式 | 内容 | 常用关联字段 | 注意事项 |
|---|---|---|---|
| `vm-manager-api.log*` | VM 生命周期、快照、迁移编排 | VMID、UPID、trace、HostID | 先确认所在节点和故障窗口 |
| `sfvt_vtp_vm_control.log*` | VM 控制请求和返回 | VMID、request/reply、error | 与 vm-manager、QEMU 同窗关联 |
| `sfvt_qemu_<VMID>.log*` | VM 专属 QEMU 行为 | VMID、disk、net、QMP、exit | 文件可能位于日期目录或归档 |
| `vt/ha.log*`、`vt/scheduler.log*` | HA、调度、资源决策 | VMID、源/目标节点、constraint | 同时检查控制节点及执行节点 |
| `volumeapi.log*`、`volume-worker-*.log*` | 卷和副本编排 | storage/volume ID、VMID、trace | 文件名可能带 `sfvt_` 前缀 |
| `vmdisk*.log*` | 虚拟磁盘服务 | disk/volume ID、路径、VMID | 只使用当前包实际存在的文件名 |
| `pmxcfs.log*`、`cfgdbd.log*` | 配置文件系统和配置数据库 | node、master、sync、quorum | 路径可能跨不同日期目录 |
| `zookeeper/`、`mysqld.log*` | 协调和控制面数据库 | connection、election、lock | 数据库错误需与调用方交叉验证 |
| `networkd.log*`、`dataplane.log*` | 网络配置和转发面 | interface、IP、MAC、trace | 与 VN/SDN 和命令快照关联 |
| `kernel.log*`、`mcelog.log*` | 内核与机器检查异常 | 时间、设备、BDF、MCE/EDAC | 时区和设备标识必须校准 |
| `boot.log`、服务启动日志 | 系统或服务启动 | service、PID、status | 识别重启和启动失败 |

## 第 4 章：告警和界面事件

| 路径模式 | 内容 | 关键字段 | 读取建议 |
|---|---|---|---|
| `sf/log/log_new.db` | 告警/事件 SQLite 路径变体 | 以实际 schema 为准 | 只读、限定字段和条件 |
| `sf/data/local/log_new.db` | 同类数据库另一已知路径 | 同上 | 不假设两条路径同时存在 |
| `*vtpalertd*.log*` | 告警检测和生成 | time、type、object、log_id | 数据库无记录时查生成链 |
| `*vtplogd*.log*` | 告警解析、翻译和入库 | log_id、type、DB error | 与数据库记录对应 |
| `alert-pushd.log*` | 告警推送 | logID、trace、channel、result | 用于外部通知问题 |
| `tmp/.alert.*` | 当前检测状态快照 | 设备/VM/指标 | 不作为历史告警清单 |

详细候选交互见 [ALERTS.md](ALERTS.md)。

操作报错应先找对应 task/请求，告警入库只是另一观察。对资源不足、同步卡住等现象，沿原文查产生结论的输入、状态判据及依赖返回，不将告警文本重复计算为独立证据。

## 第 5 章：`sf/log/vn/`、`sf/log/sdn/` 与网络黑盒

| 路径模式 | 内容 | 关键字段 | 用途 |
|---|---|---|---|
| `sf/log/<DAY>/vn/` 或其他 `vn/` | 虚拟网络控制日志 | VMID、IP、MAC、trace、VLAN/VXLAN | 虚拟网络配置和控制 |
| `sf/log/sdn/networkd.out` | 网络配置服务 | interface、route、link | 地址、路由、链路问题 |
| `sf/log/sdn/dataplane.out` | 数据转发面 | flow、port、drop | 丢包和转发异常 |
| `sf/log/sdn/dhcpd.out`、`dhcp6d.out` | DHCP 服务 | MAC、IP、lease | 地址分配 |
| `sf/log/sdn/dnsd.out` | DNS 服务 | query、name、result | 解析问题 |
| `sf/log/sdn/tunneld.out`、`mrouted.out` | 隧道和组播 | tunnel、peer、route | 隧道/组播故障 |
| `sf/log/sdn/cfgdbd.out`、`locald.out`、`appinitd.out`、`monitord.out` | SDN 配置、节点服务和监控 | node、config、service | SDN 控制链 |
| `sf/log/vn-blackbox/<DATE>/` 或归档 | 网络故障快照 | 时间、接口、流量、错误 | 只读命中日期和指标 |

## 第 6 章：Web、API 与容器服务

| 路径模式 | 内容 | 关键字段 | 读取建议 |
|---|---|---|---|
| `sf/log/apache2/access.log*`、`api_access*` | HTTP 访问 | time、URI、status、client、trace | 先按时间/URI/status 过滤并脱敏 |
| `sf/log/apache2/error.log*` | Web 入口错误 | time、module、error | 与 access 同时间关联 |
| `sf/log/sf-openapi/app.log*` | 管理 API | trace、URI、backend、exception | 串联 Web 与后端组件 |
| `sf/log/sangfor_waf/` | WAF 配置、错误和明细 | URI、rule、action、client | 注意请求内容敏感性 |
| `sf/log/containerd/` | 容器运行时与 snapshotter | container、image、mount、error | 文件可能按日期后缀轮转 |
| `sf/log/kubelet/` | Pod 生命周期和节点管理 | pod、container、volume、probe | 与 containerd、业务服务关联 |
| `sf/log/host-agentd/` | 节点代理 | node、service、request | 常见 `app.log` 及日期轮转 `.gz` |
| `sf/etc/runtime-pods-config/` | Pod/运行时配置 | pod、image、mount | 配置事实，不代表运行成功 |

## 第 7 章：任务、备份和维护

| 路径模式 | 内容 | 关键字段 | 读取建议 |
|---|---|---|---|
| `sf/log/vtp/tasks/` | 后台任务详情 | UPID、VMID、node、status | 先按 UPID 定位单个任务 |
| `sfvt_qemu-backup*.log*` | QEMU 备份执行 | VMID、snapshot、target、error | 与 task、vm-manager、volume 关联 |
| vm-manager/volume 日志 | 编排和卷操作 | trace、VMID、volume ID | 同一时间窗口交叉验证 |
| `cfs/backup*`、`cfs/sys_backup/` | 策略与系统备份配置 | policy、target、schedule | 区分配置与执行结果 |

## 第 8 章：`cmd-output/` 现场命令快照

文件名由采集器命令转义生成，不能假设完全固定。优先按命令语义匹配。

若存在 commands.tsv 等记录，先核执行状态、节点/命名空间和时间。旧包的 cmd-output 内也可能含专项采集子目录/归档，应有限发现实际成员；未展开或路径不符不能当作未采集。新旧布局均不从共享采集器版本推断已完成项。

| 文件名关键词 | 内容 | 用途 |
|---|---|---|
| `uptime`、`top`、`free`、`meminfo`、`cpuinfo` | 负载、CPU、内存快照 | 性能和资源判断 |
| `multipath`、`lsblk`、`lsscsi`、`mount`、`df` | 磁盘、多路径和文件系统 | 存储路径和容量 |
| `ipmitool_sel`、`sensor` | BMC 事件和传感器 | 硬件告警；先校准 BMC 时间 |
| `lspci`、`dmidecode`、`gpu`、`nvidia` | PCI、内存、服务器和 GPU | 设备识别和硬件配置 |
| `ip_addr`、`ip_route`、`ethtool`、`bridge`、`ovs` | 接口、路由、链路和虚拟交换 | 网络现状 |
| `dmesg`、`uname`、`lsmod` | 内核、版本和模块 | 驱动、启动、硬件错误 |
| `ps`、`systemctl`、服务状态 | 进程和服务快照 | 服务是否运行；采集时刻事实 |
| `*.sql` | 数据库导出 | 先检索表/对象，禁止整份加载 |

## 第 9 章：blackbox、kdump、dump 与历史资料

| 路径模式 | 内容 | 用途 | 限制 |
|---|---|---|---|
| `sf/log/blackbox/<YYYYMMDD>/` | 系统/产品故障快照 | 性能、资源和历史回溯 | 只读取目标日期和指标 |
| `sf/log/blackbox/<YYYYMMDD>.tar.gz` | 压缩历史快照 | 同上 | 先列成员 |
| `sf/log/vn-blackbox/` | 虚拟网络快照 | 网络丢包、接口和流量 | 需与业务网络日志关联 |
| `sf/log/kdump/` | 内核崩溃资料 | panic/崩溃回溯 | 先核对崩溃时间是否相关 |
| `pstore/`、`dump/` | 持久化内核/用户态转储 | 崩溃分析 | 可能未采集；二进制不直接加载 |
| `sf/log/<DAY>.tar.zst` | 日期日志归档 | 补充滚动前历史 | 先列成员、定向提取 |

## 第 10 章：`tmp/` 临时状态

| 路径模式 | 内容 | 用途 | 限制 |
|---|---|---|---|
| `tmp/drs/` | 调度输入、建议或状态 | DRS/迁移决策 | 采集时快照 |
| `tmp/.alert.*` | 告警检测状态 | 硬件、磁盘、网络、VM 候选线索 | 不代表历史告警 |
| 其他 `tmp/` 文件 | 组件临时状态 | 辅助验证 | 生命周期短，语义必须由正文确认 |

## 格式与读取策略

| 格式 | 首次读取 | 后续检索 |
|---|---|---|
| 小型文本/JSON/配置 | 读取正文确认 schema 和用途 | 按对象字段精确匹配 |
| 大型文本 | 先取大小、首尾时间和少量样本 | 按时间＋对象＋错误特征检索上下文 |
| `.gz` | 查看文件名并流式抽样 | 定向流式搜索，不批量解压 |
| `.tar.gz`/`.tar.zst` | 先列成员 | 只提取目标成员到临时目录 |
| SQLite | 只读查看表和字段 | 限定时间、对象、列和返回行数 |
| SQL 导出 | 先找表结构/目标 ID | 仅截取相关语句附近内容 |
| JSON | 先确认顶层结构 | 使用键/对象 ID 过滤，不整份加入上下文 |
| core/二进制 | 仅记录元数据 | 使用专用工具；不直接作为文本读取 |

## 可信度标记

- `产品规则`：由稳定产品设计或正式资料确认。
- `跨包样本`：在不同结构的多个支持包中验证。
- `单包样本`：只在一类支持包中验证，可能存在版本限制。
- `路径推断`：仅由名称或邻近结构推断，只能导航，不能形成事实结论。

当前目录中的具体路径多数属于跨包样本或单包样本。诊断输出必须引用当前案件的实际文件，而不能只引用本目录说明。
