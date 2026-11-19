package reconcile

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// descriptor.go —— 期望态的数据驱动建模（收口设计 §2.2，五个补丁全部落地）。
//
// 第一性原理：新增一条链路只需新增一个 DesiredState 值，不改骨架。
// 因此这里只描述"应该达成什么状态"，绝不出现命令字符串——命令渲染在
// Go 侧受控完成（补丁 1），把拼接面从 Python 挪到 Go 不是收口，是换个地方漏。

// DesiredState 描述"某条链路在某台节点上应该达成什么状态"。
type DesiredState struct {
	Kind     string            // KindAcliBinary | KindSpcSlot | KindConfigFile | KindPluginBundle
	NodeIP   string            // 目标节点（发起节点或集群成员）
	TaskKey  string            // 任务/部署键：备份隔离与持久还原意图的主键（spc=taskID、plugin=syncID）
	Files    []FileSpec        // 要写入的文件清单
	Install  InstallSpec       // 安装动作（声明式）
	Smoke    SmokeSpec         // 冒烟测试（声明式）
	Policy   ReconcilePolicy   // 失败/成功后的行为
	Version  VersionSpec       // 版本比对（差量三态）
	Metadata map[string]string // 链路特有的透传字段，只能展示/trace（补丁 5）
}

// FileSpec 描述要写到节点上的一个文件。
//
// Dir 是落地时新增的强类型字段（补丁 5 的正确用法：需要新语义就扩字段，
// 不塞 Metadata）：config/spc 的目标目录可能被 acli 重装清掉，写前必须
// mkdir -p 重建；acli 的落点目录恒在，留空即不额外下发 mkdir，
// 保住其远端命令序列逐字不变（S4 golden 验收）。
type FileSpec struct {
	Path   string // 节点上的目标路径
	SHA256 string // 期望摘要（hex）
	Size   int64  // 期望大小
	Mode   string // 权限位，如 "0755"；空则沿用 remote 的默认
	Dir    string // 写前需确保存在的目录；空则不下发 mkdir
	Source Source // 内容来源
	// BackupPath 是回滚/还原的前提（B1 构造性不变量）。需要备份的策略下必填，
	// 且必须任务/部署隔离（spc <path>.platform.<taskID>.bak、plugin <path>.old.<syncID>）：
	// 固定 .bak 后缀会让两次采集互相覆盖原件。
	BackupPath string
	// IsDirTree 为真时按目录树整体回滚（插件目录是树粒度，只登记文件会漏掉它）。
	IsDirTree bool
}

// Source 区分三种来源，避免把 20MB 的包序列化进 JSON（补丁 2）。
type Source struct {
	Kind   string // SourcePackage | SourceInline | SourceRemote
	URL    string // Kind=package：下载地址（acli=官方源；plugin=平台票据端点）
	Offset int64  // Kind=package：断点偏移
	Data   []byte // Kind=inline：小配置直接带
	Path   string // Kind=remote：节点上已有的路径
}

// InstallSpec 声明"要达成什么"，Args 是结构化参数，由渲染器转义拼装。
type InstallSpec struct {
	Kind string
	Args []string
}

// SmokeSpec 声明"怎样证明成功"。
type SmokeSpec struct {
	Kind string
	Args []string
}

// ReconcilePolicy 是三钩子（补丁 3）：失败回滚与成功还原混在一个 policy 里，
// 必然出现"成功也回滚"或"失败不还原"。
type ReconcilePolicy struct {
	OnFailRollback   RollbackSpec
	OnSuccessRestore RestoreSpec
	KeepBackup       bool // 成功后是否保留备份（插件保留 .old.$SYNC_ID）
}

// RollbackSpec 只接受"已登记写入集"的条目，未登记则跳过——
// 这让 rollback 里的 rm -rf 在结构上不可能再出现（B1）。
type RollbackSpec struct {
	Strategy string // RollbackRestoreBackup | RollbackUnregister | RollbackNone
}

// RestoreSpec 的 WaitFor=task_terminal 不是内存尾钩子：
// 流水线返回前把还原意图持久化，由任务终态 watcher 消费（§3.2）。
type RestoreSpec struct {
	Strategy string // RestoreOriginal | RestoreNone
	WaitFor  string // WaitTaskTerminal | WaitImmediate
}

// VersionSpec 描述比对判序。判序函数本身由调用方注入（见 CompareOptions）：
// acli 的"版本优先 + 日期兜底 + 禁降级"是产品语义，骨架不认识具体产品。
type VersionSpec struct {
	Mode     string // VersionOrdered | VersionHash | VersionDigest | VersionNone
	Expected string // 期望版本 / spec_hash / 期望摘要
	Build    string // 期望构建时间（ordered 模式的日期兜底）
}

// inline 阈值（补丁 4）。规则写死在骨架内部，不依赖调用方自觉。
const (
	// InlineSingleMax：单文件超过它就必须改走传输原语，不进 WS JSON。
	InlineSingleMax = 256 * 1024
	// InlineTotalMax：同一次 descriptor 的 inline 总量上限。
	// 8 个文件每份 2MB 仍会拼出 16MB 消息，被 CLIENT_MAX_CMD_BYTES 拦在桥门口。
	InlineTotalMax = 1 * 1024 * 1024
	// DescriptorMax：降级后仍超过它直接 fail-closed，不尝试发送
	// （发出去只会换来桥侧无回执超时，比当场拒绝更难排障）。
	DescriptorMax = 10 * 1024 * 1024
)

// Normalize 校验并归一化期望态，返回归一化副本、降级证据与错误。
//
// 三件事：
//  1. 结构校验（kind/路径/来源/策略枚举），不合法一律 fail-closed；
//  2. inline 自动降级（单文件 256KB / 总量 1MB），降级必须写 Evidence；
//  3. inline 的 Size/SHA256 由字节自算，杜绝"声明的摘要与实际字节不符"。
//
// 降级的落地口径（与设计表格的一处偏离，理由写在这里）：设计假定降级后
// 有一个可用的 package 引用（URL）。桥内构造的 descriptor 未必有下载地址
// （config/spc 的原文只来自 WS 消息），此时**不能**把 Data 置空——那等于
// 凭空丢掉要写的内容。故：有 URL 才真降级；无 URL 则保留 inline 并记
// inline_downgrade_blocked 证据，最终由 DescriptorMax 硬上限兜住。
// 硬上限在任何情况下都生效，fail-closed 语义不变。
func Normalize(d DesiredState) (DesiredState, []Evidence, error) {
	var ev []Evidence
	if !ValidKind(d.Kind) {
		return d, ev, fmt.Errorf("%s：未知链路 kind %q", CodeDescriptorInvalid, d.Kind)
	}
	if strings.TrimSpace(d.NodeIP) == "" {
		return d, ev, fmt.Errorf("%s：缺少目标节点", CodeDescriptorInvalid)
	}
	for key := range d.Metadata {
		if !IsMetadataKey(key) {
			return d, ev, fmt.Errorf("%s：Metadata key %q 未登记（只能展示/trace，需要新语义请扩强类型字段）", CodeDescriptorInvalid, key)
		}
	}
	if err := validatePolicy(d.Policy); err != nil {
		return d, ev, err
	}
	if err := validateVersionMode(d.Version.Mode); err != nil {
		return d, ev, err
	}
	if needsBackup(d.Policy) && strings.TrimSpace(d.TaskKey) == "" {
		return d, ev, fmt.Errorf("%s：需要备份的策略必须给定 TaskKey（备份名要按任务/部署隔离）", CodeDescriptorInvalid)
	}

	out := d
	out.Files = make([]FileSpec, 0, len(d.Files))
	// 先做单文件降级与摘要自算，再按总量降级（顺序不可颠倒：
	// 总量判定必须基于单文件降级后的事实，否则会把已降级的文件再算一遍）。
	inlineTotal := int64(0)
	for _, f := range d.Files {
		if strings.TrimSpace(f.Path) == "" || !strings.HasPrefix(f.Path, "/") || strings.Contains(f.Path, "..") {
			return d, ev, fmt.Errorf("%s：非法目标路径 %q", CodeDescriptorInvalid, f.Path)
		}
		if needsBackup(d.Policy) {
			if err := validateBackupPath(f); err != nil {
				return d, ev, err
			}
		}
		switch f.Source.Kind {
		case SourceInline:
			if int64(len(f.Source.Data)) > InlineSingleMax && f.Source.URL != "" {
				ev = append(ev, KV("inline_downgraded_file", f.Path))
				f.Source = Source{Kind: SourcePackage, URL: f.Source.URL, Offset: f.Source.Offset}
			} else if int64(len(f.Source.Data)) > InlineSingleMax {
				ev = append(ev, KV("inline_downgrade_blocked", f.Path))
			}
			if f.Source.Kind == SourceInline {
				sum := sha256.Sum256(f.Source.Data)
				digest := hex.EncodeToString(sum[:])
				if f.SHA256 != "" && !strings.EqualFold(f.SHA256, digest) {
					return d, ev, fmt.Errorf("%s：%s 声明的摘要与 inline 字节不符", CodeDescriptorInvalid, f.Path)
				}
				if f.Size != 0 && f.Size != int64(len(f.Source.Data)) {
					return d, ev, fmt.Errorf("%s：%s 声明的大小与 inline 字节不符", CodeDescriptorInvalid, f.Path)
				}
				f.SHA256, f.Size = digest, int64(len(f.Source.Data))
				inlineTotal += f.Size
			}
		case SourcePackage:
			if strings.TrimSpace(f.Source.URL) == "" {
				return d, ev, fmt.Errorf("%s：%s 的 package 来源缺少下载地址（%s）", CodeDescriptorInvalid, f.Path, CodePackageSourceUndef)
			}
		case SourceRemote:
			if strings.TrimSpace(f.Source.Path) == "" {
				return d, ev, fmt.Errorf("%s：%s 的 remote 来源缺少节点路径", CodeDescriptorInvalid, f.Path)
			}
		default:
			return d, ev, fmt.Errorf("%s：%s 的来源 kind %q 非法", CodeDescriptorInvalid, f.Path, f.Source.Kind)
		}
		out.Files = append(out.Files, f)
	}

	// 总量降级：按字节数从大到小逐个降，直到达标。只降"降得动"的（有 URL）。
	if inlineTotal > InlineTotalMax {
		order := make([]int, 0, len(out.Files))
		for i := range out.Files {
			if out.Files[i].Source.Kind == SourceInline {
				order = append(order, i)
			}
		}
		sort.SliceStable(order, func(a, b int) bool {
			return out.Files[order[a]].Size > out.Files[order[b]].Size
		})
		downgraded := 0
		for _, i := range order {
			if inlineTotal <= InlineTotalMax {
				break
			}
			f := out.Files[i]
			if f.Source.URL == "" {
				continue
			}
			inlineTotal -= f.Size
			out.Files[i].Source = Source{Kind: SourcePackage, URL: f.Source.URL, Offset: f.Source.Offset}
			downgraded++
		}
		if downgraded > 0 {
			ev = append(ev, KV("inline_downgraded", fmt.Sprintf("%d", downgraded)))
		}
	}
	if inlineTotal > DescriptorMax {
		return d, ev, fmt.Errorf("%s：inline 总量 %d 字节超过硬上限 %d（降级后仍超限，不尝试发送）",
			CodeDescriptorTooLarge, inlineTotal, DescriptorMax)
	}
	ev = append(ev, KV("inline_bytes", fmt.Sprintf("%d", inlineTotal)), KV("file_count", fmt.Sprintf("%d", len(out.Files))))
	return out, ev, nil
}

func validatePolicy(p ReconcilePolicy) error {
	switch p.OnFailRollback.Strategy {
	case "", RollbackNone, RollbackRestoreBackup, RollbackUnregister:
	default:
		return fmt.Errorf("%s：回滚策略 %q 非法", CodeDescriptorInvalid, p.OnFailRollback.Strategy)
	}
	switch p.OnSuccessRestore.Strategy {
	case "", RestoreNone, RestoreOriginal:
	default:
		return fmt.Errorf("%s：还原策略 %q 非法", CodeDescriptorInvalid, p.OnSuccessRestore.Strategy)
	}
	switch p.OnSuccessRestore.WaitFor {
	case "", WaitImmediate, WaitTaskTerminal:
	default:
		return fmt.Errorf("%s：还原时机 %q 非法", CodeDescriptorInvalid, p.OnSuccessRestore.WaitFor)
	}
	if p.OnSuccessRestore.Strategy == RestoreOriginal && p.OnSuccessRestore.WaitFor == "" {
		return fmt.Errorf("%s：restore_original 必须显式声明 WaitFor", CodeDescriptorInvalid)
	}
	return nil
}

func validateVersionMode(mode string) error {
	switch mode {
	case "", VersionNone, VersionOrdered, VersionHash, VersionDigest:
		return nil
	}
	return fmt.Errorf("%s：版本比对模式 %q 非法", CodeDescriptorInvalid, mode)
}

// needsBackup 报告策略是否依赖备份：失败回滚要它，成功还原也要它。
// 两者缺一都会让"无备份不删"变成一句空话。
func needsBackup(p ReconcilePolicy) bool {
	return p.OnFailRollback.Strategy == RollbackRestoreBackup || p.OnSuccessRestore.Strategy == RestoreOriginal
}

// validateBackupPath 把备份名的硬要求写成校验：绝对路径、不含 ..、
// 且不能与目标路径相同（相同等于"用现场证明现场"，回滚时什么都恢复不了）。
func validateBackupPath(f FileSpec) error {
	backup := strings.TrimSpace(f.BackupPath)
	if backup == "" {
		return fmt.Errorf("%s：%s 声明了依赖备份的策略却未给定 BackupPath", CodeDescriptorInvalid, f.Path)
	}
	if !strings.HasPrefix(backup, "/") || strings.Contains(backup, "..") {
		return fmt.Errorf("%s：非法备份路径 %q", CodeDescriptorInvalid, f.BackupPath)
	}
	if backup == f.Path {
		return fmt.Errorf("%s：备份路径与目标路径相同，回滚无法恢复原件", CodeDescriptorInvalid)
	}
	return nil
}

// DesiredHash 计算期望态的稳定指纹，用于桥侧账本对齐判定。
//
// inline 字节不参与序列化（Normalize 已把摘要算进 SHA256），否则账本文件
// 会随配置原文膨胀，且同一份内容的两次下发可能因字节序不同而误判"有修改"。
func DesiredHash(d DesiredState) string {
	clone := d
	clone.Files = make([]FileSpec, len(d.Files))
	for i, f := range d.Files {
		f.Source.Data = nil
		clone.Files[i] = f
	}
	raw, err := json.Marshal(clone)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
