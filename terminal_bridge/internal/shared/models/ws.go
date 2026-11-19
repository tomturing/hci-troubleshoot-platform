package models

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"

	"golang.org/x/net/websocket"
)

type SpcFile struct {
	Slot    string `json:"slot"`
	Content string `json:"content"`
	SHA256  string `json:"sha256"`
}

// ScriptFile 是平台「收集脚本」库下发的一份脚本。
//
// 与 SpcFile 同构但落地目录不同：spc 写清单目录下的 <slot>.json，
// 脚本写与 spc 同级的脚本目录下的 <code>（code 是带扩展名的文件名）。
// 内容体积上界由平台限制在 4MB，桥侧仍需独立复算摘要——载荷经前端透传，不可信。
type ScriptFile struct {
	Code        string `json:"code"`
	Content     string `json:"content"`
	SHA256      string `json:"sha256"`
	Interpreter string `json:"interpreter,omitempty"`
	Revision    int    `json:"revision,omitempty"`
}

type InMessage struct {
	Type           string         `json:"type"`
	CaseID         string         `json:"case_id"`
	Host           string         `json:"host"`
	Username       string         `json:"username"`
	Port           int            `json:"port"`
	AuthType       string         `json:"auth_type"`
	Password       string         `json:"password"`
	PrivateKey     string         `json:"private_key"`
	Passphrase     string         `json:"passphrase"`
	Data           string         `json:"data"`
	Command        string         `json:"command"`
	ExecID         string         `json:"exec_id"`   // 用于 ssh_exec_command 和 ssh_exec_process
	NodeIP         string         `json:"node_ip"`   // 目标节点 IP（多节点路由）
	Container      string         `json:"container"` // 目标容器名（空或"host"=物理机直连）
	Timeout        int            `json:"timeout"`   // 命令最大执行秒数（1-300）
	TraceID        string         `json:"trace_id"`  // 端到端链路追踪 ID（Custom-UI → Bridge → Agent 统一）
	Traceparent    string         `json:"traceparent"`
	Tracestate     string         `json:"tracestate"`
	ExecutionMode  string         `json:"execution_mode,omitempty"`
	TestRunID      string         `json:"test_run_id,omitempty"`
	ConversationID string         `json:"conversation_id"`
	ToolCallID     string         `json:"tool_call_id"`
	Resume         bool           `json:"resume"`         // P0-2: 浏览器重连时发送 resume 信号，触发历史日志回放  // 端到端链路追踪 ID（Custom-UI → Bridge → Agent 统一）
	OutputFilters  []OutputFilter `json:"output_filters"` // 平台定义的安全逐行筛选，不执行 shell/正则
	// Pty 强制为本次命令分配伪终端。acli 插件命令（如 log_collect）在容器内执行时会
	// 校验 stdin 是否为 tty，非 PTY 直接报 "input is not a terminal" 且退出码仍为 0，
	// 极易被误判成功。默认对 "acli " 开头的命令自动开启，此字段用于显式覆盖。
	Pty bool `json:"pty,omitempty"`

	// ── acli_sync 专用字段 ──
	Force     bool   `json:"force"`              // 强制重新安装/更新
	MinDiskMB int    `json:"min_disk_mb"`        // 最小剩余磁盘要求（默认 100MB）
	AcliURL   string `json:"acli_url,omitempty"` // 可选自定义下载链接
	// RetryStage 由前端「重试」按钮指定复用的起点，避免把已完成的阶段重跑一遍：
	//   all（默认）—— 重新下载 + 上传 + 安装（能修复下载侧污染）；
	//   upload      —— 复用已校验的安装包缓存，只重传（对应「重新上传」按钮）；
	//   install     —— 复用节点上已校验的安装包，只重跑安装命令。
	RetryStage string `json:"retry_stage,omitempty"`
	// ── acli_sync 第二阶段（插件差量同步）专用字段 ──
	// 对外契约只**增**这一个可选字段（platform_url 复用交付/日志取证已有的同名字段，
	// 三处都是“平台根地址”，语义同一）：旧桥不认识 → 阶段二不执行，阶段一与
	// 终端/日志业务完全不受影响（兼容路径）。
	// PluginTicket 是平台按 case 签发的短期只读票据，只能拉 manifest 与插件包。
	// 桥拿的是票据，绝不是管理令牌或客户身份——桥跑在客户机上，是不受信进程。
	// 自签证书平台沿用交付通道已有的 tls_insecure 用户授权位，不再新增契约字段。
	PluginTicket string `json:"plugin_ticket,omitempty"`

	// ── log_collect 专用字段 ──
	// 参数在 bridge 侧二次校验并以单引号转义后拼命令：acli 插件本身不防注入。
	LogAction  string   `json:"log_action,omitempty"`  // probe | create | progress | cancel | stat | transfer
	TicketNo   string   `json:"ticket_no,omitempty"`   // acli log_collect -s
	Modules    []string `json:"modules,omitempty"`     // acli log_collect -m 枚举
	DateFrom   string   `json:"date_from,omitempty"`   // YYYY-MM-DD
	DateTo     string   `json:"date_to,omitempty"`     // YYYY-MM-DD
	IPs        string   `json:"ips,omitempty"`         // all 或逗号分隔 IPv4
	ExtraPaths []string `json:"extra_paths,omitempty"` // acli log_collect -p
	// ConfigJSON 是平台生成的 `-c` 配置原文（只含 paths），由前端原样透传。
	// 平台是唯一生成方：bridge 必须二次校验、写入节点、回读比对摘要，
	// 绝不接受前端自行拼装的路径清单——这条链路最终会拼进 shell 命令。
	ConfigJSON string `json:"config_json,omitempty"`
	// ConfigSHA256 是下发内容的摘要，bridge 写完文件后回读比对，不符即拒绝执行。
	ConfigSHA256 string `json:"config_sha256,omitempty"`
	// SpcJSON 是平台生成的 spc 清单原文（`-c` 的替代通道）。
	//
	// 与 ConfigJSON 互斥：两者同时到达说明调用方版本错乱或载荷被改过，必须拒绝。
	// spc 是"节点本地文件"而不是命令参数，bridge 要做的是**扇出写到每台目标节点**
	// 再执行 acli；只写发起节点等于其余节点仍按原清单收集，且不会报任何错。
	SpcJSON string `json:"spc_json,omitempty"`
	// SpcSHA256 是 spc 原文摘要，bridge 侧独立复算，不符即拒绝（防截断/改写）。
	SpcSHA256 string `json:"spc_sha256,omitempty"`
	// SpcSlot 是覆写槽位，留空取 core；非白名单值直接拒绝，避免未来平台在
	// 不升级 bridge 的前提下悄悄换槽位，把厂商原文件指到别的模块上。
	SpcSlot string `json:"spc_slot,omitempty"`
	// SpcFiles 是默认场景的逐槽位清单：每个槽位对应 acli 的一个 -m 模块，
	// 各自覆写同名清单文件。与 SpcJSON 互斥，后者是自定义场景的单槽位通道。
	// 旧桥不认识此字段会静默忽略，前端必须先做 capabilities 探测再启用。
	SpcFiles []SpcFile `json:"spc_files,omitempty"`
	// SpcSlots 供平台兜底 restore_spc 指定要还原的槽位集合；
	// 留空时桥按内存态/单槽位兼容路径还原。
	SpcSlots []string `json:"spc_slots,omitempty"`
	// ScriptFiles 是需要覆写到节点 script/ 目录的平台脚本（创建时内联）。
	// 与 spc 是两条独立的文件通道：可以只下发脚本，也可以两者同时下发。
	ScriptFiles []ScriptFile `json:"script_files,omitempty"`
	// ScriptCodes 供平台兜底 restore_script 指定要还原的脚本文件名集合。
	ScriptCodes []string `json:"script_codes,omitempty"`
	// RemoteURL 在 log_collect 中语义为：probe 时作为被探测的日志服务器地址；
	// create 时作为 -u 上传目标（仅 http/https，且只认官方日志服务器白名单）。
	RemoteURL string `json:"remote_url,omitempty"`
	// UploadTicket 是平台签发的 P1 直传票据（仅创建时下发，短时效、绑定工单）。
	//
	// 为什么需要它：5888 启用 ACLI_UPLOAD_TOKEN 后，acli 直传不带任何认证头，
	// /init_upload 全量 401（2026-11-03 Q2026110300278）。acli 1.0.0 起支持
	// `-L|--url-header`，因此 bridge 把票据注入成一个**只对第 0 个上传地址生效**
	// 的请求头，而不改动 acli。
	//
	// 为空表示不带凭据：此时应走 P2——不要用空票据去发 P1，那只会换来一次 401
	// 和一条"直传失败"的假故障。
	UploadTicket string `json:"upload_ticket,omitempty"`
	// EvidenceTicket 是平台签发的证据上报票据（绑定 task_id、24h），只在 create 下发。
	// 不落盘、不回显给任何 WS 回包：泄露也只影响单个任务，且随进程重启自然失效。
	EvidenceTicket string `json:"evidence_ticket,omitempty"`

	// ── bridge_log_token（日志直传凭据）专用字段 ──
	// 前端向平台换取的短期令牌，只允许写绑定工单的日志；
	// bridge 拿到后自行上报，不再依赖浏览器在线。
	LogToken    string `json:"token,omitempty"`
	LogEndpoint string `json:"endpoint,omitempty"`

	// ── log_collect transfer（P3 段①+段②）专用字段 ──
	// 段① 用 `tail -c +N` 从 HCI 续读到客户机；段② 按 chunk_size 切片直传平台数据面。
	// 平台的 init / complete 由前端调用（需客户身份），bridge 只持数据面 token，
	// 因此这里不包含任何平台控制面字段。
	TransferSourcePath string `json:"source_path,omitempty"` // HCI 上的 zip 绝对路径
	// PlatformURL 是平台根地址（http/https），三条链路共用：日志取证上传、
	// 交付通道、acli 插件第二阶段的 manifest/包拉取。JSON 名不能重复声明：
	// encoding/json 对同层重复 tag 会**两边都解不出来**，静默把票据拉取变成空 URL。
	PlatformURL           string `json:"platform_url,omitempty"`   // 平台根地址（http/https）
	TransferToken         string `json:"upload_token,omitempty"`   // 平台签发的数据面 token
	TransferChunkSize     int64  `json:"chunk_size,omitempty"`     // 分片大小（字节）
	TransferPartCount     int    `json:"part_count,omitempty"`     // 分片总数
	TransferTotalSize     int64  `json:"total_size,omitempty"`     // 压缩包总大小
	TransferUploadedParts []int  `json:"uploaded_parts,omitempty"` // 平台已收到的分片（续传跳过）
	TransferLocalPath     string `json:"local_path,omitempty"`     // 可选：客户机落地路径
	// TransferDeliveryMode 决定交付前是否等采集：
	//   collect（默认）—— 采集进行中，先只读 progress 等它收完再拉包；
	//   direct        —— 用户显式要求"补交付已有包"，跳过等待，直接 stat 校验后拉包。
	// 为什么必须由调用方显式声明：acli progress 是节点全局态、不带任务身份，
	// 单看它能拿到的是"上一次采集的完成态"（2026-10-27 现场：新任务 4 秒即被判完成）。
	TransferDeliveryMode string `json:"delivery_mode,omitempty"`
	// TransferTLSInsecure 由前端在用户确认"该平台使用自签证书"后置位；bridge 只对本次交付放宽校验，不写全局状态。
	TransferTLSInsecure bool `json:"tls_insecure,omitempty"`
	// JobID 供 delivery_status / resume / cancel 定位作业（作业化后前端不再等待长任务）。
	JobID string `json:"job_id,omitempty"`

	// ── sftp_op（远端文件下载）专用字段 ──
	// 只传动作与远端路径：本地落点由 bridge 自行决定，绝不接受浏览器指定的绝对路径
	// （桌面模式 Origin=*，任意页面都能连 ws://localhost:9999）。
	SftpAction string `json:"sftp_action,omitempty"` // list | download | cancel
	SftpPath   string `json:"sftp_path,omitempty"`   // 远端绝对路径

	// ── WebSocket 保活（ping/pong）专用字段 ──
	Pong int64 `json:"pong,omitempty"` // 客户端回复心跳时间戳（回显服务端 ping 值）

	// ── vm_console_op（虚拟机控制台截图固定操作）专用字段 ──
	// 该消息类型刻意不包含自由文本 command 字段：Bridge 只执行代码常量表
	// 构造的固定操作（screendump / sendkey down / 固定读取 / 无条件删除）。
	Operation  string `json:"operation"`    // 仅 capture_baseline | wake_down_key，其他一律拒绝
	CaptureID  string `json:"capture_id"`   // 服务端生成的 UUID，截图临时文件名
	HostNodeID string `json:"host_node_id"` // Inventory 验证后的宿主机节点标识（vtpsh 路径）
	VMID       string `json:"vm_id"`        // 纯数字 VMID
}

// OutputFilter 只能表达字面量行筛选，刻意不支持命令、正则、脚本和管道。
type OutputFilter struct {
	Source        string   `json:"source"`
	Include       []string `json:"include"`
	Exclude       []string `json:"exclude"`
	IncludeMode   string   `json:"include_mode"`
	ExcludeMode   string   `json:"exclude_mode,omitempty"`
	CaseSensitive bool     `json:"case_sensitive"`
}

type OutMessage struct {
	Type    string `json:"type"`
	CaseID  string `json:"case_id"`
	Output  string `json:"output,omitempty"`
	Message string `json:"message,omitempty"`
	Detail  string `json:"detail,omitempty"`
	ExecID  string `json:"exec_id,omitempty"` // 用于 exec_result
	// exec_result 必须始终携带 exit_code；0 是成功值，不能因 omitempty 被省略，
	// 否则浏览器会把缺失字段解析成 NaN，误判已成功的命令为失败。
	ExitCode    int    `json:"exit_code"`           // 用于 exec_result
	Stdout      string `json:"stdout,omitempty"`    // 双通道物理隔离输出 (Scheme B)
	Stderr      string `json:"stderr,omitempty"`    // 双通道物理隔离输出 (Scheme B)
	TraceID     string `json:"trace_id,omitempty"`  // 回显端到端 trace_id
	CustomUI    string `json:"custom_ui,omitempty"` // 来源 Custom-UI（自动按 Origin 关联）
	Traceparent string `json:"traceparent,omitempty"`
	ArtifactID  string `json:"artifact_id,omitempty"`
	// Sessions 仅用于 list_sessions_result：当前已登录的 SSH 会话摘要，不含凭据。
	Sessions        []BridgeSessionInfo `json:"sessions,omitempty"`
	StdoutBytes     int64               `json:"stdout_bytes,omitempty"`
	StderrBytes     int64               `json:"stderr_bytes,omitempty"`
	StdoutSHA256    string              `json:"stdout_sha256,omitempty"`
	StderrSHA256    string              `json:"stderr_sha256,omitempty"`
	StdoutTruncated bool                `json:"stdout_truncated,omitempty"`
	StderrTruncated bool                `json:"stderr_truncated,omitempty"`
	DurationMS      int64               `json:"duration_ms,omitempty"`
	TimedOut        bool                `json:"timed_out,omitempty"`
	Cancelled       bool                `json:"cancelled,omitempty"`
	ErrorType       string              `json:"error_type,omitempty"`
	// PtyMode 声明本次执行的终端语义：pipe（默认，stdout/stderr 双通道）或 pty
	// （伪终端，stderr 合并进 stdout）。下游不得把 pty 模式下"stderr 为空"解释为无错误。
	PtyMode         string `json:"pty_mode,omitempty"`
	StderrPtyMerged bool   `json:"stderr_pty_merged,omitempty"`
	// 模拟执行标记只来自已认证的 sim-ssh 租约上下文，绝不从 host 名推断。
	Simulation        bool   `json:"simulation,omitempty"`
	ExecutionMode     string `json:"execution_mode,omitempty"`
	SimulationBackend string `json:"simulation_backend,omitempty"`

	// ── vm_console_result（虚拟机控制台截图结果）专用字段 ──
	// WS 只回元数据：原始 PPM 经 HTTP 直传制品服务，绝不降级走 WebSocket。
	CaptureID    string `json:"capture_id,omitempty"`    // 截图 capture UUID 回显
	Operation    string `json:"operation,omitempty"`     // capture_baseline | wake_down_key 回显
	SHA256       string `json:"sha256,omitempty"`        // 解码后原始 PPM 的 SHA-256
	SizeBytes    int64  `json:"size_bytes,omitempty"`    // 解码后原始 PPM 字节数
	UploadStatus string `json:"upload_status,omitempty"` // uploaded | artifact_upload_disabled | upload_failed | not_applicable

	// ── acli_sync 专用字段 ──
	Status         string `json:"status,omitempty"` // up_to_date | installed | disk_space_insufficient | error | checking | downloading | uploading | installing | plugins
	Architecture   string `json:"architecture,omitempty"`
	CurrentVersion string `json:"current_version,omitempty"`
	LatestVersion  string `json:"latest_version,omitempty"`
	AvailableMB    int64  `json:"available_mb,omitempty"`
	RequiredMB     int64  `json:"required_mb,omitempty"`
	// 安装包完整性事实：让「包到底对不对」不再需要登录节点去猜。
	// IntegrityState 是校验强度的自证：sha256/md5 为真比对，size_only 是降级，
	// 前端必须把 size_only 显式展示成「仅比对了字节数」。
	PackageSize     int64  `json:"package_size,omitempty"`
	PackageSHA256   string `json:"package_sha256,omitempty"`
	PackageMD5      string `json:"package_md5,omitempty"`
	PackageFilename string `json:"package_filename,omitempty"`
	PackagePath     string `json:"package_path,omitempty"`
	// 上传进度：字节级事实，避免长链路只能看到“正在上传...”。
	UploadedBytes  int64  `json:"uploaded_bytes,omitempty"`
	Percent        int    `json:"percent,omitempty"`
	IntegrityState string `json:"integrity_state,omitempty"`
	RemoteSize     int64  `json:"remote_size,omitempty"`
	RemoteAlgo     string `json:"remote_algo,omitempty"`
	RemoteDigest   string `json:"remote_digest,omitempty"`
	Attempts       int    `json:"attempts,omitempty"`
	RetryStage     string `json:"retry_stage,omitempty"`
	Stage          string `json:"stage,omitempty"` // download | upload | verify | install
	// 安装命令的完整事实：退出码 0 在 omitempty 下会被吞掉，因此用 InstallSuccess
	// 表达「成功」这一结论，退出码只作为附加信息。
	InstallSuccess  bool   `json:"install_success,omitempty"`
	InstallExitCode int    `json:"install_exit_code,omitempty"`
	InstallMS       int64  `json:"install_ms,omitempty"`
	InstallOutput   string `json:"install_output,omitempty"`
	InstallStderr   string `json:"install_stderr,omitempty"`
	// PluginSync 是 acli_sync 第二阶段回执（只读事实，不参与阶段一结论）：
	// 阶段二失败绝不把已成功的安装整体判失败，反之阶段一失败时阶段二根本不跑。
	PluginSync *AcliPluginStageReceipt `json:"plugin_sync,omitempty"`
	// Degraded 如实标注"本次结论带降级"（B6：期望证据缺失未做升级判定；
	// B7：磁盘空间未验证）。只增可选字段，旧前端忽略不受影响。
	Degraded bool `json:"degraded,omitempty"`

	// ── log_collect_result 专用字段 ──
	// 进度与探测结果是结构化数据；RAW 保留插件原文，解析失败时前端按原文展示，
	// 不得据此把任务判为失败（插件文案可能随版本变化）。
	LogAction string `json:"log_action,omitempty"`
	LogData   any    `json:"log_data,omitempty"`
	Raw       string `json:"raw,omitempty"`
	// TicketNo 原样回显请求里的工单号。acli 的采集任务是**节点级单任务**，
	// 同一台节点可能正在跑另一个工单的采集；前端必须据此判断"这份进度属于谁"，
	// 否则会把别单的 85% 渲染成本单的进度（2026-10-28 现场）。
	TicketNo string `json:"ticket_no,omitempty"`

	// ── 统一错误诊断字段（全链路错误码）──
	// ErrorCode 是规范码（LC-<LAYER>-<NNN>），layer/owner/retryable 由 SendMsg 自动补全；
	// ErrorType 保留人类可读短名，向后兼容旧前端。
	ErrorCode      string `json:"error_code,omitempty"`
	ErrorLayer     string `json:"error_layer,omitempty"`
	ErrorOwner     string `json:"error_owner,omitempty"`
	ErrorRetryable string `json:"error_retryable,omitempty"`

	// ── sftp_list_result / sftp_transfer_progress / sftp_result 专用字段 ──
	// 字节不经 WebSocket：这里只有目录项、进度与落地路径等元数据。
	SftpAction string `json:"sftp_action,omitempty"`
	SftpData   any    `json:"sftp_data,omitempty"`

	// ── WebSocket 保活（ping/pong）专用字段 ──
	Ping int64 `json:"ping,omitempty"` // 服务端发送心跳时间戳（Unix毫秒），客户端需回复 pong
}

// ErrorFieldsFunc 由装配方注入（logcollect 错误码表），models 不认识消费者。
var ErrorFieldsFunc func(code string) (layer, owner, retryable string)

// SendErrorHook 由装配方注入（blog 留痕 + metrics），models 不认识 logpush/app。
var SendErrorHook func(msg OutMessage, err error)

var WebsocketWriteLocks sync.Map

func SendWebSocketRaw(ws *websocket.Conn, payload string) error {
	lockValue, _ := WebsocketWriteLocks.LoadOrStore(ws, &sync.Mutex{})
	lock := lockValue.(*sync.Mutex)
	lock.Lock()
	defer lock.Unlock()
	return websocket.Message.Send(ws, payload)
}

func SendMsg(ws *websocket.Conn, msg OutMessage) {
	// 统一错误诊断：只要带了规范错误码，就自动补齐层/责任方/可重试性，
	// 避免每个调用点重复填写导致漂移（元数据来自 log_collect_errors.go）。
	if msg.ErrorCode != "" && msg.ErrorLayer == "" {
		if ErrorFieldsFunc != nil {
			msg.ErrorLayer, msg.ErrorOwner, msg.ErrorRetryable = ErrorFieldsFunc(msg.ErrorCode)
		}
	}
	data, _ := json.Marshal(msg)
	if err := SendWebSocketRaw(ws, string(data)); err != nil {
		// 发送失败 = 结果回不去浏览器：必须显式记录，否则表现为"命令没有结果"却无任何线索。
		if SendErrorHook != nil {
			SendErrorHook(msg, err)
		}
	}
}

type BridgeSessionInfo struct {
	CaseID string `json:"case_id"`
	NodeIP string `json:"node_ip"`
	Host   string `json:"host"`
	Key    string `json:"key"`
}

type AcliPluginStageReceipt struct {
	Stage           string                   `json:"stage"`
	OK              bool                     `json:"ok"`
	Reason          string                   `json:"reason,omitempty"`
	Detail          string                   `json:"detail,omitempty"`
	DesiredCount    int                      `json:"desired_count"`
	Installed       int                      `json:"installed"`
	Overwritten     int                      `json:"overwritten"`
	Skipped         int                      `json:"skipped"`
	Failed          int                      `json:"failed"`
	Nodes           []string                 `json:"nodes,omitempty"`
	Plugins         []AcliPluginPluginResult `json:"plugins,omitempty"`
	Warnings        []string                 `json:"warnings,omitempty"`
	ManifestVersion string                   `json:"manifest_version,omitempty"`
	ElapsedMS       int64                    `json:"elapsed_ms"`
}

type AcliPluginPluginResult struct {
	Name     string                  `json:"name"`
	Version  string                  `json:"version,omitempty"`
	SpecHash string                  `json:"spec_hash,omitempty"`
	Action   string                  `json:"action"` // 最差动作：failed > overwrite > install > skip
	Hosts    []AcliPluginHostOutcome `json:"hosts,omitempty"`
}

type AcliPluginHostOutcome struct {
	IP        string `json:"ip"`
	Action    string `json:"action"` // install | overwrite | skip | failed | unreachable
	Reason    string `json:"reason,omitempty"`
	Status    string `json:"device_status,omitempty"` // 设备侧 ACP_RESULT.status
	Error     string `json:"error,omitempty"`         // 设备侧自检错误键
	Smoke     string `json:"smoke_preview,omitempty"`
	ElapsedMS int64  `json:"elapsed_ms,omitempty"`
}

func (r *AcliPluginStageReceipt) SummaryText() string {
	if r == nil {
		return ""
	}
	if r.Reason != "" {
		return fmt.Sprintf("\n插件同步：未执行（%s）", r.Reason)
	}
	return fmt.Sprintf("\n插件同步：新增 %d、覆盖 %d、跳过 %d、失败 %d（期望 %d 个插件 / %d 个节点）",
		r.Installed, r.Overwritten, r.Skipped, r.Failed, r.DesiredCount, len(r.Nodes))
}

type ExecRequestContext struct {
	Context         context.Context
	CaseID          string
	ConversationID  string
	ExecID          string
	ToolCallID      string
	TraceID         string
	Traceparent     string
	Tracestate      string
	TestRunID       string
	NodeIP          string
	Container       string
	CustomUI        string
	Command         string
	CommandRedacted string
	CommandSHA256   string
	// Pty 表示本次执行使用伪终端；由 shouldUsePty 在单一入口判定后写入，
	// 保证日志、输出语义与真实终端行为一致。
	Pty bool
}
