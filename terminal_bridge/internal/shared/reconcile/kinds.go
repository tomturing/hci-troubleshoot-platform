// Package reconcile 是节点同步链路的纵向骨架（收口设计 §二/§三）。
//
// 两轴分离：
//   - internal/shared/remote  横向节点面原语（执行/写入/探测/传输/转义/标记）
//   - internal/shared/reconcile 纵向编排（期望态 → 比对 → 写入 → 安装 → 验证 → 回执）
//
// 硬约束（§七，由 terminal_bridge/remote_convergence_test.go 的静态守卫钉死）：
//   - reconcile 只能 import remote 与标准库，禁止 import main；
//   - remote 禁止 import reconcile；
//   - 包名用 reconcile 而非 sync，避免遮蔽 stdlib 的 sync.Mutex。
//
// 新增一条链路只需新增一个 DesiredState 值 + 一个 Provider，不改骨架。
package reconcile

// Kind 是链路枚举。四条现存链路全部落在这四个值上（§3.1）。
const (
	KindAcliBinary   = "acli_binary"
	KindSpcSlot      = "spc_slot"
	KindConfigFile   = "config_file"
	KindPluginBundle = "plugin_bundle"
)

// InstallSpec.Kind：安装动作是声明式的，descriptor 里不出现命令字符串（补丁 1）。
//
// 通用动作（none/chmod_exec）由本包渲染；产品特有动作（acli_install/
// register_plugin）由调用方经 Deps.InstallFunc 注册渲染器——命令仍在 Go 侧
// 受控拼装，但 reconcile 不需要认识 acli 或插件注册表长什么样。
const (
	InstallNone           = "none"
	InstallChmodExec      = "chmod_exec"
	InstallAcli           = "acli_install"
	InstallRegisterPlugin = "register_plugin"
)

// SmokeSpec.Kind：声明"怎样证明成功"，不是"跑哪条 shell"。
const (
	SmokeNone           = "none"
	SmokeVersionMatch   = "version_match"
	SmokeStdoutContains = "stdout_contains"
	SmokeSHA256         = "sha256"
)

// Source.Kind：大文件走传输原语引用，不把 20MB 包序列化进 JSON（补丁 2）。
const (
	SourcePackage = "package"
	SourceInline  = "inline"
	SourceRemote  = "remote"
)

// 回滚/还原策略（补丁 3：失败回滚与成功还原是两件事）。
const (
	RollbackRestoreBackup = "restore_backup"
	RollbackUnregister    = "unregister"
	RollbackNone          = "none"

	RestoreOriginal = "restore_original"
	RestoreNone     = "none"

	WaitTaskTerminal = "task_terminal"
	WaitImmediate    = "immediate"
)

// VersionSpec.Mode：版本比对判序。
//
//	ordered —— 版本优先 + 日期兜底 + 禁降级（acli，判序函数由调用方注入）
//	hash    —— spec_hash 差量三态（plugin）
//	digest  —— 文件摘要差量
//	none    —— 总是覆写（config/spc 现状：按 task 隔离，无需比对）
const (
	VersionOrdered = "ordered"
	VersionHash    = "hash"
	VersionDigest  = "digest"
	VersionNone    = "none"
)

// Metadata key 白名单（补丁 5）。
//
// Metadata 只能是展示/trace 的逃生舱，**不得驱动流程分支**：
// 本包内禁止出现 Metadata[...] 形式的读取（静态守卫扫描），
// 校验与回执一律用 range 遍历。真正需要新语义时扩强类型字段
// （如 FileSpec.Dir / Slot），而不是往 Metadata 里塞开关。
const (
	MDKeyTaskID     = "task_id"
	MDKeySyncID     = "sync_id"
	MDKeyCaseID     = "case_id"
	MDKeyTraceID    = "trace_id"
	MDKeySPCSlot    = "spc_slot"
	MDKeyPluginName = "plugin_name"
	MDKeySpecHash   = "spec_hash"
	MDKeyArch       = "arch"
	MDKeyRetryStage = "retry_stage"
	MDKeyCustomUI   = "custom_ui"
)

var metadataKeyWhitelist = map[string]bool{
	MDKeyTaskID:     true,
	MDKeySyncID:     true,
	MDKeyCaseID:     true,
	MDKeyTraceID:    true,
	MDKeySPCSlot:    true,
	MDKeyPluginName: true,
	MDKeySpecHash:   true,
	MDKeyArch:       true,
	MDKeyRetryStage: true,
	MDKeyCustomUI:   true,
}

// IsMetadataKey 报告 key 是否已登记。未登记的 key 由 Normalize 拒绝，
// 避免"随手塞一个字段"慢慢长成第二套 descriptor。
func IsMetadataKey(key string) bool { return metadataKeyWhitelist[key] }

// ValidKind 报告 kind 是否是已登记链路。
func ValidKind(kind string) bool {
	switch kind {
	case KindAcliBinary, KindSpcSlot, KindConfigFile, KindPluginBundle:
		return true
	}
	return false
}
