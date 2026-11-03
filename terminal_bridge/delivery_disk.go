package main

// 客户机本地磁盘准入：交付（P2 中转）必须先整包落地再分片上传，
// 因此"能不能写"必须在拉取第一个字节之前回答，而不是等 write() 返回 ENOSPC。
//
// 第一性原理
// ----------
// 一次交付需要的空间只由两个不可再分的量决定：
//   1) 源包尚未落地的字节数（SourceSize - 本地已有）；
//   2) 同盘还要写的元数据（job json、日志、平台响应体）。
// 所以判定式是 free >= remaining + slack，slack 取 max(64 MiB, 5%)：
// 太小会在写满边缘失败，太大则误拦本来能完成的交付。
//
// 对抗性审查
// ----------
//   - 探测失败（Windows 上权限/UNC/异常路径）必须放行，不能把"未知"当"不足"；
//     放行后若真的写不进去，仍会由 isLocalDiskFull 归类成磁盘满，不会静默传坏包；
//   - 可用字节是瞬时值，只能作为准入闸门，不能当作配额承诺；
//   - 磁盘满属于"重试不会变好"的错误，必须与网络抖动严格分开，
//     否则 5 次秒级退避会在 30 秒内空转完，用户还没看到提示就已经终态失败。

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"syscall"
)

const (
	// deliveryDiskSlackMin 是预检的最小安全余量。
	//
	// 取 64 MiB：一次 job 落盘（json + tmp）与日志写入都在 KiB 量级，
	// 64 MiB 足以吸收这些并发写；再大就会在 88 MB 级小包上造成明显误拦。
	deliveryDiskSlackMin int64 = 64 << 20
	// deliveryDiskSlackDiv 是百分比余量：remaining/20 = 5%。
	//
	// 大包（GB 级）写盘期间系统其它组件也在增长，固定 64 MiB 不够。
	deliveryDiskSlackDiv int64 = 20
)

// errLocalDiskFull 是"本地盘写满"的哨兵错误，供 errors.Is 穿透 os.PathError。
var errLocalDiskFull = errors.New("客户机本地磁盘空间不足")

// diskFreeFunc 是 freeDiskBytes 的可注入包装。
//
// 为什么用变量而不是直接调用：单测必须能模拟"盘满"与"探测失败"，
// 而这两种状态在 CI 机器上无法真实制造。生产路径始终指向平台实现
// （windows: GetDiskFreeSpaceEx / unix: Statfs）。
var diskFreeFunc = freeDiskBytes

// probeDisk 返回 path 所在卷的可用字节数；第二个返回值表示"是否探测成功"。
//
// 为什么用 >0 判定：freeDiskBytes 的契约是"探测失败返回 0"。
// 真正的 0 字节可用也会被当作探测失败而放行——这是刻意的取舍：
// 放行后 write() 立刻失败，仍会被 isLocalDiskFull 归类，不会把半截包传上去。
func probeDisk(path string) (int64, bool) {
	free := diskFreeFunc(path)
	return free, free > 0
}

// diskNeed 计算本次拉取需要的可用空间（含安全余量）。
func diskNeed(remaining int64) int64 {
	if remaining <= 0 {
		return 0
	}
	slack := remaining / deliveryDiskSlackDiv
	if slack < deliveryDiskSlackMin {
		slack = deliveryDiskSlackMin
	}
	return remaining + slack
}

// diskPrecheck 是一次预检的结论，直接作为 delivery.disk_precheck 事件的载荷。
type diskPrecheck struct {
	// Need 是本次需要的可用空间（含余量）；0 表示无需拉取。
	Need int64
	// Free 是探测到的可用空间；探测失败时为 0。
	Free int64
	// Probed 表示探测是否成功；false 时 OK 恒为 true（放行）。
	Probed bool
	// OK 表示是否可以继续拉取。
	OK bool
}

// precheckDisk 判定"本地副本还差的空间是否拿得到"。
//
// 抽成纯函数（注入 diskFreeFunc）而不是埋在 pull 里：这是唯一能拦住
// "写 9 MB 才报 ENOSPC"的闸门，必须能被单测直接钉住边界。
func precheckDisk(localPath string, sourceSize, existing int64) diskPrecheck {
	remaining := sourceSize - existing
	if remaining <= 0 {
		return diskPrecheck{OK: true}
	}
	need := diskNeed(remaining)
	free, probed := probeDisk(filepath.Dir(localPath))
	if !probed {
		// 探测不可用 ≠ 空间不足：宁可放行，让 write 给出真实错误。
		return diskPrecheck{Need: need, Free: 0, Probed: false, OK: true}
	}
	return diskPrecheck{Need: need, Free: free, Probed: true, OK: free >= need}
}

// admissionVerdict 是并发准入的结论：在单任务预检之上叠加"同盘其它作业还要占的字节"。
type admissionVerdict struct {
	diskPrecheck
	// Reserved 是其它进行中作业预计还要写入的字节数。
	Reserved int64
	// Concurrent 表示失败只因叠加了其它作业（单任务本来能通过）。
	// 用于区分 008（清盘）与 009（等其它作业完成），两者处置动作不同。
	Concurrent bool
}

// admissionCheck 在单任务预检的基础上叠加 reserved 再做判定。
//
// 第一性原理：客户机的盘是共享资源，一次交付能不能写成，
// 取决于"这块盘上所有进行中交付的未落地字节之和"，而不是只看自己。
// 只做单任务预检时，两个 20 GiB 作业各自都"够"，一起写就满了——
// 那时失败点已经推后到 write() 的 ENOSPC，用户又回到"拉了 20 分钟才失败"。
func admissionCheck(localPath string, sourceSize, existing, reserved int64) admissionVerdict {
	base := precheckDisk(localPath, sourceSize, existing)
	verdict := admissionVerdict{diskPrecheck: base, Reserved: reserved}
	if reserved <= 0 || !base.Probed || !base.OK {
		// 单任务就不够（或探测不可用）时，结论与单任务预检一致，
		// 不能把"本来就没空间"错报成"并发占满"。
		return verdict
	}
	need := base.Need + reserved
	verdict.diskPrecheck = diskPrecheck{Need: need, Free: base.Free, Probed: true, OK: base.Free >= need}
	verdict.Concurrent = !verdict.OK
	return verdict
}

// isLocalDiskFull 识别"本地盘写满"。
//
// 为什么不能只比字符串：Windows 返回 *os.PathError 包着 syscall.Errno(112)，
// Go 版本升级或系统本地化都可能改变文案；errno 才是稳定事实。
// 字符串只作为最后兜底：跨平台封装库常把 errno 吃掉，只留下 message。
func isLocalDiskFull(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, errLocalDiskFull) {
		return true
	}
	var errno syscall.Errno
	if errors.As(err, &errno) {
		switch uintptr(errno) {
		case 28: // ENOSPC (unix)
			return true
		case 39: // ERROR_HANDLE_DISK_FULL (windows)
			return true
		case 112: // ERROR_DISK_FULL (windows)
			return true
		}
	}
	lowered := strings.ToLower(err.Error())
	return strings.Contains(lowered, "not enough space") ||
		strings.Contains(lowered, "no space left on device") ||
		strings.Contains(lowered, "disk is full")
}

// deliveryDiskFullError 表示客户机本地盘写满。
//
// 为什么单独一个类型而不是复用 deliveryFatalError：
//   - 它需要携带 need/free/path 三件事给前端做可操作提示；
//   - 它必须让重试预算立刻收敛到 1 次（重试不会变好，且每次都白写几个 MB）；
//   - 后续若要做"等空间再续"（PR-3），也只在磁盘满这一条分支上做。
type deliveryDiskFullError struct {
	// Written 是本次已成功写入的字节数（用于续传与用户判断进度）。
	Written int64
	// Need 是本次预检/失败时刻估算的所需空间（含余量）；0 表示未知。
	Need int64
	// Free 是失败时刻探测到的可用空间；-1 表示探测不可用。
	Free int64
	// LocalPath 是本地副本路径，用户需要据此清理。
	LocalPath string
	Cause     error
}

func (e *deliveryDiskFullError) Error() string {
	free := "未知"
	if e.Free >= 0 {
		free = formatBytesForLog(e.Free)
	}
	need := "未知"
	if e.Need > 0 {
		need = formatBytesForLog(e.Need)
	}
	return fmt.Sprintf(
		"客户机磁盘空间不足：本地副本还需 %s，目标目录当前可用 %s（%s），已写入 %s；"+
			"请清理该目录后点「上传到平台」重试，无需重新采集",
		need, free, e.LocalPath, formatBytesForLog(e.Written),
	)
}

// Unwrap 让 errors.Is/As 能穿透到原始 ENOSPC，便于上层统一识别。
func (e *deliveryDiskFullError) Unwrap() error { return e.Cause }

// isDeliveryDiskFull 用 errors.As 穿透包装（与 auth/fatal 的判定方式保持一致）。
func isDeliveryDiskFull(err error) bool {
	var target *deliveryDiskFullError
	return errors.As(err, &target)
}

// deliveryAdmissionError 表示并发准入失败：本地空间被其它交付作业占着。
//
// 与 deliveryDiskFullError 分开的理由：两者对用户的处置动作完全不同——
// 008 要用户清目录，009 要用户等其它作业完成。合并成一个类型会让前端
// 只能回显一句"空间不足"，用户清完盘再传仍然失败。
type deliveryAdmissionError struct {
	// Need 是本次作业需要的空间（含余量）。
	Need int64
	// Free 是探测到的可用空间。
	Free int64
	// Reserved 是其它进行中作业预计还要写入的字节数。
	Reserved int64
	// LocalPath 是本地副本路径，便于用户与运维定位目录。
	LocalPath string
}

func (e *deliveryAdmissionError) Error() string {
	return fmt.Sprintf(
		"客户机交付并发容量不足：还有其它交付任务未完成，预计还需占用 %s；"+
			"本次还需 %s，目标目录当前可用 %s（%s）；"+
			"请等待其它任务完成后再点「上传到平台」重试，无需重新采集",
		formatBytesForLog(e.Reserved), formatBytesForLog(e.Need),
		formatBytesForLog(e.Free), e.LocalPath,
	)
}

// isDeliveryAdmission 用 errors.As 穿透包装。
func isDeliveryAdmission(err error) bool {
	var target *deliveryAdmissionError
	return errors.As(err, &target)
}

// deliveryErrorCode 把交付错误映射成平台能落库、前端能分支的错误码。
//
// 只有磁盘满与并发准入需要专属码：其余失败（网络/平台/凭据）已有各自的
// 语义与文案，强行给它们编新码只会让码表膨胀而无人使用。
func deliveryErrorCode(err error) string {
	if err == nil {
		return ""
	}
	if isDeliveryDiskFull(err) {
		return ErrTransferLocalDiskFull
	}
	if isDeliveryAdmission(err) {
		return ErrTransferAdmissionExceeded
	}
	return ""
}
