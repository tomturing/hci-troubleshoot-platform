# Diagnosis Service（诊断服务）

离线诊断领域控制面，当前提供：

- `DiagnosisSession`（诊断会话）和 `CollectionPlan`（采集计划）领域模型；
- `Collection Profile`（采集画像）不可变修订版本发布与生效；
- `Collector Registry`（采集器注册表）的草稿、审批、禁用和不可变修订版本；
- `Collector Artifact`（采集器制品）的 Structured Command Manifest（结构化命令清单）生成、Ed25519
  分离式签名、Manifest（清单）签名、有效期和可审计撤销；
- `Collector Verification Bundle`（采集器验证包），包含受信根、短时签名吊销清单和
  校验后直接执行结构化采集项的离线 Go 运行时；
- 版本化状态机；
- 租户级幂等创建；
- 对象级租户查询；
- 内部服务 Bearer Token（承载令牌）身份验证；
- OpenTelemetry、结构化日志和 Prometheus 指标。

服务默认监听 `8008`，避免与现有 eval-service（评测服务）`8007` 端口冲突。
Docker Compose（本地容器编排）直接启用；Helm（Kubernetes 包模板）中的
`diagnosisService.enabled` 默认关闭，配置正式身份和签名密钥后再按环境启用。
Helm 将签名私钥放在 `diagnosis-service-signing` 专用 Secret（密钥对象）中，
只挂载给诊断服务，不通过公共 `hci-secrets` 分发给其他工作负载。

## 安全约束

P0 服务间 API 复用项目统一 `INTERNAL_API_TOKEN`（内部接口令牌）。
只有令牌校验通过后，才读取 `X-Tenant-ID`（租户标识）和 `X-Actor-ID`
（操作者标识）；调用方不能通过角色头自提权，内部角色由服务固定签发。

创建诊断会话前会校验工单存在。服务支持内部 Token 和 OIDC（开放身份连接）
RS256/ES256/EdDSA 验签两种模式；正式面向客户开放时必须使用 OIDC，并配置
`CaseAuthorizer`（工单授权器）执行租户及对象级授权，浏览器不得持有内部 Token。

健康检查和指标接口不依赖身份验证器。

API Gateway（接口网关）仅对白名单诊断控制面路径提供安全代理。内部 Token 模式要求
可信调用方携带合法的 `X-Tenant-ID / X-Actor-ID`；OIDC 模式只透传已签发 Bearer
Token，并剥离浏览器伪造的身份头。代理请求体上限为 1 MiB，
Diagnostic Evidence Bundle（诊断证据包）通过一次性上传令牌直传诊断服务数据面。
默认返回 `/api/direct/diagnosis-uploads/...` 相对地址，由 Customer/Admin UI（客户/管理界面）
同源入口的 Nginx/Ingress（反向代理/入口）直接流式转发到 Diagnosis Service（诊断服务），
不经过 API Gateway（接口网关），也不需要浏览器跨域。只有使用独立上传域名或外部对象存储时，
才通过 `DIAGNOSIS_ALLOWED_ORIGINS` 显式放行可信 UI Origin，并只开放 `PUT / OPTIONS` 及
`Content-Type / X-Upload-Token / X-Part-SHA256 / traceparent / tracestate` 请求头；禁止配置 `*`。

## 当前范围

当前 Collection Profile（采集画像）和已审批只读 Collector（采集器）的数量由已发布
KBD/Tool 同步结果动态决定，不再以固定种子数量作为业务基线。系统已支持初始/补充采集计划、
Direct Command（直接命令）、固定只读 HCI API
（HCI 接口）和 Manual Attachment Guide（人工附件指引）三种执行器。制品只包含
采集计划中 `active`（当前激活）的已审批 Collector，按不可变 revision（修订版本）
解析；每项独立超时，stdout（标准输出）与 stderr（标准错误）合计受输出上限约束，
单项失败不终止后续项。

KBD 同步生成 Collector 时，命令/指引唯一来自 Tool Registry（工具注册表）已发布修订的
`usage_template`（使用模板），参数边界来自 `parameters_schema`（参数模式）；不再使用
Diagnosis Service（诊断服务）内部硬编码命令目录。`qfk_log`（后端日志信号）严格按 Tool 模板
生成参数化直执行 Collector，同步器不私自增加命令参数；
KBD 派生参数由 Collection Profile（采集画像）固化到 Collection Plan Item（采集计划项），
再进入签名制品。KBD 和 Tool 修订使用独立 Watermark（游标），Tool 变更可被增量同步自动发现。

存量数据库迁移：删除重复事实源 `tool_definition.offline_collection_spec`；
`collection_plan_item` 新增 `collector_revision/collector_version/collector_checksum`（采集器修订/版本/校验和）。
`03_qkv_qfk_tools.sql` 仅为首次部署注册缺失的平台 Tool，使用 `ON CONFLICT DO NOTHING`
（冲突时不覆盖）；不再存在独立的离线采集规范种子。

Diagnostic Evidence Bundle（诊断证据包）支持 AES-256-GCM + RSA-OAEP-SHA256
Envelope Encryption（信封加密）、分片直传、断点查询、安全扫描/解压、不可变证据、
三态 Signal（信号）、KBD 候选、Diagnosis Report（诊断报告）、一次补采、时间线、
双人 Legal Hold（法务保全）和异步删除。Go 离线运行时支持 `--cleanup-plaintext`，
加密成功后只删除清单声明的明文和受控执行文件，不递归删除输出目录。供应商 KMS
（密钥管理服务）和外部对象存储属于生产环境适配。

未配置 `COLLECTOR_SIGNING_PRIVATE_KEY_B64` 和 `COLLECTOR_SIGNING_KEY_ID` 时，
制品生成默认拒绝并返回 `ARTIFACT_SIGNER_UNAVAILABLE`，不会降级为无签名制品。
未配置 `DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64` 和 `DIAGNOSIS_ENCRYPTION_KEY_ID` 时，
制品生成会提前返回 `ARTIFACT_ENCRYPTION_UNAVAILABLE`，不会交付只能采集、不能加密打包的
无效工具。本地首次启动前执行 `make diagnosis-dev-keys`；`make dev-up` 已自动依赖该步骤。
历史缺少 `bundle_encryption`（证据包加密配置）的制品不可原地补写，必须重生成计划和制品。
下载响应通过 `X-Artifact-SHA256`、`X-Detached-Signature`、
`X-Public-Key-Base64` 和 `X-Public-Key-Fingerprint` 返回校验材料；公钥指纹仍须通过
可信第二通道核对。

新生成制品使用 `schema_version=1.2` 和 `artifact_type=structured_collector`，同时签名
结构化执行清单原始字节和 `artifact-manifest.json`。历史 `1.0/1.1` Shell 制品不满足
无 Shell 执行契约，下载验证包时会
返回 `ARTIFACT_VERIFICATION_MANIFEST_UNAVAILABLE`，必须重新生成，禁止伪装成已具备
完整离线信任链。

Verification Bundle（验证包）为 ZIP 文件，包含结构化执行清单、签名 Manifest（清单）、
`runtime-manifest.json`（运行时清单）、`trust-store.json`（受信根）、24 小时有效的签名
`revocations.json`（吊销清单）、内嵌 `case.json`（工单上下文）、操作说明，以及
`hci-collect-linux-amd64`（Linux x86_64 静态 Go 运行时）。客户解压后直接执行：

```console
./hci-collect-linux-amd64 --expected-root-fingerprint <可信第二通道提供的指纹>
```

该程序不依赖 Python、pip、cryptography、OpenSSL、glibc、Bash 启动器或联网安装。
它使用 Go 标准库完成 Ed25519（爱德华曲线数字签名）验证、RSA-OAEP-SHA256
（RSA 最优非对称加密填充）密钥封装和 AES-256-GCM（认证加密）打包，并先依据签名的
运行时清单校验自身 SHA-256；`case.json` 同样由受信根签名，并与制品 `session_id`
（会话标识）绑定。运行时逐项以参数数组直接创建进程，不调用 `/bin/sh`、Bash、curl
或 timeout。吊销清单过期、未知 `key_id`、运行时/制品/清单被篡改、
制品过期或制品已撤销时均默认拒绝。下载响应额外返回
`X-Verification-Bundle-SHA256`（验证包摘要），可与受控下载通道记录交叉核对。

## 内部 API 调用头

```http
Authorization: Bearer <INTERNAL_API_TOKEN>
X-Tenant-ID: <tenant-id>
X-Actor-ID: <service-or-user-id>
Idempotency-Key: <request-id>
```

`Idempotency-Key`（幂等键）用于创建诊断会话和生成采集计划；画像发布依赖内容
checksum（校验和）自动复用相同 revision（修订版本）。Collector 定义更新、
审批和禁用使用 `If-Match`（条件更新头）与 `ETag`（实体标签）实现乐观锁。

## 已开放接口

- `POST /api/diagnosis-sessions`：创建 DiagnosisSession（诊断会话）；
- `GET /api/diagnosis-sessions/{session_id}`：读取诊断会话；
- `POST /api/internal/collection-profiles/{profile_id}/revisions`：发布 Collection Profile（采集画像）修订版本；
- `GET /api/internal/collection-profiles/{profile_id}`：读取当前生效采集画像；
- `POST /api/diagnosis-sessions/{session_id}/collection-plans`：生成 Collection Plan（采集计划）；
- `GET /api/diagnosis-sessions/{session_id}/collection-plans/{plan_id}`：读取采集计划；
- `PUT /api/internal/collectors/{collector_id}`：创建或更新 Collector 草稿；
- `GET /api/internal/collectors`：按审批/启用状态列出 Collector；
- `POST /api/internal/collectors/{collector_id}/review`：批准或拒绝 Collector；
- `POST /api/internal/collectors/{collector_id}/disable`：禁用 Collector；
- `GET /api/internal/collectors/{collector_id}`：读取 Collector 事实源和生效修订版本；
- `POST /api/diagnosis-sessions/{session_id}/collector-artifacts`：生成签名 Linux 制品；
- `GET /api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}`：读取制品元数据；
- `GET /api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}/download`：下载制品；
- `GET /api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}/verification-bundle`：下载离线验证包；
- `POST /api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}/revoke`：撤销制品。
- `GET /api/internal/collectors/security/trust-store`：读取当前 P0 单密钥受信根；
- `GET /api/internal/collectors/security/revocations`：读取租户范围签名吊销清单。

证据上传、诊断分析、报告审核、补采、时间线、法务保全、删除和信号映射接口以
`/docs` 中的 OpenAPI（开放接口规范）为准；当前共 43 条路径。
