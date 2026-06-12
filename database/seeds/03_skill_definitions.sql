-- ===========================================================================
-- database/seeds/03_skill_definitions.sql — 技能定义初始种子数据
-- ===========================================================================
-- 用途：初始化 skill_definition 表（Agent Skills Open Standard 合规版本）
--       预置 HCI 平台领域专业 Skill
-- 执行时机：
--   1. 首次建库后（迁移 20260605000000 执行完毕）
--   2. ON CONFLICT (skill_name) DO NOTHING — 已存在同名技能时跳过，保护用户在 admin-ui 中的修改
-- 执行方法：
--   psql "$DATABASE_URL" -f database/seeds/03_skill_definitions.sql
-- ===========================================================================

INSERT INTO skill_definition (
    skill_name,
    description,
    instructions_md,
    compatibility,
    metadata_json,
    display_name,
    is_active
) VALUES (
    -- =========================================================
    -- Skill: hci-disk-vendor-lifetime
    -- 对应原始材料：skills/硬盘厂商识别与寿命判断.md
    -- =========================================================
    'hci-disk-vendor-lifetime',

    -- description（发现阶段使用，描述"做什么"和"何时触发"）
    '识别 HCI 节点物理磁盘的厂商（铠侠/东芝、英特尔、三星、美光、创见、海康、大唐存储、华为、江波龙、Foresee、建兴），并依据各厂商专属 SMART 指标（第 173、233、177、202、167、231 项等）判断磁盘是否达到返修阈值。当用户报告磁盘 IO 异常、存储池降级、坏道告警，或需要判断磁盘是否需要更换时触发。',

    -- instructions_md（SKILL.md 正文，激活阶段加载）
    $SKILL$
## 硬盘厂商识别与寿命判定

### 前置条件

已获取到硬盘的 SMART 原始回显文本（通常通过 `smartctl -a /dev/sdX` 或 `smartctl -a -d megaraid,N /dev/sdX` 命令获得），存储在 `{smart_info}` 变量中。

---

### Step 1：提取硬盘型号信息

从 `{smart_info}` 中提取以下字段，**按优先级依次检查并拼接**作为型号字符串用于匹配：

1. `Device Model`
2. `Model Number`
3. `Product`

将提取到的型号字符串统一转为**小写**后进行匹配。

---

### Step 2：厂商识别（型号前缀匹配）

根据型号字符串（小写），按以下规则匹配厂商：

| 厂商 | 匹配前缀/关键词（小写，前缀匹配或包含匹配） |
|------|------------------------------------------|
| **Kioxia / Toshiba（铠侠 / 东芝）** | `kcm*`、`kpm*`、`krm*`、`kcd*`、`kxd*`、`kfl*`、`klc*`、`khk*`、`kioxia`（含）、`toshiba`（含） |
| **Intel（英特尔）** | `ssdsc*`、`ssdpe*`、`ssdpf*`、`mdtpe*`、`intel`（含） |
| **Samsung（三星）** | `mz*`、`samsung`（含） |
| **Micron（美光）** | `mtfd*`、`micron`（含） |
| **Transcend（创见）** | `ts*`、`transcend`（含） |
| **Hikvision（海康）** | `hs-ssd*`、`hikvision`（含） |
| **大唐存储** | `sts*`、`dts*`、`datssd`（含） |
| **Huawei（华为）** | `hwe`（前缀）、`hssd*`、`huawei`（含） |
| **Longsys（江波龙）** | `rsye*`、`longsys`（含） |
| **Foresee** | `fi*`、`foresee`（含） |
| **Liteon / SSSTC（建兴）** | `liteon*`、`ssstc`（含） |

**Gotchas（厂商识别陷阱）：**
- `ts*` 前缀会匹配 Transcend，但要注意 Toshiba 的型号以 `toshiba` 关键词为准（含匹配），而非 `ts` 前缀
- `hwe` 是 Huawei 的前缀匹配，但 `hwss*` 类型请检查是否属于海康变体
- 若型号无法匹配任何规则，应告知用户"未能识别厂商，请提供完整 SMART 信息或联系技术支持"

---

### Step 3：寿命判定（按厂商读取对应 SMART 指标）

识别厂商后，从 `{smart_info}` 中读取对应 SMART 属性行，判断是否需要返修：

#### Kioxia / Toshiba（铠侠 / 东芝）
- **指标**：SMART 第 **173** 项（Media Wearout Indicator）
- **读取字段**：`VALUE`（注意：是 VALUE 列，不是 RAW_VALUE）
- **返修阈值**：`VALUE < 100`
- **示例**：`173 Media_Wearout_Ind  ... VALUE=95 ...` → VALUE=95 < 100 → **返修**

#### Intel（英特尔）
- **指标**：SMART 第 **233** 项（Media Wearout Indicator）
- **读取字段**：`VALUE`
- **返修阈值**：`VALUE ≤ 10`

#### Samsung（三星）
- **指标**：SMART 第 **177** 项（Wear Leveling Count）
- **读取字段**：`VALUE`
- **返修阈值**：`VALUE ≤ 10`

#### Micron（美光）
- **指标**：SMART 第 **202** 项（Percentage Lifetime Used）
- **读取字段**：`VALUE`
- **返修阈值**：`VALUE ≤ 10`

#### Transcend（创见）
- **指标**：SMART 第 **167** 项（SSD Protect Mode）
- **读取字段**：`RAW_VALUE`（注意：是 RAW_VALUE，不是 VALUE）
- **返修阈值**：`RAW_VALUE ≥ 2700`

#### Hikvision（海康）
- **指标**：SMART 第 **233** 项
- **读取字段**：`RAW_VALUE`（注意：是 RAW_VALUE，不是 VALUE）
- **返修阈值**：`RAW_VALUE ≥ 90`

#### 大唐存储
- **指标**：SMART 第 **233** 项
- **读取字段**：`VALUE`
- **返修阈值**：`VALUE ≤ 10`

#### Huawei（华为）
- **指标**：SMART 第 **231** 项（SSD Life Left）
- **读取字段**：`VALUE`
- **返修阈值**：`VALUE ≤ 10`

#### Longsys（江波龙）
- **指标**：SMART 第 **202** 项
- **读取字段**：`VALUE`
- **返修阈值**：`VALUE ≤ 10`

#### Foresee
- **指标**：SMART 第 **167** 项
- **读取字段**：`RAW_VALUE`
- **返修阈值**：`RAW_VALUE ≥ 2700`

#### Liteon / SSSTC（建兴）
- **优先指标**：SMART 第 **202** 项，`VALUE ≤ 10` → 返修
- **备选指标**（若无 202 项）：SMART 第 **177** 项，`VALUE ≤ 10` → 返修

---

### Step 4：输出判定结果

根据 Step 3 的判定，给出明确结论：

**结果为"正常"时：**
```
硬盘厂商：{厂商名}
寿命指标：SMART 第 {N} 项 {字段名} = {实际值}（阈值：{阈值描述}）
判定结论：✅ 硬盘寿命正常，无需更换
```

**结果为"返修"时：**
```
硬盘厂商：{厂商名}
寿命指标：SMART 第 {N} 项 {字段名} = {实际值}（阈值：{阈值描述}）
判定结论：⚠️ 硬盘寿命已达返修阈值，建议更换硬盘
建议操作：联系原厂或备件库申请更换，更换前确认存储池状态
```

---

### Gotchas（关键陷阱清单）

1. **VALUE vs RAW_VALUE 混淆**：Transcend、Hikvision、Foresee 使用 `RAW_VALUE`；其余厂商使用 `VALUE`。两者单位和含义完全不同，务必区分。

2. **Liteon/SSSTC 双指标**：优先读 202 项，仅当 202 项不存在时才回退到 177 项，不能同时使用两者。

3. **SMART 第 1 项（Raw_Read_Error_Rate）**：在希捷（Seagate）磁盘上 RAW_VALUE 通常非常高（正常现象），**不适用本 Skill**，勿误判为故障。本 Skill 仅处理上述 11 个厂商。

4. **NVMe 磁盘**：NVMe 磁盘无传统 SMART 属性编号，通常通过 `nvme smart-log` 命令读取，本 Skill 中的属性编号（173/233/177 等）**不适用于 NVMe**，遇到 NVMe 磁盘请告知用户使用专项诊断流程。

5. **SMART 项目缺失**：若在 SMART 输出中找不到对应编号的项目（如没有第 202 项），需告知用户该厂商版本可能不支持标准寿命项，建议联系厂商确认。

6. **`ts*` 前缀碰撞**：Transcend 使用 `ts*` 前缀，但应警惕部分厂商型号也以 `ts` 开头（如 Toshiba 的部分型号）。当 `ts*` 与 `toshiba` 关键词同时存在时，优先以 `toshiba` 关键词判定为 Kioxia/Toshiba。
$SKILL$,

    -- compatibility
    '适用于 HCI v2.x 环境，需要能执行 smartctl 命令（smartmontools），或已通过 SSH 采集工具获取 SMART 原始文本',

    -- metadata_json
    '{"author": "hci-team", "category": "storage", "tags": ["disk", "smart", "ssd", "lifetime", "vendor"]}'::jsonb,

    -- display_name（中文展示名，平台扩展字段）
    '硬盘厂商识别与寿命判定',

    -- is_active
    true
)
-- 幂等保护：已存在同名技能时跳过，避免覆盖用户在 admin-ui 中的自定义内容
ON CONFLICT (skill_name) DO NOTHING;


INSERT INTO skill_definition (
    skill_name,
    description,
    instructions_md,
    compatibility,
    metadata_json,
    allowed_tools,
    display_name,
    is_active
) VALUES (
    -- =========================================================
    -- Skill: hci-alert-parsing
    -- 对应原始材料：skills/告警解析.md
    -- =========================================================
    'hci-alert-parsing',

    -- description（发现阶段使用，描述"做什么"和"何时触发"）
    '解析 HCI 平台告警事件（acli alert get 导出的 JSON 告警记录）中的关键字段，识别告警对象、告警主机 IP、告警类型和告警时间，为后续排障提供结构化上下文。当用户提供告警 JSON 数据、描述告警事件、询问某个告警的含义，或需要定位告警对应的节点 IP 时触发。',

    -- instructions_md（Skill 正文，激活阶段加载）
    $SKILL$
# 告警解析

## 触发场景

当用户提供一条 HCI 告警记录（JSON 格式的 `{alert}`），需要将其解析成易于理解的结构化结果，并确认告警对应的节点 IP，以便后续排障步骤定位正确主机。

---

## 前置条件

- 已提供告警记录 `{alert}`，格式为 JSON（来自 `acli alert get` 或导出的告警数据）
- 若已知集群节点列表，可直接提供 `{nodes}` 跳过命令采集步骤

---

## Step 1：提取告警关键字段

解析 `{alert}` JSON，提取以下字段（字段可能直接在根层或在 `data` 子对象中）：

| 字段 | 含义 |
|------|------|
| `alert_type` | 告警类型（如 vs_disk_warn / iface_down / host_bond） |
| `description` | 描述（常含括号变量，如 `（192.168.1.10）`） |
| `end` | 时间（可能是时间戳或已格式化时间） |
| `host` | 主机名或 IP（告警来源，可能不准确） |
| `hostid` | 主机 ID（用于在节点列表中匹配） |
| `object_type` | 对象类型（如 存储 / 主机 / 集群） |
| `target` | 告警对象（节点名、虚机名等） |
| `type` | 事件（如 磁盘状态异常 / 网口掉线告警） |
| `vm` | 虚拟机 ID（如无则为空） |

以 Key-Value 格式输出原始提取结果。

---

## Step 2：处理与标准化

**2.1 解析 `description`**

从 `description` 字段提取括号 `（）` 或 `()` 内的变量值，并根据上下文说明变量含义。
示例：`内存使用率超过阈值 (192.168.1.10)` → 提取出 IP `192.168.1.10`，含义为触发告警的主机 IP。

**2.2 转换 `end` 为可读时间**

若 `end` 是 Unix 时间戳（纯数字，10位或13位），转换为 `YYYY-MM-DD HH:MM:SS`（时区以集群本地时区为准，默认 UTC+8）。若为 13 位毫秒时间戳，需先除以 1000 再转换。

**2.3 确认 `node_ip`（核心步骤）**

执行命令获取集群节点列表：
```bash
acli --formatter json platform node list
```

将返回结果存为 `{nodes}`，按以下优先级依次尝试匹配，**一旦匹配成功立即停止**：

| 优先级 | 匹配字段 | 目标字段 | 说明 |
|--------|---------|---------|------|
| 1 | `{alert}.target` ↔ `{nodes}[].name` | `{nodes}[].ip` | 告警对象名与节点名匹配 |
| 2 | `{alert}.hostid` ↔ `{nodes}[].id` | `{nodes}[].ip` | 主机 ID 与节点 ID 匹配 |
| 3 | `description` 中提取的 IP ↔ `{nodes}[].ip` | 直接使用该 IP | 描述变量与节点 IP 直接匹配 |

若三级均未匹配成功：告知用户"无法从节点列表中确认对应节点 IP，请人工核实 `host` 或 `target` 字段"。

---

## Gotchas（关键陷阱清单）

1. **`{alert}` 中可能有多个 IP**：`host`、`description` 变量、`target` 均可能含有 IP 地址，只有通过 `{nodes}` 列表反查才能确认哪个是真正负责的节点 IP。

2. **`host` 字段不可直接使用**：该字段在部分版本中存储的是 HCI 平台主机名（非节点 IP），直接使用会导致排障执行命令连接到错误主机。

3. **每条告警都必须明确 `node_ip`**：错误的 `node_ip` 会导致后续排障命令在错误节点上执行，是排障失败最常见的原因之一。

4. **`end` 时间戳可能是毫秒级**：若时间戳为 13 位，需除以 1000 后再转换。

5. **`description` 括号格式不统一**：部分记录使用中文括号 `（）`，部分使用英文括号 `()`，匹配时需兼容两种格式。

---

## 输出格式

```
告警类型（`alert_type`）：vs_disk_warn
时间（`end`）：2026-06-12 10:30:00
对象类型（`object_type`）：存储
告警对象（`target`）：SVR_aCloud_670
事件（`type`）：磁盘状态异常
描述（`description`）：主机（SVR_aCloud_670）SSD寿命告警（1号盘），告警盘槽位（1），剩余寿命3%！建议：请购买新的SSD，并联系深信服科技更换SSD！
告警主机（`node_id`）：172.28.24.4
告警虚拟机（`vm`）：（无）
```

若 `node_ip` 未能从节点列表匹配，在"告警主机"行注明"待确认（匹配失败，原始 host 字段：xxx）"。
$SKILL$,

    -- compatibility
    '适用于 HCI 5.x.x 及以上环境；需要能执行 acli 命令（获取节点列表），或已预先提供 {nodes} 变量',

    -- metadata_json
    '{"author": "hci-team", "category": "platform", "tags": ["alert", "platform", "node-ip", "event", "hci"]}'::jsonb,

    -- allowed_tools（声明本 Skill 可能调用的工具类型）
    'bash',

    -- display_name
    '告警解析',

    -- is_active
    true
)
ON CONFLICT (skill_name) DO NOTHING;


INSERT INTO skill_definition (
    skill_name,
    description,
    instructions_md,
    compatibility,
    metadata_json,
    allowed_tools,
    display_name,
    is_active
) VALUES (
    -- =========================================================
    -- Skill: hci-task-parsing
    -- 对应原始材料：skills/任务解析.md
    -- =========================================================
    'hci-task-parsing',

    -- description（发现阶段使用，描述"做什么"和"何时触发"）
    '解析 HCI 平台任务日志（acli task get 命令输出的 JSON 任务记录）中的关键字段，识别任务类型、操作目标、失败错误码、调用链 ID 以及执行节点 IP，为后续排障提供结构化上下文。当用户提供任务 JSON 数据、询问任务执行情况、描述操作失败场景（如虚机开关机失败、迁移失败、存储操作失败），或需要通过任务日志定位故障节点时触发。',

    -- instructions_md（Skill 正文，激活阶段加载）
    $SKILL$
# 任务解析

## 触发场景

当用户提供一条 HCI 任务记录（JSON 格式的 `{task}`），需要将其解析成易于理解的结构化结果，提取错误码和调用链 ID，并确认任务执行所在节点的 IP，以便后续排障步骤定位正确主机。

---

## 前置条件

- 已提供任务记录 `{task}`，格式为 JSON（来自 `acli task get` 或 `acli task get -s failed` 命令输出）
- 若已知集群节点列表，可直接提供 `{nodes}` 跳过命令采集步骤

---

## Step 1：提取任务关键字段

解析 `{task}` JSON，提取以下字段（字段可能直接在根层或在 `data` 子对象中）：

| 字段 | 含义 |
|------|------|
| `description` | 任务详细描述（常含括号变量，如 `（VM名）`, `（172.28.24.4）`） |
| `end` | 任务结束时间（可能是时间戳或已格式化时间） |
| `errcode_tracing` | 错误码追踪链（如 `0x0C000005`，多个时用逗号分隔） |
| `host` | 主机名或 IP（任务执行主机，可能不准确） |
| `hostid` | 主机 ID（用于在节点列表中匹配） |
| `request_id` | 调用链 ID（用于关联日志，格式如 `,a2c2056eaff140d09dd85e55999b69a1`） |
| `target` | 操作目标名称（VM 名、存储卷名等） |
| `type` | 任务类型/行为（如 登录 / 启动虚拟机 / 编辑网卡连接） |
| `vm` | 虚拟机 ID（如无则为空） |

以 Key-Value 格式输出原始提取结果。

---

## Step 2：处理与标准化

**2.1 解析 `description`**

从 `description` 字段提取括号 `（）` 或 `()` 内的变量值，并根据上下文说明变量含义。
示例：`虚拟机开机失败（MEM）主机（SVR_aCloud_668）` → VM名 `MEM`，目标节点 `SVR_aCloud_668`。

**2.2 转换 `end` 为可读时间**

若 `end` 是 Unix 时间戳（纯数字，10位或13位），转换为 `YYYY-MM-DD HH:MM:SS`（时区以集群本地时区为准，默认 UTC+8）。若为 13 位毫秒时间戳，需先除以 1000 再转换。

**2.3 清理 `request_id`**

`request_id` 字段值通常以 `,` 开头（如 `,a2c2056eaff140d09dd85e55999b69a1`），需去掉前导逗号，仅保留纯 ID 字符串，用于日志检索。

**2.4 确认 `node_ip`（核心步骤）**

执行命令获取集群节点列表：
```bash
acli --formatter json platform node list
```

将返回结果存为 `{nodes}`，按以下优先级依次尝试匹配，**一旦匹配成功立即停止**：

| 优先级 | 匹配字段 | 目标字段 | 说明 |
|--------|---------|---------|------|
| 1 | `{task}.target` ↔ `{nodes}[].name` | `{nodes}[].ip` | 操作目标名与节点名匹配 |
| 2 | `{task}.hostid` ↔ `{nodes}[].id` | `{nodes}[].ip` | 主机 ID 与节点 ID 匹配 |
| 3 | `description` 中提取的 IP ↔ `{nodes}[].ip` | 直接使用该 IP | 描述变量与节点 IP 直接匹配 |

若三级均未匹配成功：告知用户"无法从节点列表中确认对应节点 IP，请人工核实 `host` 或 `target` 字段"。

---

## Gotchas（关键陷阱清单）

1. **`{task}` 中可能有多个 IP**：`host`、`description` 变量、`target` 均可能含有 IP 地址，只有通过 `{nodes}` 列表反查才能确认哪个是真正执行任务的节点 IP。

2. **`host` 字段不可直接使用**：该字段在部分版本中存储的是 HCI 平台内部主机名（非节点 IP），直接使用会导致排障命令连接到错误主机。

3. **每条任务都必须明确 `node_ip`**：错误的 `node_ip` 会导致后续排障命令在错误节点上执行，是排障失败最常见的原因之一。

4. **`request_id` 前导逗号**：HCI 5.x 部分版本的任务 API 返回的 `request_id` 有前导 `,`，必须去除才能用于日志检索（如 `grep` 或 loki 查询）。

5. **`errcode_tracing` 为空时**：表示任务未产生可追踪的错误码（任务可能因超时或外部信号终止），此时重点关注 `description` 和 `request_id` 进行日志追踪。

6. **`end` 时间戳可能是毫秒级**：若时间戳为 13 位，需除以 1000 后再转换。

7. **`description` 括号格式不统一**：部分记录使用中文括号 `（）`，部分使用英文括号 `()`，匹配时需兼容两种格式。

---

## 输出格式

```
行为（`type`）：启动虚拟机
结束时间（`end`）：2026-06-12 10:30:00
主机（`node_ip）：172.28.24.4
虚拟机（`vm`）：451922388030
对象（`target`）：MEM
描述（`description`）：没有主机能够启动这台虚拟机，具体原因如下：\n主机（SVR_aCloud_668）：此主机计算内存不足，可请关闭主机上其它未使用的虚拟机或虚拟设备释放计算内存！\n主机（SVR_aCloud_669）：此主机计算内存不足，可用计算内存上其它未使用的虚拟机或虚拟设备释放计算内存！\n主机（SVR_aCloud_670）：此主机计算内存不足，可用计算内存 54.11 GB用的虚拟机或虚拟设备释放计算内存！\n
错误码（`errcode_tracing`）：0x0C000005
调用链（`request_id`）：a2c2056eaff140d09dd85e55999b69a1
```

若 `node_ip` 未能从节点列表匹配，在"主机"行注明"待确认（匹配失败，原始 host 字段：xxx）"。
若 `errcode_tracing` 为空，在"错误码"行注明"（无，建议通过调用链追踪日志）"。
$SKILL$,

    -- compatibility
    '适用于 HCI 5.x.x 及以上环境；需要能执行 acli 命令（获取节点列表），或已预先提供 {nodes} 变量',

    -- metadata_json
    '{"author": "hci-team", "category": "platform", "tags": ["task", "log", "node-ip", "error-code", "request-id", "hci", "platform"]}'::jsonb,

    -- allowed_tools（声明本 Skill 可能调用的工具类型）
    'bash',

    -- display_name
    '任务解析',

    -- is_active
    true
)
ON CONFLICT (skill_name) DO NOTHING;
