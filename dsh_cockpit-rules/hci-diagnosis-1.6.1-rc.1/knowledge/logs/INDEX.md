# 产品日志知识入口

本目录保存跨案件复用的产品日志知识，不记录某个支持包的主机、IP、集群名称、采集时间、文件数量或实际时间范围。诊断时先用本页选择资料类型，再在当前 `Q...` 日志包中确认真实路径。

## 路径模型

- `<CASE_ROOT>`：当前案件目录，例如某个 `Q...` 目录。
- `<NODE_BUNDLE>`：节点解压目录；一个案件可以包含一个或多个节点目录。
- `<NODE_ROOT>`：`<CASE_ROOT>/<NODE_BUNDLE>`。
- `<DAY>`：日志采用日期目录时的日号，只能根据故障时间和实际目录确定。
- `<YYYYMMDD>`、`<YYYY-MM-DD>`：日志或归档中的日期占位符。
- `<VMID>`、`<UPID>`、`<TRACE_ID>`：从用户信息、告警或日志中提取的关联标识，禁止凭空生成。

本文档中的路径默认相对于 `<NODE_ROOT>`。支持包根部的 `_members`、`collect_record.txt`、`log_list.json` 等采集元数据相对于 `<CASE_ROOT>`。不同版本和采集方式可能使用不同路径布局，实际文件优先于知识说明。

## 使用顺序

1. 确认当前 `<CASE_ROOT>`，识别所有 `<NODE_BUNDLE>`，不要预设只有一个节点。
2. 从用户问题和包内记录缩小对象/事件；仅在影响判断的歧义无法自行区分时追问。静态查询不强求故障时间。
3. 打开 [故障域路由](ROUTES.md) 选择起始来源，沿实际对象、请求与时间关系按需跨域。
4. 用 [组件索引](COMPONENTS.md) 和 [日志参考目录](LOG_CATALOG.md) 将逻辑资料映射到当前包的实际路径。
5. 用 [时间规则](TIME_RANGES.md) 检查覆盖范围，用 [采集完整性](COLLECTION_GAPS.md) 判断缺失是否由采集造成。
6. 从相关来源和已知窗口开始，按发现的机制扩展到必要历史；查询无命中也可核路径/覆盖和关联组件，结论必须回到实际原文。

## 快速路由

| 故障现象 | 首查资料 | 关联资料 |
|---|---|---|
| 界面告警、报错、弹窗 | `log_new.db` 的告警表或告警处理日志 | OpenAPI、Web、对应业务组件；见 [ALERTS.md](ALERTS.md) |
| VM 启停、状态、卡死 | VM 状态/配置、vm-manager | 控制链、QEMU、volume、VN |
| 迁移、HA、DRS、调度 | HA、scheduler、DRS、任务记录 | vm-manager、集群、资源状态 |
| 存储、磁盘、卷、多路径 | storage 配置、volume/vmdisk | VM 配置、multipath、kernel |
| 管理网、业务网、SDN | 网络配置、VN/SDN | dataplane、networkd、网络黑盒 |
| 集群、仲裁、同步、数据库 | 成员/主节点信息、pmxcfs/cfgdbd | ZooKeeper、MySQL、corosync |
| 容器、Kubelet、API、页面 | kubelet/containerd、OpenAPI/Web | host-agentd、运行时配置 |
| CPU、内存、PCI、GPU、IPMI、MCE | 命令快照、MCE、kernel | kdump、驱动和硬件告警 |
| 性能、负载、历史回溯 | uptime、blackbox | kernel、组件日志、业务基线 |
| 备份、快照、归档 | task、backup 配置 | vm-manager、QEMU、volume |
| 采集完整性 | 采集记录和当前包实际目录 | 压缩归档、替代证据 |

用户问操作失败时优先锁定对应任务/请求，不用同时段告警替换该任务。主机、服务、硬件和静态配置问题直接从相关对象出发。案例/手册等已可用时见 [KNOWLEDGE_USE.md](KNOWLEDGE_USE.md)，无案例继续自主取证。

## 文档职责

- [ROUTES.md](ROUTES.md)：故障现象到首查资料、关联资料和检索参数。
- [COMPONENTS.md](COMPONENTS.md)：组件职责、路径变体、关联标识和上下游。
- [LOG_CATALOG.md](LOG_CATALOG.md)：按目录和文件类型说明内容、格式和读取方法。
- [ALERTS.md](ALERTS.md)：告警候选、用户确认以及告警链路验证。
- [TIME_RANGES.md](TIME_RANGES.md)：动态判断时间范围、轮转、归档和时区。
- [COLLECTION_GAPS.md](COLLECTION_GAPS.md)：判断缺失、空文件和采集失败。
- [SENSITIVE_DATA.md](SENSITIVE_DATA.md)：敏感路径和脱敏要求。
- [SCENARIO_CHECKS.md](SCENARIO_CHECKS.md)：开机、镜像占用、日志缺失、网络和容量等问题的按需区分检查。
- [KNOWLEDGE_USE.md](KNOWLEDGE_USE.md)：错误码/特征/场景检索、知识适用性与方案出处。

## 强制约束

1. 知识中的路径是候选路径，不证明当前包中一定存在。
2. 禁止无目的遍历所有日志；先限定节点、对象、时间和关键词。
3. SQLite、SQL、`.gz`、`.tar.gz`、`.tar.zst` 和大日志不得整份载入上下文。
4. 未找到记录不等于事件未发生，必须先检查节点、时间覆盖、轮转和采集状态。
5. 按断言类型和作用关系判断证据强度，单条可信记录可支持限定判断；多文件可能同源，错误转述不能充当独立证明。
6. 路径说明的可信度分为：产品规则、跨包样本、单包样本、路径推断；后两者只用于导航。

