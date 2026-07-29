---
status: active
category: solution
audience: all
last_updated: 2026-07-29
owner: team
update_trigger: HCI 产品版本、aCLI 版本、设备能力清单、QKV/QFK 契约或安全策略变化
---

# HCI 底层目录、日志、容器与 aCLI 知识基线

> 本文是知识生产、专家复核和 Agent 消费共同使用的底层事实基线。它回答的不是“某条命令在一台机器上跑通了吗”，而是“什么事实可以被稳定抽象成知识，什么能力可以安全执行，什么仍需跨版本验证”。
>
> **适用边界**：本文的设备观察基线为 **HCI 6.11.1_R1 + aCLI 1.0.0**。其中“观察节点事实”不能自动推广为所有 HCI 版本的产品契约；只有经设备 manifest、实现源码或跨版本验证确认的内容，才可升级为通用契约。

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|---|---|---|---|
| 2026-07-29 | v1.0 | 首次形成 HCI 日志、配置、数据、补丁、容器、aCLI 实机知识基线，并给出对 Signal Schema、Prompt、数据库、Admin UI 和 Agent 的演进建议 | [KBD 专家复核与全生命周期闭环方案](../events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |

---

## 1. 目的、非目标与安全边界

### 1.1 目的

本文服务于四个直接目标：

1. 让 KBD 生产器只生成客户现场真实可获取、可判定、可复现的关键信号；
2. 让专家审核时能看懂“信号从哪里取、实际执行什么、为什么能证明或排除案例”；
3. 让 Agent 在目标 HCI 的真实能力边界内执行，而不是把文档示例当作可执行事实；
4. 让 HTP 根据产品版本与 aCLI 版本持续发现能力漂移，并快速闭环新知识与新工具能力。

### 1.2 非目标

- 不把 HTP 建成复杂审批流系统；
- 不开放自由 Shell 作为知识无法执行时的默认兜底；
- 不因单机文件存在、目录名或一次命令输出就推断全产品行为；
- 不在本文记录凭据、Cookie、Token、私钥、业务数据或完整敏感配置；
- 不把“Schema 中存在一个工具名”误写成“Agent 已实现且目标设备可执行”。

### 1.3 本轮探索边界

本轮仅执行限量、只读意图的目录、挂载、版本、帮助和 manifest 检查；未执行配置写入、服务重启、补丁安装、文件删除或变更型 aCLI。需要特别说明：aCLI 会固有地写入命令审计日志；查询历史白盒日志还可能按需解压归档。因此“只读意图”不等于“文件系统零副作用”。

### 1.4 事实分级

| 等级 | 含义 | 可否直接进入可执行契约 |
|---|---|---|
| F1 · 观察节点确认 | 在本次目标节点上直接观察到 | 仅可作为该环境事实；需版本约束 |
| F2 · 设备 manifest/实现确认 | 由设备端命令 manifest 或实现源码确认 | 可作为该 aCLI 版本的强契约 |
| F3 · 在线文档确认 | 由 aCLI 在线文档确认 | 需与设备版本核对；文档可能滞后 |
| F4 · HTP 当前实现 | 仓库当前代码、Schema、Prompt 或 UI 的行为 | 只说明“现在怎么做”，不证明正确 |
| H · 待验证假设 | 合理推断但尚未跨版本/跨节点确认 | 不得自动执行或作为发布阻断条件 |

文中使用“确认”时会给出版本或来源；使用“建议”时表示目标设计，不表示当前已经实现。

---

## 2. 环境指纹与兼容维度

### 2.1 观察基线

| 项目 | 观察值 | 事实等级 | 设计含义 |
|---|---|---|---|
| HCI 产品版本 | 6.11.1_R1 | F1 | 案例与能力必须携带产品版本范围 |
| HCI build 时间 | 2025-10-23 09:56:28 | F1 | 同版本不同 build 仍可能有差异 |
| support-version | 5.4.1 | F1 | 支持组件可能独立演进 |
| aCLI 版本 | 1.0.0 | F1 | 不能从 HCI 版本推导 aCLI 行为 |
| aCLI build 时间 | 2026-07-13 16:41:58 | F1 | 比本次仓库静态目录来源更新 |
| 内核 | 4.18.0-6.11.1_R1 | F1 | 系统检查工具不能盲套通用发行版假设 |
| `/etc/os-release` | 不存在 | F1 | Agent 不应依赖该文件识别 HCI |

### 2.2 第一性原理结论

“一个工具能否执行”至少由四个相互独立的条件决定：

```text
代码 Handler 已实现
  ∩ 目标设备实际提供该命令
  ∩ 参数/输出与目标版本兼容
  ∩ 当前安全策略允许
= 本次可执行 Capability
```

因此能力版本不能只绑定 `product_version`，最小兼容主键应包含：

```text
product_version + product_build + acli_version + manifest_hash + policy_version
```

其中 `product_build` 在兼容范围已被充分验证后可降为可选，但 `product_version` 与 `acli_version` 不应合并。

---

## 3. `/sf/log/`：白盒应用日志

### 3.1 挂载与目录语义

观察节点上 `/sf/log` 最终落在独立 ext3 分区，而不是普通根目录。判断日志容量必须对目标路径执行 `findmnt/df/stat`，不能只看根文件系统。

当前实时日志使用“月内日号”目录：

```text
/sf/log/28/
/sf/log/29/
/sf/log/today -> 29
```

关键结论：案例中常写的 `/sf/log/{date}/`，在该版本上实际应理解为：

```text
/sf/log/<D或DD>/
```

它不是 `YYYYMMDD`，也不是 `YYYY-MM-DD`。因此 Signal Schema 不能把所有日志日期统一为一个模糊的 `date` 字段。

### 3.2 日志组织方式

单日目录含上千个文件，既有按业务域/容器分层的目录：

```text
/sf/log/<DD>/vn/
/sf/log/<DD>/vs/
/sf/log/<DD>/vt/
/sf/log/<DD>/acli/
/sf/log/<DD>/audit_log/
/sf/log/<DD>/zookeeper/
```

也有平台根层日志，例如虚拟化调度、节点管理、内核与 CLI 日志。轮转文件至少同时存在：

```text
*.log
*.log.1
*.log.2.gz
```

日志行时间格式至少观察到两类：

```text
2026-07-29 16:45:06
2026/07/29 22:58:34
```

所以统一解析器必须支持多时间格式，并在无法解析时保留原始行与采集时间，不能因一种正则不匹配就丢弃 Evidence。

### 3.3 历史白盒日志

观察节点存在按日号归档的压缩包：

```text
/sf/log/1.tar.zst
...
/sf/log/30.tar.zst
```

设备实现表明：查询历史日期时，如果对应日目录尚未解压，会根据版本读取 `.tar.zst` 或 `.tar.gz` 并在 `/sf/log` 下按需解压；解压前还会估算可用空间。因此：

- 历史查询有磁盘写入与容量消耗；
- 日号在跨月时必须结合绝对日期解析，不能仅凭 `29` 判断是哪一个月；
- 该设备实现只接受今天至过去 29 天，保留天数仍需跨版本验证；
- Agent 执行前应先判断目录是否已经存在、目标归档是否存在和可用空间是否满足。

### 3.4 qfk_log 的直接设计结论

`qfk_log` 应至少表达：

```json
{
  "file": "sfvt_vtpdaemon.log",
  "path": "/sf/log/today/",
  "keyword": "由 match.pattern 表达",
  "date_semantics": "current_alias_or_day_of_month"
}
```

其中：

- `file` 只能是 basename，不能混入目录；
- `path` 必须经路径规范化与允许根校验；
- 匹配模式属于 `match`，不应再次塞入资源选择器；
- 日期必须最终解析成设备接受的绝对日期；
- 历史归档缺失或未解压时，安全策略需要感知其副作用。

---

## 4. `/sf/log/blackbox/<YYYYMMDD>/`：系统时间序列快照

### 4.1 与白盒日志的本质区别

观察节点的 blackbox 目录使用完整日期：

```text
/sf/log/blackbox/20260727/
/sf/log/blackbox/20260728/
/sf/log/blackbox/20260729/
```

单日包含约八十余种周期性系统快照，典型类型包括：

```text
进程用户态/内核态快照
cgroup 状态
vmstat / iostat / diskstats
meminfo / dmesg
df / mounts
网卡与 TCP 状态
sar 设备统计
存储卷与硬盘健康信息
```

白盒日志回答“哪个组件在何时记录了什么事件”；blackbox 回答“系统各项指标和状态随时间如何变化”。两者的文件选择、时间定位、解析器、证据语义和阈值判断均不同。

### 4.2 为什么不应强塞进现有 qfk_log

现有 `qfk_log(file, keyword)` 适合文本存在性或关键字匹配，但 blackbox 常见判断是：

- 某时间段 CPU、内存、IO 是否持续超阈值；
- 某设备计数器是否单调增长或突变；
- 故障前后挂载、进程、网络连接是否发生状态变化；
- 多个快照文件是否在同一时间窗相互印证。

这些判断需要结构化时间序列、数值比较、变化率、连续次数和多源关联。仅用 grep 容易产生“命中即成立”的伪结论。

### 4.3 建议能力

优先把 blackbox 建模为独立逻辑能力 `qfk_blackbox`；若短期仍复用 `qfk_log`，至少必须显式携带：

```text
log_family = blackbox
date_semantics = yyyymmdd
parser = <snapshot type>
predicate = compare/range/trend/change
```

示意契约：

```json
{
  "acquire": {
    "tool": "qfk_blackbox",
    "args": {
      "snapshot": "vmstat",
      "date": "{{INCIDENT_DATE}}",
      "start": "{{INCIDENT_START}}",
      "end": "{{INCIDENT_END}}"
    }
  },
  "match": {
    "type": "threshold",
    "field": "wa",
    "operator": ">=",
    "value": 20,
    "consecutive": 3
  }
}
```

这只是目标模型，尚未进入当前 Signal v2 契约。

---

## 5. `/cfs/`：集群配置与元数据视图

### 5.1 已确认布局

观察节点：

```text
/cfs -> /mnt/shared/cfs/
```

底层为 FUSE 挂载，容量较小，权限受限。已观察到的语义域包括：

```text
nodes/          节点级元数据
qemu-server/    虚拟机配置
vmdisk/         虚拟磁盘元数据
vs/             存储相关配置
backup/         备份元数据
sched_policy/   调度策略
start_order/    启动顺序
alert/          告警配置
auth/           认证相关区域
certs/          证书相关区域
priv/           敏感私有区域
```

在观察节点可见多节点结构，说明 `/cfs` 不是单机普通配置目录，而是集群级共享状态视图。

### 5.2 Agent 安全边界

- 默认禁止任意递归读取 `/cfs`；
- 只允许 Catalog 中登记的明确文件/字段；
- `priv`、`certs`、`auth` 等敏感域默认拒绝；
- 读取结果必须有大小、行数、超时与脱敏限制；
- 证据记录应包含节点、路径、mtime、内容摘要 hash 与截断状态；
- 不允许 LLM 自行拼接 `/cfs/{{用户输入}}` 路径。

### 5.3 知识生产规则

案例出现“查看 `/cfs/...`”时，抽取器不能直接生成自由 `cat`。应先映射到已注册的语义能力，例如“获取虚拟机配置字段”“获取节点列表”“获取调度策略”，由 Handler 固化安全路径与结构化输出。无法映射时，Proposal 应标记 `capability_gap`，交由专家补充工具能力，而不是发布不可执行信号。

---

## 6. `/sf/cfg/`：主机运行时与服务配置

### 6.1 已确认布局

观察节点：

```text
/sf/cfg -> /mnt/shared/sf/cfg
```

它是独立 ext3 挂载，包含 aCLI、告警、数据库、监控、虚拟化、网络、Zookeeper、服务定义、日志、SSH、SSL、证书与根 CA 等配置域。文件形态不只 `.conf`：

```text
.ini / .json / .conf / .cfg / .yaml
无扩展名文件
数据库及 WAL/共享内存文件
key / pem / pub 等密钥和证书材料
```

所以不能依据扩展名判断“是否安全”“是否配置文件”。

### 6.2 与 `/cfs` 的边界

| 目录 | 主要语义 | 风险 |
|---|---|---|
| `/cfs` | 集群级配置与元数据视图 | 读取可能跨节点/跨资源，含集群敏感信息 |
| `/sf/cfg` | 主机本地和运行时服务配置 | 含数据库凭据、密钥、证书及服务内部配置 |

两者都不能作为 qfk_log 的普通日志路径，也不应通过自由 Shell 暴露。

### 6.3 配置读取能力的最低要求

若案例确需读取配置，Handler 至少应实现：

1. 语义化路径 Catalog，而非任意路径；
2. 敏感目录与敏感字段 denylist；
3. 文件大小、输出行数和执行时间上限；
4. 密码、Token、私钥、证书正文等内容脱敏；
5. 结构化 parser 与明确允许返回的字段；
6. Evidence 中记录 hash、mtime、截断与脱敏状态；
7. 按 HCI/aCLI 版本声明兼容范围。

当前生产器将部分 `/sf/cfg` 日志样式描述转成 `qfk_system cat`，虽然没有扩大 qfk_log 白名单，但自由 `cat` 仍不能满足上述边界，应标记为过渡实现。

---

## 7. `/sf/data/`：多挂载、多数据语义的存储骨架

### 7.1 根目录容量不代表子目录

观察节点的 `/sf/data` 根是一个很小的 tmpfs 骨架，其下挂载多个真实文件系统：

```text
/sf/data/local/                 本地 sffs
/sf/data/platform_database/     平台数据库分区
/sf/data/datareport_database/   报表/时序数据分区
/sf/data/vs/local/...           本地存储卷与 device-mapper
/sf/data/vs/gfs/<storage>/      NFS/集群存储
/sf/data/<storage-id>/          NFS/集群存储
```

因此 `df -h /sf/data` 只反映骨架挂载，不能回答某个数据库、虚拟磁盘或存储池是否有空间。Agent 必须对目标子路径执行 `findmnt` 与 `df`。

### 7.2 主要语义区

| 路径 | 主要内容 | 默认策略 |
|---|---|---|
| `/sf/data/local/` | 本地主机数据、安装/升级/补丁、诊断、备份、ISO、部分本地虚拟机数据 | 仅开放明确登记的诊断子路径 |
| `/sf/data/platform_database/` | MySQL、Redis、Kafka、Zookeeper、MongoDB 等平台数据 | 禁止通用文件扫描；使用专用只读能力 |
| `/sf/data/datareport_database/` | RRD/报表数据 | 使用专用时序查询能力 |
| `/sf/data/vs/local/` | 本地存储卷、LVM 和 metadata | 高风险，只允许结构化存储状态查询 |
| `/sf/data/<storage-id>/` | 集群存储、ISO、volume、snapshot、backup | 高风险，不允许无界递归 |

设备端 `acli log get` 的实现仅允许 `/sf/data/local`，而不是整个 `/sf/data`。即使位于 `/sf/data/local`，也必须限制目录深度、文件数、文件大小、超时和输出量。

### 7.3 当前 HTP 差距

当前共享契约允许 `/sf/data/` 和 `/sf/datanew/` 前缀，范围比观察设备的 aCLI 实现更宽；同时仅用 `startswith()` 校验路径。这既可能生成设备拒绝的命令，也存在 `..` 路径段绕过根目录边界的风险。目标实现必须先规范化路径，再验证解析后的真实父目录。

---

## 8. 补丁、升级与版本生效证据

### 8.1 已观察目录

补丁和升级相关内容分散在：

```text
/boot/firmware/sp/
/sf/data/local/sp/
/sf/data/local/upgrade/
/sf/data/local/upgrade/upload/
/sf/data/local/pkg_download/
```

同名补丁包可能同时出现在固件、下载、上传或 staging 位置。个别目录中的同名文件可能只是占位或引用，不能仅靠文件名与存在性判定补丁已安装。

### 8.2 补丁状态模型

补丁知识必须区分：

```text
downloaded → uploaded → verified → staged → applied → effective
                                             ↘ rolled_back
```

| 状态 | 可接受证据 |
|---|---|
| downloaded/uploaded | 包文件存在、大小与校验摘要 |
| verified | 平台校验结果或签名验证结果 |
| staged | 升级/补丁管理器 staging 状态 |
| applied | 安装任务与审计记录成功 |
| effective | 当前产品/组件运行版本、进程或镜像版本与目标一致 |
| rolled_back | 回滚任务和当前有效版本共同证明 |

第一性原理：**文件存在只能证明文件存在，不能证明代码已经生效**。KBD 若把“目录里有补丁包”作为最终判断，应在审核时标记为证据层级不足。

---

## 9. 容器运行时与日志定位

### 9.1 运行时基线

观察节点使用：

```text
containerd + nerdctl + ctr + kubelet + runc
```

containerd 同时包含 `buildkit`、`default`、`k8s.io` namespace。kubelet 的关键路径为：

```text
root-dir: /container-data/kubelet
pod logs: /sf/log/pods
```

### 9.2 混合容器形态

设备上同时存在：

- Kubernetes sandbox/pause 容器；
- Kubernetes 实际业务容器；
- 非 sandbox 的存储控制面/数据面容器；
- 按存储角色和实例动态生成名称的容器。

平台组件涉及节点管理、虚拟化控制、网络、设备管理、日志、事件与存储等域。镜像标签包含产品版本或组件版本，但组件版本不一定与 HCI 产品版本完全相同。

### 9.3 Agent 定位原则

- 不依赖完整动态容器名；
- 使用稳定组件身份、namespace、label、image metadata 和 pod/container ID 解析；
- 容器日志优先从 `/sf/log/pods` 或受控 aCLI 能力获取；
- 不读取完整容器环境变量、Secret 挂载或凭据；
- Evidence 记录解析后的组件身份，而不是只记录易漂移的运行实例名；
- `container` 参数必须从目标环境发现结果中选择，不能由 LLM 自由生成。

建议变量池补充：

```text
{{COMPONENT}}
{{CONTAINER_NAMESPACE}}
{{POD_UID}}
{{CONTAINER_ID}}
{{IMAGE_VERSION}}
```

这些变量应由确定性 resolver 获取，并带采集时间和节点范围。

---

## 10. aCLI：QKV/QFK 的核心设备契约

### 10.1 命名空间与插件

观察节点 aCLI 提供的顶层命名空间包括：

```text
acli / alert / task / log / service / system / vm
network / storage / platform / plugin / hardware
```

可见插件包括系统离线诊断、日志采集、网络诊断和性能工具。插件有独立版本，且执行 `--help` 也可能启动插件程序或打印环境扫描信息，因此插件能力必须单独探测和分级，不能假设帮助命令总是零副作用。

### 10.2 设备自描述 manifest

设备端命令 manifest 位于 aCLI 配置目录中，本次观察到 326 条命令定义。manifest 可描述：

```text
input / output / output_format
execute.container
execute.execute_script 或 execute_cmd
execute_description
is_change_cmd
is_raw_cmd
is_support_global_container_arg
```

这是比网页抓取更接近目标设备真相的能力源。但 `is_change_cmd` 也不能机械当作最终风险：部分命令可读可写，部分名称含 `status` 的命令仍被标成 change。最终风险应由“manifest + 具体参数 + 平台覆盖策略”共同决定。

### 10.3 静态目录漂移

| 来源 | 观察时间/版本 | 命令数 | 结论 |
|---|---|---:|---|
| 在线 aCLI 文档 | Last-Modified 2026-05-20，标注 preview | 未作为运行时真相 | 文档明确警告不要随意使用变更命令 |
| HTP 静态 Catalog | source 2026-05-20，generated 2026-06-11 | 336 | 已落后于目标设备构建 |
| 目标设备 manifest | aCLI build 2026-07-13 | 326 | 与仓库静态目录存在数量漂移 |

命令数量不同不直接说明哪边“更完整”；它证明静态目录不能作为唯一运行时真相。HTP 应保留代码支持 Catalog，同时增加目标设备 Probe，并对差异给出原因。

### 10.4 `acli log get` 真实参数契约

设备帮助、在线文档和实现基本一致：

```text
-k / --keyword     关键字
-i / --request-id  请求 ID
-t / --time        绝对日期/时间前缀
-f / --file        日志 basename
-c / --context     上下文行
-E / --extend      扩展正则
-p / --path        受限搜索路径
-g / --gzip        压缩日志搜索
```

#### `file`

`-f` 只能接 basename，例如：

```text
-f sfvt_vtpdaemon.log
```

完整路径必须拆成 `-p` 与 `-f`。当前 Signal v2 的 basename 正则方向正确，CI 必须保留“`file` 含 `/` 时拒绝”的反例。

#### `path`

该设备实现明确允许：

```text
/sf/log
/sf/log/...
/sf/data/local
/sf/data/local/...
```

未确认允许整个 `/sf/data`，也未确认 `/sf/logs`、`/sf/datanew`。当前 HTP 白名单比实机更宽。

仅使用字符串前缀校验不安全。例如 `/sf/log/../cfg` 仍以 `/sf/log/` 开头。正确校验顺序是：

1. 必须是绝对路径；
2. 拆分路径段并拒绝 `.`、`..`、空段、NUL 与控制字符；
3. 规范化后验证仍位于允许根目录；
4. 如支持通配符，只允许出现在最后一个 path segment；
5. 在设备侧再次 fail closed，不能只信任生产端 Schema。

#### `time`

设备端 `-t` 不是通用相对时间窗。其接受值必须以完整日期开头：

```text
YYYY-MM-DD
YYYY-MM-DD HH
YYYY-MM-DD HH:MM:SS
```

当前 HTP 的 `time_window: "-1h"` 和 UI 示例 `now/-1h` 与真实 aCLI 不兼容。正确做法是把相对事故时间窗先在 Agent 侧解析为绝对时间，再生成设备参数；若需要真正的 `[start,end]` 范围，应使用支持范围的插件/能力或在采集后确定性过滤，不能假装 `-t` 原生支持。

#### 搜索行为

设备实现会组合 `grep`、`find`、`zgrep` 等方式：

- 指定目录时可能递归搜索；
- `-g` 需与 path 组合；
- 指定历史日期时可能定位并解压日归档；
- `-t` 同时参与日目录选择和文本过滤；
- `-E` 启用扩展正则；
- `request_id` 是独立检索维度；
- 不提供真正的起止时间范围语义。

这些行为应进入 Handler 契约和测试，而不是只写在 Prompt 示例里。

### 10.5 alert/task 的结构化输出价值

`task get` 可按关键字、错误码、虚拟机、时间、主机、状态、UPID 等过滤，输出包含任务描述、节点、虚拟机、对象、状态、开始/结束时间、错误码追踪、request_id 与 UPID。

`alert get` 可按关键字、事件、对象名/类型、时间、级别等过滤，输出包含告警类型、对象、节点、虚拟机、描述、时间、状态与紧急程度。

因此 `qkv_task` 与 `qkv_alert` 应优先使用结构化 JSON 字段判定，而不是把全部输出降为关键字 grep。`request_id`、对象 ID、host ID 和 VM ID 是连接 task、alert 与 log Evidence 的关键关联键。

### 10.6 `qkv_dialog` 的实机结论

观察节点不存在 `dialog` 命名空间。当前 HTP 一处实现尝试使用 `acli log get ... -l`，但 `log get` 又不支持 `-l`。因此在该目标环境：

```text
qkv_dialog.runtime_status = unsupported
reason_code = namespace_absent
```

“KBD 截图被分类为弹框”只说明案例证据中存在弹框，不等于客户现场存在 `acli dialog get`。若要支持弹框信号，必须先确认真实运行时数据源，例如浏览器遥测、操作日志 API 或 task/alert 的确定性映射。截图 OCR/视觉描述只能作为知识生产 Evidence，不能冒充现场可查询信号。

---

## 11. 自观测与探针污染

### 11.1 已确认现象

执行 aCLI 日志查询会把命令写入 aCLI/audit 日志；如果随后递归搜索相同关键词，可能命中本次探针自身，从而形成“我搜索了 X，所以日志里存在 X”的假阳性。

### 11.2 必须建立的证据边界

每次 acquisition 至少记录：

```text
exec_id / probe_id
acquisition_started_at
acquisition_finished_at
target_node
command_fingerprint
self_observation_filtered
filter_rule
```

默认处理顺序：

1. 在执行前固定采集时间上界；
2. 为本次采集生成唯一 `exec_id`；
3. 排除本次执行后写入的 aCLI/audit 记录，或对这些日志设置时间上界；
4. Evidence 明确记录是否做了自观测过滤；
5. 回归测试包含“探针关键词不会自证故障成立”的反例。

这不是日志去重问题，而是因果边界问题：采集动作产生的数据不能反过来证明采集前已经存在的故障。

---

## 12. HTP 当前实现与实机契约差距

| 主题 | 实机/设备契约 | HTP 当前实现 | 风险 | 优先级 |
|---|---|---|---|---|
| qfk_log file | 只能是 basename | Schema 正则正确，但 CI 合法 fixture 曾写完整路径 | CI 失败、示例误导 | P0，本 PR 修复 fixture |
| qfk_log path | `/sf/log`、`/sf/data/local` 受限范围 | 允许 `/sf/logs`、整个 `/sf/data`、`/sf/datanew`，且仅 `startswith` | 设备拒绝与路径逃逸 | P0，待确认后改运行语义 |
| log time | 绝对日期前缀 | `time_window=-1h` 直接映射 `-t` | 命令确定失败 | P0 |
| 历史日志 | 可能解压归档并占空间 | QFK 一律声明 risk=1/auto | 风险记录失真 | P0/P1 |
| qkv_dialog | 目标设备无 namespace | Schema、Prompt、UI、Descriptor 均宣称存在 | 信号永远不可执行 | P0 |
| QFK 风险 | 171 个 manifest 命令标读，155 个标 change；还需参数级判断 | 上层统一 risk=1/auto，底层 regex 仅覆盖部分命令 | 未知变更命令可能默认放行 | P0/P1 |
| Capability | Handler ∩ Device ∩ Policy | 11 个 Schema 均 `available`、`read_only_intent=true` | 把声明当能力 | P1 |
| aCLI Catalog | 目标设备 326 条 | 静态 Catalog 336 条 | 版本漂移 | P1 |
| tool_definition | 可执行需 Handler、输出与安全策略 | 主要是说明、参数 JSON 与示例 CRUD | 专家新增记录后误以为已可执行 | P1/P2 |
| blackbox | 时间序列快照 | 无专门采集/判断契约 | 只能做脆弱 grep | P2 |
| 探针污染 | 查询写审计日志 | 未显式记录自观测过滤 | 假阳性 | P1 |

### 12.1 当前 `tool_definition` 能力边界

现有工具管理支持名称、分类、描述、用法模板、参数 Schema、示例、风险级别、启停与版本等配置，适合管理“工具说明与参数入口”，但不能单独证明：

- Agent Handler 已实现；
- 参数会按该模板构造；
- 输出可被确定性解析；
- 目标节点提供对应命令；
- 命令在当前版本安全；
- 示例符合参数 Schema；
- 保存后执行链路真实生效。

因此现阶段出现 `capability_gap` 时，Admin UI 不能靠“新建一条 tool_definition”实现零代码可执行能力。快速闭环需要“声明、实现、探测、策略、验证”五层状态可见，但操作流程仍可保持三个动作：探测、对比/导入、只读验证。

---

## 13. 目标 Capability Descriptor

### 13.1 最小描述模型

```json
{
  "capability_id": "qfk_log",
  "contract_version": "2",
  "handler": "log_keyword_v2",
  "validator": "qfk_log_args_v2",
  "product_compatibility": ["6.11.1_R1"],
  "acli_compatibility": ["1.0.0"],
  "device_probe": {
    "node_id": "<redacted>",
    "probed_at": "2026-07-29T00:00:00+08:00",
    "manifest_hash": "sha256:...",
    "runtime_status": "available",
    "reason_code": null
  },
  "safety": {
    "read_only_intent": true,
    "side_effects": ["audit_log_write", "lazy_archive_decompress"],
    "unknown_command_policy": "confirm",
    "free_shell": false
  },
  "verification": {
    "status": "verified_on_target",
    "last_replay_id": "..."
  }
}
```

### 13.2 状态不得混用

| 状态 | 说明 |
|---|---|
| `contract_status` | 代码是否声明参数和输出契约 |
| `handler_status` | Agent 是否实现 Handler/Validator |
| `runtime_status` | 目标环境是否提供所需 namespace/command |
| `policy_status` | 当前安全策略是否允许执行 |
| `verification_status` | 是否在兼容环境通过回放/探针验证 |

只有以上均满足的能力才能进入 `executable`。`runtime_status=unknown` 不能在 UI 中显示为“可用”。

---

## 14. 对知识生产、专家复核与 Agent 消费的应用

### 14.1 知识生产

LLM 抽取关键信号前，应接收目标版本可用的 Capability Descriptor，而不是自由编造工具名和参数。每条 Proposal 必须回答：

1. 来源是正文、截图可见文字、截图语义还是隐含推断；
2. 客户现场能否获取该信号；
3. 用哪个已验证能力获取；
4. 返回什么结构；
5. 用什么确定性 predicate 判定；
6. 命中证明什么，未命中又排除什么；
7. 是否有版本和副作用限制。

如果没有已验证能力，生产器应输出明确的 `capability_gap`，而不是生成看似完整但无法执行的 QFK。

### 14.2 专家复核

审核页应以一屏完成以下闭环：

```text
原始案例证据
  → LLM Proposal
  → 结构化信号与实际命令预览
  → 目标环境支持/风险/版本状态
  → 专家编辑
  → 契约校验
  → 只读试跑或 Evidence Replay
  → 保存并立即以专家版本生效
```

专家需要直接看到：

- 哪个字段错误以及如何修改；
- `file/path/time` 最终会变成什么 aCLI 参数；
- 目标设备是否支持、为何不支持；
- 是否会解压历史归档或写审计日志；
- 输入变量由谁产生；
- 运行输出与 predicate 的实际结果；
- LLM 原值与专家值的字段级差异。

### 14.3 Agent 消费

Agent 运行时不直接信任 KBD 中的命令字符串。执行路径应为：

```text
KBD declarative signal
  → Schema validation
  → Capability resolution
  → target version/device probe match
  → variable resolution
  → deterministic command builder
  → safety classification
  → execution
  → typed parser
  → predicate evaluator
  → Evidence + Replay
```

任何一步未知都应返回结构化 reason code，例如：

```text
namespace_absent
command_absent
version_incompatible
handler_missing
unsafe_path
relative_time_unsupported
archive_decompression_required
output_parser_missing
capability_probe_stale
```

---

## 15. Prompt、Schema、数据库与 Admin UI 修改建议

> 本节是待用户确认的目标变更，不表示当前已经实现。

### 15.1 Prompt

- 从可选工具中移除或降级目标环境不支持的 `qkv_dialog`；
- 明确截图分类不等于运行时信号能力；
- qfk_log 强制 `file=basename`、`path=允许目录`；
- 禁止生成 `time_window=-1h` 并说明需解析成绝对日期；
- 只向模型提供当前目标版本可执行能力交集；
- root cause/solution 不作为信号来源时，应在调用输入层真正移除，而不只在文字中要求模型忽略；
- 不可执行时输出 `capability_gap`，禁止用自由 Shell 补洞。

### 15.2 Signal Schema

P0：

- 路径规范化并收紧允许根；
- 时间字段从模糊 `time_window` 迁移为明确的绝对时间语义；
- `qkv_dialog` 标记 unsupported 或从可执行集合移出；
- 安全 profile 不再把所有 QFK 都写成只读。

P1/P2：

- 增加 `side_effects`、兼容版本、Handler 与验证状态；
- 为 blackbox 增加结构化采集与 threshold/trend predicate；
- Evidence 增加探针污染过滤状态和采集时间边界。

### 15.3 数据库

优先在现有 `tool_definition` 或其 JSONB 扩展中增加：

```text
output_schema
runtime_contract
compatibility
safety_profile
source_metadata
verification_state
```

只有在需要保留“多节点 × 多版本 × 多次探测”的历史快照时，才有充分理由新增独立 probe snapshot 表；否则先保持轻量。无论采用哪种方式，数据库记录都只是能力描述，不能取代代码 Handler。

### 15.4 Admin UI

工具管理页最小增加三个动作：

1. **探测目标环境**：获取产品/aCLI 版本和设备命令 manifest；
2. **导入并对比契约**：展示代码、数据库、设备和安全策略的差异；
3. **运行只读验证**：使用受控参数执行，并展示解析结果、predicate、Evidence 和副作用。

KBD 审核页复用这些能力，不新建复杂工作台。保存专家修改后应立即重新校验和验证，发布内容以专家版本为准，同时保留 LLM Proposal 作为后续评估样本。

### 15.5 Agent 风险策略

- 未知命令不再默认 risk=1；
- 设备 manifest 的 `is_change_cmd` 作为输入，不作为唯一结论；
- 风险由命令、参数、目标、版本、副作用与政策共同计算；
- 历史日志解压须显示磁盘前置条件；
- 路径和参数必须经 Handler 构造，不执行 KBD 自由命令；
- 结构化审计中的风险级别必须与最终执行器一致，不能上层先记 risk=1、底层再悄悄升级。

---

## 16. 轻治理、自动化优先的实施顺序

### P0 · 修正确定错误

- 修复 Signal Schema CI 的合法 fixture，并增加完整路径反例；
- 收紧路径规范化与目标版本允许根；
- 停止把 `-1h` 直接传给 `acli log get -t`；
- 将目标环境的 `qkv_dialog` 标为 unsupported；
- 未知/变更命令不再静默自动执行；
- 修正所有 Capability `read_only_intent=true` 的错误声明。

### P1 · 设备能力探测

从版本命令和设备 manifest 生成脱敏、版本化快照：

```json
{
  "product_version": "6.11.1_R1",
  "acli_version": "1.0.0",
  "manifest_hash": "sha256:...",
  "node_id": "<redacted>",
  "probed_at": "...",
  "commands": [],
  "plugins": []
}
```

运行时只暴露 `Code Handler ∩ Device Capability ∩ Safety Policy`。

### P2 · 专家单屏闭环

在现有工具管理与 KBD 审核页增加探测、差异对比和只读验证，不引入双审或复杂状态机。字段错误就地提示，修改后立即回放。

### P3 · 黑盒与结构化 predicate

补充 blackbox 采集、目录计数、阈值、范围、趋势和连续次数判断，使“文件数是否超阈值”等知识不再被压缩成关键字搜索。

### P4 · 用专家差异优化 LLM

保留 LLM Proposal 与专家修订的 stable-ID 字段级 diff，按以下维度分桶：

```text
tool / field / error_code / model / prompt_version
product_version / acli_version / capability_version
```

优先把高频差异转为确定性 validator 与 Prompt 约束，再做模型 champion/challenger 评估。专家修改只有经过契约验证与执行回放后才可升级为 Expert Gold；当前 126 条中仍是 0 条 Expert Gold、0 条正式业务专家批准，不能把 Proposal 数量冒充 Gold 数量。

---

## 17. 跨版本验证计划

至少覆盖：

| 维度 | 验证项 |
|---|---|
| HCI 版本 | 6.11.1_R1 之外的存量主版本 |
| aCLI 版本 | manifest 数量、参数、输出与副作用 |
| 日志归档 | `.tar.gz` / `.tar.zst`、保留天数、跨月行为 |
| 集群角色 | 管理、计算、存储节点目录与命令差异 |
| 容器形态 | K8s 与非 K8s 组件解析 |
| 存储类型 | 本地、共享、NFS、device-mapper 路径与容量判断 |
| 语言/时间 | 多日志时间格式、时区、跨日/跨月事故 |
| 安全 | 路径逃逸、敏感配置、未知命令、探针污染 |

每次探测都应保留脱敏快照及 hash，差异自动生成报告；不保存业务日志内容和敏感配置正文。

---

## 18. 用户确认项

在进入运行语义改造前，需要确认以下决策：

1. 是否按 P0 立即将 `qkv_dialog` 从“可执行”降为“目标环境不支持”，保留截图 `dialog` 分类作为生产 Evidence；
2. 是否将 qfk_log 时间契约改为绝对日期/时间，并由 Agent 解析相对事故窗口；
3. 是否将 qfk_log 路径收紧到目标设备确认的 `/sf/log` 与 `/sf/data/local`，其他路径通过专用能力读取；
4. 历史归档未解压时，是默认要求确认，还是只在磁盘前置条件满足时自动执行；
5. Capability Probe 快照第一阶段存现有 JSONB，还是因多节点历史审计需求直接建独立表；
6. blackbox 第一阶段独立建 `qfk_blackbox`，还是先以逻辑适配层复用现有执行器；
7. 未知 aCLI 命令默认策略采用 `require_confirm` 还是严格 `deny`。

推荐答案分别为：**降级、改绝对时间、收紧、条件确认、先 JSONB、独立逻辑能力、默认确认且生产环境可配置 deny**。这些选择保持架构简单，同时不牺牲可执行真实性与安全边界。

---

## 19. 脱敏证据命令模板

以下仅用于授权环境的限量核验，不含访问凭据：

```bash
uname -a
/sf/bin/acli --version
findmnt -T /sf/log
findmnt -T /cfs
findmnt -T /sf/cfg
findmnt -T /sf/data/local
find /sf/log/blackbox -maxdepth 1 -mindepth 1 -type d | sort | tail
find /sf/cfg/acli/etc/cmds -type f -name '*.json' | wc -l
```

禁止把以下内容加入自动证据包：完整环境变量、Secret、私钥、证书正文、数据库凭据、任意递归 `/cfs`、任意递归 `/sf/cfg`、完整业务日志或客户数据。

---

## 20. 一句话结论

HCI 知识要成为 Agent 可消费的 KBD，必须从“人能看懂的目录和命令描述”升级为“受版本约束、设备可探测、参数可校验、输出可解析、风险可计算、证据可回放的 Capability 契约”；专家审核是当前质量兜底，而设备自描述、确定性校验和执行回放才是长期自动化的基础。
