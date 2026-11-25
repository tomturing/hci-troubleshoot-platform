package delivery

// 交付作业的**节点阶段与传输**：等采集完成、包归属校验、源包摘要、拉取、
// 分片上传、完成回执、传输证据采样。
//
// 与 delivery_runner.go 的分工：那边只管队列、租约归属、阶段推进与错误分类，
// 这里只管"跟节点和平台搬字节"。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"terminal_bridge/internal/shared/acliout"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/logcontract"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/shared/uplink"
	"time"
)

const (
	// deliveryProgressCommandTimeout 是 `acli log_collect progress` 的单条上限。
	// acli 首次调用有进程启动与读状态文件的开销，给足 60 秒；超过即视为会话不可用。
	deliveryProgressCommandTimeout = 60 * time.Second
	// deliveryNodeProbeTimeout 是探测类命令（stat/ls/date/sha256sum）的单条上限。
	// 这些命令在正常节点上都是毫秒级，15 秒已经非常宽松，只用于把"永久挂起"变成"可重试失败"。
	deliveryNodeProbeTimeout = 15 * time.Second
	// deliveryNodeCommandFailureLimit 是"连续多少次节点命令失败"判定会话不可用的阈值。
	//
	// 为什么不是看字节增量：等待采集阶段节点本来就可以长时间没有新字节（多节点串行采集），
	// 用增量判定会误杀正常等待。而"命令一条都执行不了"是另一件事——它说明 SSH 会话已死
	// （TMOUT 静默登出、NAT 老化导致通道半开），继续在这个会话上轮询就会永久冻结。
	// 2026-11-24 Q2026112400942：P2 中继 149 分钟 0 字节、全程 fetching、无任何重连，
	// 正是"命令永久挂起 + 等待阶段不判停摆"叠加的结果。
	deliveryNodeCommandFailureLimit = 3
)

// waitCollect 只读 progress，直到 acli 报 done == total。
//
// 硬约束：**绝不 cancel、绝不发 create**——采集是节点侧的事，交付只负责搬运。
//
// ⚠️ 完成判据必须叠加"包归属"：`acli log_collect progress` 是**节点全局态、不带任务身份**
// （bridge_execution_logs 里 task_id 恒为空）。新任务 create 后它立刻返回上一次的
// `done=1/total=1`，只看 Done>=Total 就会在 4 秒内把上一次的完成当成本次完成，
// 然后去拉一个已被覆盖/不存在的包（2026-10-27 现场 48f890d5）。
// 归属判据：用节点自己的 now - mtime 算年龄，并把这把时间尺按测得偏移校正到
// bridge 时钟；节点内年龄与作业相对时间同时指向旧包才拒绝。
// 不能用未校正的节点 mtime 与 bridge 墙钟比较——现场节点时钟慢 6 分钟就会
// 把新包误杀（Q2026112400585，2026-11-24）。
func (r *deliveryRunner) waitCollect(ctx context.Context, session NodeSession, job *deliveryJob) error {
	deadline := time.Now().Add(deliveryCollectTimeout)
	interval := 10 * time.Second
	failTracker := newCommandFailTracker(deliveryNodeCommandFailureLimit)
	for {
		if time.Now().After(deadline) {
			return fmt.Errorf("等待采集完成超时（%s）", deliveryCollectTimeout)
		}
		output, _, err := session.RunCommandPty(ctx, "acli log_collect progress", deliveryProgressCommandTimeout)
		if err != nil {
			// 取消/掐断不是"节点不可用"：必须原样上交，否则会被误判成会话失效重排。
			if ctx.Err() != nil {
				return ctx.Err()
			}
			// no-session 是**确定的**会话失效，不是"节点正忙"：立即交回重排。
			// 累计到 3 次只会白等 3×60s，并把有限的重试预算耗在同一个僵尸会话上
			// （2026-11-25 Q2026112500592：每轮 3 次 × 60s，5 轮全部撞同一堵墙）。
			if isNodeSessionLost(err) {
				evidence.Emit("WARN", "delivery.node_unreachable", "节点会话已失效（no-session），立即交回重排以重建连接", "", job.CaseID, job.NodeIP, "", map[string]any{
					"job_id": job.JobID, "task_id": job.TaskID,
					"command": "acli log_collect progress", "fail_streak": 1,
					"timeout_sec": int64(deliveryProgressCommandTimeout / time.Second),
					"last_error":  err.Error(),
				})
				return &sessionLostError{cause: err}
			}
			// 命令失败本身也可能是"节点正在忙"，所以只累计；连续失败到阈值才判定会话已死。
			// 判据取"命令能不能执行"而不是"字节有没有涨"：等待采集阶段节点本就可以长时间
			// 没有新字节（多节点串行），用增量判定会误杀正常等待。
			streak, unreachable := failTracker.fail()
			if unreachable {
				evidence.Emit("WARN", "delivery.node_unreachable", "连续多条节点命令无响应，判定 SSH 会话已失效，本轮交回重排以重建连接", "", job.CaseID, job.NodeIP, "", map[string]any{
					"job_id": job.JobID, "task_id": job.TaskID,
					"command": "acli log_collect progress", "fail_streak": streak,
					"timeout_sec": int64(deliveryProgressCommandTimeout / time.Second),
					"last_error":  err.Error(),
				})
				return fmt.Errorf("节点命令连续 %d 次无响应（最后错误：%v）：会话可能已被节点空闲登出，本轮交回重排以重建连接", streak, err)
			}
		} else {
			failTracker.reset()
			progress := acliout.ParseProgress(output)
			if progress.ParseOK && progress.Total > 0 && progress.Done >= progress.Total {
				sourcePath := firstNonEmpty(strings.TrimSpace(job.SourcePath), strings.TrimSpace(progress.ArchivePath))
				if sourcePath == "" {
					// progress 到 100% 却没有包路径：不能就这么进入拉取。
					// 空路径会让后面每一步都失败（digest_unavailable、stat 失败、
					// 拉取命令 status 1），错误文案却指向"源包可能已被清理"。
					evidence.Emit("WARN", "delivery.archive_path_missing", "采集已完成但未取到压缩包路径，继续等待", "", job.CaseID, job.NodeIP, "", map[string]any{
						"job_id": job.JobID, "total": progress.Total,
					})
				} else {
					// 包归属判定：只用节点自己的时钟算年龄，避免节点时钟偏差被当成旧包。
					// 现场 Q2026112400585 的节点慢约 6 分钟，旧实现直接拿 mtime 与
					// bridge 墙钟比较，把本轮刚打好的包判成上一轮产物。
					if err := ensureArchiveNotStale(ctx, session, job, sourcePath); err != nil {
						return err
					}

					// done>=total 只代表 acli 的采集任务结束，zip 仍在收尾追加。
					// 必须等它真正写完再交付，否则拉取追的是一个移动目标：
					// 2026-11-04 Q2026110400883 提交 882701883 字节，拉取期间还在长到 892074444，
					// 最终本地副本与平台会话基线不符，每片都过不了校验。
					settledSize, settleErr := r.waitArchiveSettled(ctx, session, job, sourcePath)
					if settleErr != nil {
						return settleErr
					}

					r.mu.Lock()
					job.SourcePath = sourcePath
					if settledSize > 0 {
						job.SourceSize = settledSize
					}
					job.Phase = phasePulling
					r.mu.Unlock()
					_ = r.saveJob(job)
					evidence.Emit("INFO", "delivery.collect_finished", "节点采集已完成且压缩包已落定，开始交付", "", job.CaseID, job.NodeIP, "", map[string]any{
						"job_id": job.JobID, "total": progress.Total, "archive_path": sourcePath, "size": settledSize,
					})
					// 包已确认写完，此刻取基线最可靠。
					r.ensureSourceDigest(ctx, session, job)
					return nil
				}
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(interval):
		}
		// 自适应降频：采集通常十几分钟，10s→30s 减少对节点与桥的打扰。
		if interval < 30*time.Second {
			interval += 10 * time.Second
		}
	}
}

// errSessionLostText 是 sshx.ErrNoSession 的稳定文案。
//
// 交付域刻意不 import sshx 具体包（保持端口化边界，见 NodeSession 注释），
// 因此用文案契约识别会话失效。改动该文案必须同步 sshx.ErrNoSession。
const errSessionLostText = "no-session"

// isNodeSessionLost 判定错误是否表示"SSH 会话已关闭/不存在"。
func isNodeSessionLost(err error) bool {
	return err != nil && strings.Contains(err.Error(), errSessionLostText)
}

// sessionLostError 给"会话已失效"的失败带上规范码。
//
// 为什么不能只靠文案：平台归因依赖 error_message 匹配，文案一改码就丢；
// 带上 ErrorCode() 后，deliveryErrorCode 会直接把它写进 job.LastErrorCode，
// 落库与前端分支都有稳定锚点（与磁盘满/并发准入两条路径一致）。
type sessionLostError struct{ cause error }

func (e *sessionLostError) Error() string {
	return fmt.Sprintf("节点会话已失效（%v）：立即交回重排以重建连接", e.cause)
}

func (e *sessionLostError) Unwrap() error { return e.cause }

func (e *sessionLostError) ErrorCode() string { return logcontract.ErrDeliverySessionLost }

// 压缩包落定判定：两次采样间隔与最长等待。
const (
	// 太短会与 acli 的写缓冲撞车（大小看起来"没变"其实在攒），
	// 太长会让正常交付白白多等十几秒。
	deliveryArchiveSettleInterval = 3 * time.Second
	// 正常 zip 收尾是秒级；5 分钟还没写完基本是采集异常（进程卡死/盘满）。
	deliveryArchiveSettleTimeout = 5 * time.Minute
)

// statArchiveSize 取节点上压缩包的字节数；stat 不可用时退回两级探测。
func statArchiveSize(ctx context.Context, session NodeSession, path string) (int64, bool) {
	if strings.TrimSpace(path) == "" {
		return 0, false
	}
	// 探测预算必须自己给：上层 ctx 是整个作业的（最长 48 小时），
	// 一旦 SSH 通道半开，stat 会永远不返回，交付就此冻结且看不出任何异常。
	probeCtx, cancel := context.WithTimeout(ctx, deliveryNodeProbeTimeout)
	defer cancel()
	if out, _, err := session.RunQuickCommand(probeCtx, fmt.Sprintf("stat -c %%s %s", remote.ShellQuote(path))); err == nil {
		if size, parseErr := strconv.ParseInt(strings.TrimSpace(out), 10, 64); parseErr == nil && size > 0 {
			return size, true
		}
	}
	probed := remote.ProbeArchive(probeCtx, session, path)
	if probed.Exists != nil && *probed.Exists && probed.Size > 0 {
		return probed.Size, true
	}
	return 0, false
}

// waitArchiveSettled 等"压缩包真正写完"再允许拉取。
//
// 为什么 done>=total 不够：acli 的 progress 到 100% 只说明采集任务结束，
// zip 仍在追加尾部。2026-11-04 Q2026110400883：提交时 882701883 字节，
// 拉取前 889677835，拉取完 892074444——三次大小都不同，而平台会话按
// 提交时的大小校验，末片长度必然对不上，每片都会失败。
//
// 判据必须两条同时成立：
//  1. 连续两次 stat 大小相同（不再增长）；
//  2. 尾部存在 zip EOCD 标记（确实收尾）。
//
// EOCD 校验手段缺失（受限 shell 没有 tail/od/tr/grep）时降级为只看大小稳定，
// 与 prepareDirect 同一取舍：少一个观测手段不该阻断交付。
func (r *deliveryRunner) waitArchiveSettled(ctx context.Context, session NodeSession, job *deliveryJob, path string) (int64, error) {
	deadline := time.Now().Add(deliveryArchiveSettleTimeout)
	var last int64
	for {
		if time.Now().After(deadline) {
			return 0, newFatal(
				logcontract.ErrTransferSourceStillWriting,
				"节点压缩包在 %s 内仍未写完（最后一次大小为 %d 字节）：采集进程可能异常或节点磁盘已满；"+
					"请先确认节点采集状态，再决定是等待还是重新采集，不要在当前包上继续交付",
				deliveryArchiveSettleTimeout, last,
			)
		}
		size, ok := statArchiveSize(ctx, session, path)
		if ok && size == last {
			completeErr := verifyArchiveComplete(ctx, session, path)
			if completeErr == nil {
				return size, nil
			}
			if errors.Is(completeErr, errNodeCommandUnreachable) {
				// 会话层面已经不可用，继续在本轮里等只会白耗到 5 分钟超时并给出错误结论。
				return 0, completeErr
			}
			if errors.Is(completeErr, errArchiveCompleteUnverifiable) {
				evidence.Emit("WARN", "delivery.archive_eocd_unavailable", "节点缺少 zip 结束标记校验命令，仅按大小稳定判定包已写完", "", job.CaseID, job.NodeIP, "", map[string]any{
					"job_id": job.JobID, "source_path": path, "size": size, "reason": completeErr.Error(),
				})
				return size, nil
			}
			// EOCD 缺失＝还在写，继续观察。
		}
		if ok {
			last = size
		}
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-time.After(deliveryArchiveSettleInterval):
		}
	}
}

// prepareDirect 为"补交付"准备源包：不碰 progress，只量物理事实。
//
// 为什么要校验 zip 结束标记：acli 边采集边写 zip，"文件存在"不等于"包完整"。
// 只 stat 大小会把半截包传上去，用户下载时才发现打不开——那时已经白跑一趟
// 460MB~20GB 的传输。EOCD（PK\x05\x06）在 zip 写完时必然出现在尾部，
// zip64 同样保留该标记（只是字段填 0xFFFF/0xFFFFFFFF），因此判据对所有尺寸都成立。
//
// 大小探测必须与 progress/stat 动作共用 remote.ProbeArchive：Q2026102900165
// （2026-10-29）现场证明 aSV 受限 shell 没有 stat（exit 127），只写单条 stat 的实现
// 会把盘上 29.9 MB 的包报成"源包不可读"，让补交付在最后一步永远走不通。
func (r *deliveryRunner) prepareDirect(ctx context.Context, session NodeSession, job *deliveryJob) error {
	sourcePath := strings.TrimSpace(job.SourcePath)
	if sourcePath == "" {
		return fmt.Errorf("缺少源包路径，无法交付")
	}

	probed := remote.ProbeArchive(ctx, session, sourcePath)
	if probed.Exists == nil {
		// 探测不出来 ≠ 包没了：返回普通错误交给重试，不得写成 fatal。
		return fmt.Errorf(
			"无法确认源包状态（%s）：%s；若节点 shell 缺少 stat，需确认 ls -l 可用",
			sourcePath, firstNonEmpty(probed.Diag, "探测命令不可用"),
		)
	}
	if !*probed.Exists {
		return &deliveryFatalError{reason: fmt.Sprintf(
			"节点上已确认没有源包（%s）：%s；重试无意义，请重新采集后再交付",
			sourcePath, firstNonEmpty(probed.Diag, "命令已执行且明确报文件不存在"),
		)}
	}
	if probed.Size <= 0 {
		return fmt.Errorf(
			"源包大小无法确认（探测级 %s，诊断 %s，路径 %s）；零字节包无法分片上传",
			probed.Level, firstNonEmpty(probed.Diag, "无"), sourcePath,
		)
	}
	size := probed.Size

	if err := verifyArchiveComplete(ctx, session, sourcePath); err != nil {
		if !errors.Is(err, errArchiveCompleteUnverifiable) {
			// 包没写完就必须终态失败：补交付里重试同样会读到半截包，
			// 而且每轮都会重新校验一遍几十 GB 的文件尾。给 010 让前端
			// 明确告诉用户"等压缩结束再点"，而不是反复清盘/重点。
			return newFatal(logcontract.ErrTransferSourceStillWriting,
				"源包尚未写完：%v；请等待节点采集/压缩真正结束后再点「上传到平台」，不要连续重试",
				err.Error(),
			)
		}
		// 校验手段缺失不是包坏了：只能降级为"未校验"并留痕，不能因此阻断交付。
		evidence.Emit("WARN", "delivery.archive_eocd_unavailable", "节点缺少 zip 结束标记校验命令，跳过完整性预检", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "source_path": sourcePath, "reason": err.Error(),
		})
	}

	r.mu.Lock()
	submittedSize := job.SubmittedSize
	job.SourcePath = sourcePath
	// 以节点上的真实大小为准：前端 stat 与这里可能来自不同时刻，
	// 用旧值会把末片长度算错，导致最后一片永远校验不过。
	job.SourceSize = size
	job.Phase = phasePulling
	r.mu.Unlock()

	// 但"以真实大小为准"只对**本地拉取**成立：平台会话的分片方案仍是提交时的
	// total_size。两者一旦分叉，后面每一片都会与平台校验不符，而且会白拉一次
	// 几十 GB 的包。所以在拉取之前就判死。
	if err := sourceSizeGuard(submittedSize, size); err != nil {
		return err
	}
	_ = r.saveJob(job)

	evidence.Emit("INFO", "delivery.direct_ready", "补交付源包校验通过，开始拉取", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "source_path": sourcePath, "size": size,
	})
	// 摘要必须在拉取前取：拉完再取，节点上的包可能已被下一次采集同名覆盖，
	// 拿到的基线就不属于本次交付，后面每一跳的"校验通过"都会变成假证据。
	r.ensureSourceDigest(ctx, session, job)
	return nil
}

// ensureSourceDigest 取一次节点侧源包摘要，作为端到端完整性的唯一基线。
//
// 为什么失败不阻断：HCI 可能既没有 sha256sum 也没有 md5sum（精简系统），取不到
// 摘要不等于包坏了。此时降级为 unverified，并在 UI 上明示"未校验源包"，
// 让用户自己决定是否接受——比直接让 20 GiB 的采集白跑更合理。
func (r *deliveryRunner) ensureSourceDigest(ctx context.Context, session NodeSession, job *deliveryJob) {
	if session == nil || strings.TrimSpace(job.SourcePath) == "" {
		return
	}
	r.mu.Lock()
	already := job.SourceSHA256 != "" || job.SourceMD5 != ""
	r.mu.Unlock()
	if already {
		return
	}

	algo, digest, err := remote.ProbeSourceDigest(ctx, session, job.SourcePath)
	r.mu.Lock()
	if err != nil {
		job.Integrity = integrityUnverified
		job.IntegrityNote = "未取得源包摘要：" + err.Error()
		r.mu.Unlock()
		_ = r.saveJob(job)
		evidence.Emit("WARN", "delivery.digest_unavailable", "节点无法计算源包摘要，完整性校验降级", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "source_path": job.SourcePath, "error": err.Error(),
		})
		return
	}
	job.DigestSource = algo
	if algo == "sha256" {
		job.SourceSHA256 = digest
	} else {
		job.SourceMD5 = digest
	}
	// 这里只拿到"基线"，还不代表任何一跳校验通过；Integrity 留给 pull/complete 定论。
	r.mu.Unlock()
	_ = r.saveJob(job)
	evidence.Emit("INFO", "delivery.digest_probed", "已取得节点源包摘要", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "algorithm": algo, "digest": digest, "source_size": job.SourceSize,
	})
}

// errArchiveCompleteUnverifiable 表示节点缺少校验 zip 结束标记所需的命令。
// 它必须与"包不完整"严格区分：前者只是少了一个可观测手段，后者是数据真的坏了。
var errArchiveCompleteUnverifiable = errors.New("节点缺少 tail/od/tr/grep，无法校验 zip 结束标记")

// errNodeCommandUnreachable 表示节点命令根本没有返回（超时/通道半开）。
//
// 它必须与"包还在写"和"节点缺工具"都分开：
//   - 当成"包还在写"会白等满 5 分钟再报一个假原因；
//   - 当成"缺工具"会降级放行，可能把半截包传上去。
//
// 正确处置是交给上层重排，重建会话后重来。
var errNodeCommandUnreachable = errors.New("节点命令无响应（会话可能已被空闲登出）")

// verifyArchiveComplete 通过 zip 尾部的 EOCD 标记判断包是否写完。
// 只看尾部 64KiB：EOCD 在最末尾（含最大 65535 字节注释），无需读全文件。
//
// 失败必须分两类：命令不可用（126/127 或 stderr 报 not found）返回
// errArchiveCompleteUnverifiable，由调用方降级放行；只有命令真跑了且没找到标记，
// 才说明包还在写入中。混在一起会让没有 tail 的受限 shell 永远交付不上去。
func verifyArchiveComplete(ctx context.Context, session NodeSession, path string) error {
	command := fmt.Sprintf(
		`tail -c 65536 %s | od -An -tx1 | tr -d ' \n' | grep -c 504b0506`,
		remote.ShellQuote(path),
	)
	// 校验尾部是秒级操作，给它独立的短预算：这条命令一旦挂在半开会话上，
	// 整个交付就会停在"等包写完"的判定里，且等待阶段不参与停摆看门狗。
	probeCtx, cancel := context.WithTimeout(ctx, deliveryNodeProbeTimeout)
	defer cancel()
	stdout, stderr, exitCode, err := session.RunQuickCommandStreams(probeCtx, command)
	combined := stdout + "\n" + stderr
	// 注意：runQuickCommandStreams 在非零退出时也会返回 ssh.ExitError，
	// 因此不能用 err != nil 判“命令不可用”——grep 未命中（exit 1）同样是 err != nil，
	// 那是“包不完整”的正面证据，不能当成“没法校验”放行。
	if ctx.Err() != nil {
		// 超时/取消不是"节点缺工具"：绝不能降级放行，否则半截包会被当成完整包传上去。
		return fmt.Errorf("%w：%v", errNodeCommandUnreachable, ctx.Err())
	}
	if exitCode < 0 {
		return fmt.Errorf("%w（通道异常 %s）", errArchiveCompleteUnverifiable, errString(err))
	}
	if exitCode == 126 || exitCode == 127 || remote.ArchiveProbeToolMissingRe.MatchString(combined) {
		return fmt.Errorf("%w（exit=%d %s）", errArchiveCompleteUnverifiable, exitCode, truncateForDiag(combined, 120))
	}
	// grep -c 命中返回 0；未命中返回 1（此时 exitCode != 0）
	if strings.TrimSpace(stdout) == "0" || exitCode != 0 {
		return fmt.Errorf("压缩包不完整（尾部无 zip 结束标记），可能仍在采集写入中：%s", path)
	}
	return nil
}

// 包年龄判定的阈值。只用节点内相对时间，避免把用户侧时钟偏差当成旧包：
//   - 30 分钟：包在节点上的最小“陈旧”年龄；上一轮残留包通常已是几十分钟/几小时。
//   - 5 分钟：把节点 mtime 按测得偏移校正到 bridge 时钟后，允许早于交付作业创建时间的量；
//     采集通常与交付提交并发，正常只会早几秒。
//   - 2 分钟：mtime 晚于节点当前时间的容许量；超出视为时钟回拨/未来时间，按新鲜放行。
const (
	deliveryArchiveStaleAfter       = 30 * time.Minute
	deliveryArchiveJobSkewTolerance = 5 * time.Minute
	deliveryArchiveFutureTolerance  = 2 * time.Minute
)

// archiveAgeSample 是同一次节点命令读到的两个时间读数。
// 两者必须来自同一条命令/同一时钟源，否则节点时钟偏差会重新混进判定。
type archiveAgeSample struct {
	NodeNow time.Time
	MTime   time.Time
}

type archiveAgeVerdict string

const (
	archiveAgeFresh         archiveAgeVerdict = "fresh"
	archiveAgeStale         archiveAgeVerdict = "stale"
	archiveAgeClockRollback archiveAgeVerdict = "clock_rollback"
)

// inspectArchiveAge 在节点上一次性取当前时间与包 mtime。
//
// 为什么必须同一条命令：分两次 SSH 调用之间节点时钟可能跳变，且两次读数
// 若来自不同时钟源（一次节点、一次 bridge）就又回到“跨时钟比较”的老问题。
func inspectArchiveAge(ctx context.Context, session NodeSession, path string) (archiveAgeSample, error) {
	command := `printf '%s %s\n' "$(date +%s)" "$(stat -c %Y ` + remote.ShellQuote(path) + ` 2>/dev/null)"`
	probeCtx, cancel := context.WithTimeout(ctx, deliveryNodeProbeTimeout)
	defer cancel()
	output, _, err := session.RunQuickCommand(probeCtx, command)
	if err != nil {
		return archiveAgeSample{}, err
	}
	fields := strings.Fields(output)
	if len(fields) < 2 {
		return archiveAgeSample{}, fmt.Errorf("解析节点时间失败：%q", strings.TrimSpace(output))
	}
	nodeNowEpoch, nowErr := strconv.ParseInt(fields[0], 10, 64)
	mtimeEpoch, mtimeErr := strconv.ParseInt(fields[1], 10, 64)
	if nowErr != nil || mtimeErr != nil {
		return archiveAgeSample{}, fmt.Errorf("解析节点时间失败：%q", strings.TrimSpace(output))
	}
	return archiveAgeSample{
		NodeNow: time.Unix(nodeNowEpoch, 0),
		MTime:   time.Unix(mtimeEpoch, 0),
	}, nil
}

// classifyArchiveAge 是纯判定函数：先算节点内年龄，再把 mtime 按测得偏移校正到
// bridge 时钟，与交付作业创建时间比较。
//
// 为什么需要两个条件同时成立才判旧包：
//   - 只看节点内年龄，会把“排队很久但确实是本轮新产出的包”误杀；
//   - 只看 job 相对时间，容忍不了节点时钟跳变；
//   - 两者都指向旧包才拒绝，任何一侧显示“新”都放行，优先保证功能可用。
//
// age 为负表示 mtime 在未来；超出容许量标记为 clock_rollback 并放行。
func classifyArchiveAge(nodeNow, mtime, bridgeNow, jobStartedAt time.Time) (archiveAgeVerdict, time.Duration) {
	age := nodeNow.Sub(mtime)
	if mtime.After(nodeNow.Add(deliveryArchiveFutureTolerance)) {
		return archiveAgeClockRollback, age
	}
	// 用同一次读数测得的偏移把 mtime 校正到 bridge 时钟；静态时钟偏差在此抵消。
	mtimeOnBridge := mtime.Add(bridgeNow.Sub(nodeNow))
	olderThanJob := mtimeOnBridge.Before(jobStartedAt.Add(-deliveryArchiveJobSkewTolerance))
	if age > deliveryArchiveStaleAfter && olderThanJob {
		return archiveAgeStale, age
	}
	return archiveAgeFresh, age
}

// startedAt 解析交付作业创建时间；解析失败时跳过归属判定，宁可少判一次也不误杀。
func (j *deliveryJob) startedAt() (time.Time, bool) {
	raw := strings.TrimSpace(j.CreatedAt)
	if raw == "" {
		return time.Time{}, false
	}
	if t, err := time.Parse(time.RFC3339Nano, raw); err == nil {
		return t, true
	}
	if t, err := time.Parse(time.RFC3339, raw); err == nil {
		return t, true
	}
	return time.Time{}, false
}

// ensureArchiveNotStale 判断节点上的包是否属于本轮采集。
//
// 探测失败时跳过判定（宁可少判一次，也不把“读不到时间”当成旧包）；
// 只有节点内相对年龄超过阈值才返回 LC-TRANSFER-011，并以 fatal 结束，
// 避免对确定性的旧包做 5 次无意义重试。
func ensureArchiveNotStale(ctx context.Context, session NodeSession, job *deliveryJob, path string) error {
	startedAt, ok := job.startedAt()
	if !ok {
		evidence.Emit("WARN", "delivery.archive_age_probe", "交付作业缺少创建时间，跳过旧包判定", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "task_id": job.TaskID, "archive_path": path,
		})
		return nil
	}
	sample, err := inspectArchiveAge(ctx, session, path)
	if err != nil {
		evidence.Emit("WARN", "delivery.archive_age_probe", "无法读取节点包时间，跳过旧包判定", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "task_id": job.TaskID, "archive_path": path, "error": err.Error(),
		})
		return nil
	}
	bridgeNow := time.Now()
	verdict, age := classifyArchiveAge(sample.NodeNow, sample.MTime, bridgeNow, startedAt)
	level := "INFO"
	if verdict != archiveAgeFresh {
		level = "WARN"
	}
	evidence.Emit(level, "delivery.archive_age_probe", "按节点本地时间判定压缩包归属", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id":           job.JobID,
		"task_id":          job.TaskID,
		"archive_path":     path,
		"node_now":         sample.NodeNow.UTC().Format(time.RFC3339),
		"mtime":            sample.MTime.UTC().Format(time.RFC3339),
		"mtime_on_bridge":  sample.MTime.Add(bridgeNow.Sub(sample.NodeNow)).UTC().Format(time.RFC3339),
		"job_started_at":   startedAt.UTC().Format(time.RFC3339),
		"age_sec":          int64(age / time.Second),
		"threshold_sec":    int64(deliveryArchiveStaleAfter / time.Second),
		"clock_offset_sec": int64(bridgeNow.Sub(sample.NodeNow) / time.Second),
		"verdict":          string(verdict),
	})
	if verdict != archiveAgeStale {
		return nil
	}
	return newFatal(logcontract.ErrTransferSourceStale,
		"节点上的压缩包（%s）按节点本地时间生成于 %s 前，且校正节点时钟偏差后仍早于本次交付作业创建时间；"+
			"判定为上一轮采集的产物。请先确认节点时间是否准确；如需交付已采集完成的包，请使用「上传到平台」补交付；需要本轮最新内容请重新采集",
		path, formatArchiveAge(age))
}

// formatArchiveAge 把年龄转成客户可读的中文时长，避免文案里出现原始秒数。
func formatArchiveAge(age time.Duration) string {
	if age < time.Minute {
		return "不足 1 分钟"
	}
	minutes := int(age / time.Minute)
	if minutes < 60 {
		return fmt.Sprintf("%d 分钟", minutes)
	}
	hours := minutes / 60
	minutes %= 60
	if minutes == 0 {
		return fmt.Sprintf("%d 小时", hours)
	}
	return fmt.Sprintf("%d 小时 %d 分钟", hours, minutes)
}

// pull 把包从 HCI 拉到本地；断点依据是本地文件大小（tail -c +N）。
// pendingLocalBytes 汇总"同目录其它进行中作业还没落地的字节"。
//
// 只算同目录：不同卷的作业互不挤占，算进来会凭空放大需求。
// 不算终态作业：副本要么已删（交付成功），要么不再增长（失败终态）。
// SourceSize<=0 的作业无法估算，按 0 处理——宁可少算（放行），
// 也不要把"未知"当成"已占满"，误拦一次本来能完成的交付。
func (r *deliveryRunner) pendingLocalBytes(excludeJobID, dir string) int64 {
	if dir == "" {
		return 0
	}
	cleaned := filepath.Clean(dir)
	r.mu.Lock()
	defer r.mu.Unlock()
	var total int64
	for id, other := range r.jobs {
		if other == nil || id == excludeJobID || other.Phase.terminal() {
			continue
		}
		if other.SourceSize <= 0 || other.LocalPath == "" {
			continue
		}
		if filepath.Clean(filepath.Dir(other.LocalPath)) != cleaned {
			continue
		}
		if remaining := other.SourceSize - other.PulledBytes; remaining > 0 {
			total += remaining
		}
	}
	return total
}

func (r *deliveryRunner) pull(ctx context.Context, session NodeSession, job *deliveryJob) error {
	if strings.TrimSpace(job.SourcePath) == "" {
		return fmt.Errorf("缺少源包路径，无法拉取")
	}
	if job.LocalPath == "" {
		job.LocalPath = filepath.Join(os.TempDir(), "hci-log-collect", job.TaskID+".zip")
	}
	// 每次都重新量源包大小，而不是只在"未知"时量：
	// 前端 stat 与本阶段可能相隔很久（用户点了补交付又放了半小时），陈旧的大小
	// 会让末片长度算错，也会把"源包已被同名覆盖"误判成"网络中断"，然后无限重拉。
	if output, _, err := session.RunQuickCommand(ctx, fmt.Sprintf("stat -c %%s %s", remote.ShellQuote(job.SourcePath))); err == nil {
		if size, parseErr := strconv.ParseInt(strings.TrimSpace(output), 10, 64); parseErr == nil && size > 0 {
			r.mu.Lock()
			job.SourceSize = size
			r.mu.Unlock()
		}
	}
	// 拉取是唯一必须长期占用 SSH 的环节：12.8 GiB @ ~10 MB/s 需要 20 分钟以上，
	// 远超 HCI 交互式 shell 的 TMOUT=600。两条防线同时上：
	//   1) 周期性注入 NUL，让 shell 根本不会因空闲退出；
	//   2) 标记长任务，即使 shell 真的退了也不让连接被连坐销毁/被空闲租约回收。
	session.BeginLongTask()
	defer session.EndLongTask()
	stopHeartbeat := session.StartShellHeartbeat(job.CaseID)
	defer stopHeartbeat()

	// 空间预检：12.8 GiB 要完整落地再分片上传，写满一半才失败的话
	// 用户已经白等 20 分钟，而且报错会伪装成"拉取中断"。
	//
	// 判定式必须带余量（见 diskNeed）：写盘期间同盘还要写 job json、日志与
	// 平台响应体，只按"剩余字节"卡边界会刚好在最后一刻失败。
	if job.SourceSize > 0 {
		if dir := filepath.Dir(job.LocalPath); dir != "" {
			_ = os.MkdirAll(dir, 0o700)
			var existing int64
			if info, statErr := os.Stat(job.LocalPath); statErr == nil {
				existing = info.Size()
			}
			if need := job.SourceSize - existing; need > 0 {
				// 同盘还可能躺着别的交付作业：把它们的未落地字节叠加进来，
				// 否则两个 20 GiB 作业各自预检都"够"，一起写就把盘写满。
				reserved := r.pendingLocalBytes(job.JobID, dir)
				verdict := admissionCheck(job.LocalPath, job.SourceSize, existing, reserved)
				evidence.Emit("INFO", "delivery.disk_precheck", "交付前磁盘预检", "", job.CaseID, job.NodeIP, "", map[string]any{
					"job_id": job.JobID, "local_path": job.LocalPath, "dir": dir,
					"need_bytes": verdict.Need, "free_bytes": verdict.Free,
					"reserved_bytes": reserved, "probed": verdict.Probed, "ok": verdict.OK,
				})
				if !verdict.OK {
					if verdict.Concurrent {
						// 单任务本来能通过，是别的作业把空间占走了：
						// 让用户等，而不是让他去清一个清不出来的盘。
						return &deliveryAdmissionError{
							Need: verdict.Need - verdict.Reserved, Free: verdict.Free,
							Reserved: verdict.Reserved, LocalPath: job.LocalPath,
						}
					}
					return &deliveryDiskFullError{
						Written: existing, Need: verdict.Need, Free: verdict.Free, LocalPath: job.LocalPath,
					}
				}
			}
		}
	}

	// 拉取原语直接走 remote.Pull：阶段 2 A3 之前经 logcollect 的薄包装转一手，
	// 交付域为一个三元组拆包而反向依赖采集域。BlockSize 用 remote 默认值，
	// 与采集侧 logCollectPullBlock 同源。
	pullResult, err := remote.Pull(ctx, session, remote.PullSpec{
		RemotePath: job.SourcePath,
		LocalPath:  job.LocalPath,
		TotalSize:  job.SourceSize,
		BlockSize:  remote.DefaultPullBlock,
		OnProgress: func(done, total int64) {
			r.mu.Lock()
			job.PulledBytes = done
			if job.SourceSize <= 0 && total > 0 {
				job.SourceSize = total
			}
			r.mu.Unlock()
			_ = r.saveJob(job)
		},
	})
	written, localSum := pullResult.Written, pullResult.SHA256
	if err != nil {
		// 失败时也必须回填真实已写字节：进度上报按块步进（见 progressStep），
		// 88 MB 的包在 64 MiB 步进下从头到尾一次都不上报，库里 pulled_bytes 恒为 0，
		// 用户看到"已拉 9.3 MB"、作业记录却写着 0（2026-11-03 Q2026110300988）。
		r.mu.Lock()
		job.PulledBytes = written
		remaining := job.SourceSize - written
		r.mu.Unlock()
		_ = r.saveJob(job)

		if isLocalDiskFull(err) {
			// ENOSPC 之后重新探测一次：此刻的可用值才是用户真正需要看到的证据。
			// 探测失败留 -1（文案显示"未知"），不得伪造成 0。
			free := int64(-1)
			if dir := filepath.Dir(job.LocalPath); dir != "" {
				if probed, ok := probeDisk(dir); ok {
					free = probed
				}
			}
			return &deliveryDiskFullError{
				Written: written, Need: diskNeed(remaining),
				Free: free, LocalPath: job.LocalPath, Cause: err,
			}
		}

		wrapped := fmt.Errorf("拉取中断（已拉 %d 字节）: %w", written, err)
		// 拉取失败后立刻复量源包：与拉取前不同就说明它还在写（或被重建）。
		// 这条判据必须优先于 remote.ErrPullSizeMismatch 的"被覆盖/截断"归因——
		// 2026-11-04 Q2026110400883 现场，bridge 把"包还在长"报成
		// "源包可能已被清理"，于是按普通错误重试了 4 轮，每轮都在追一个移动的目标。
		if current, ok := statArchiveSize(ctx, session, job.SourcePath); ok && job.SourceSize > 0 && current != job.SourceSize {
			// 残包不能留：否则下次续传会把旧内容当"已拉部分"接着追加。
			r.discardLocalCopy(job, "source_still_writing")
			return newFatal(
				logcontract.ErrTransferSourceStillWriting,
				"源包在拉取期间仍在写入（拉取前 %d 字节，当前 %d 字节），本次交付作废："+
					"平台上传会话已按拉取前的大小建立，继续传只会每片都失败。"+
					"请等待采集/压缩真正结束、包大小不再变化后，重新点击「上传到平台」",
				job.SourceSize, current,
			)
		}
		if errors.Is(err, remote.ErrPullSizeMismatch) {
			// 字节数对不上且大小已不再变化：源包被整体覆盖或截断，重试没有意义。
			return newFatal(logcontract.ErrTransferSourceStillWriting, "%s；源包可能已被覆盖或截断，重试无意义", wrapped.Error())
		}
		return wrapped
	}

	r.mu.Lock()
	job.PulledBytes = written
	job.LocalSHA256 = localSum
	sourceSHA := job.SourceSHA256
	sourceMD5 := job.SourceMD5
	localPath := job.LocalPath
	expectedSize := job.SourceSize
	r.mu.Unlock()

	// ── 段① 完整性结论（比对逻辑抽成纯函数便于单测钉住）─────────────────
	localMD5 := ""
	if sourceSHA == "" && sourceMD5 != "" {
		localMD5, err = uplink.FileDigest(localPath, "md5")
		if err != nil {
			return fmt.Errorf("计算本地副本 MD5 失败: %w", err)
		}
	}
	if verifyErr := verifyPulledIntegrity(sourceSHA, sourceMD5, localSum, localMD5, written, expectedSize); verifyErr != nil {
		if isDeliveryFatalError(verifyErr) {
			// 残包不能留：下次续传会把它当"已拉部分"接着追加，永远修不好。
			r.discardLocalCopy(job, verifyErr.Error())
		}
		return verifyErr
	}

	r.mu.Lock()
	job.PulledBytes = written
	job.Phase = phaseUploading
	if sourceSHA != "" || sourceMD5 != "" {
		job.Integrity = integrityVerified
		job.IntegrityNote = ""
	} else {
		// 没有基线时不得宣称"已校验"：这只证明了单跳传输一致，证明不了上游。
		job.Integrity = integrityUnverified
		job.IntegrityNote = "节点未提供源包摘要，仅校验了传输一致性"
	}
	r.mu.Unlock()
	_ = r.saveJob(job)
	evidence.Emit("INFO", "delivery.pull_verified", "拉取完成并完成完整性比对", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "size": written, "sha256": localSum,
		"source_sha256": sourceSHA, "source_md5": sourceMD5, "integrity": job.Integrity,
	})
	return nil
}

// verifyPulledIntegrity 判定"拉下来的字节"是否就是节点上的那个包。
//
// 判据顺序刻意固定：先大小、再摘要。大小不等时摘要必然不等，先报大小可以给出
// 更可操作的提示（"包被截断/覆盖"），而不是一串 64 位 hex 让用户去猜。
//
// 返回的错误一律是 fatal：源包内容已经变了，重试只会重复拉一遍。
func verifyPulledIntegrity(sourceSHA, sourceMD5, localSHA, localMD5 string, written, expected int64) error {
	if expected > 0 && written != expected {
		return &deliveryFatalError{reason: fmt.Sprintf(
			"拉取字节数与源包不一致：期望 %d，实际 %d；节点上的包可能已被覆盖或截断，请重新采集后再交付",
			expected, written,
		)}
	}
	if sourceSHA != "" {
		if !strings.EqualFold(sourceSHA, localSHA) {
			return &deliveryFatalError{reason: fmt.Sprintf(
				"源包完整性校验失败：节点 SHA-256 %s，本地副本 %s；已丢弃本地残包，需重新采集后再交付",
				sourceSHA, localSHA,
			)}
		}
		return nil
	}
	if sourceMD5 != "" && !strings.EqualFold(sourceMD5, localMD5) {
		return &deliveryFatalError{reason: fmt.Sprintf(
			"源包完整性校验失败：节点 MD5 %s，本地副本 %s；已丢弃本地残包，需重新采集后再交付",
			sourceMD5, localMD5,
		)}
	}
	return nil
}

// discardLocalCopy 丢弃不可信的本地副本，并清空分片进度。
//
// 为什么必须连 UploadedParts 一起清：平台上的分片是按旧包内容算的哈希，
// 源包变了以后继续"跳过已传分片"会拼出前半旧包 + 后半新包的杂交产物；
// 那种包连大小都可能对，只有整包摘要能发现，代价是又白跑一轮。
func (r *deliveryRunner) discardLocalCopy(job *deliveryJob, reason string) {
	// 本地副本路径按 task_id 命名（os.TempDir()/hci-log-collect/<task>.zip），
	// 新旧两代作业指向同一个文件。被顶替的作业在这里删"自己的残包"，
	// 删掉的其实是新作业正在上传的那个包——下一片立刻以"本地副本不存在"失败。
	// 资格丧失时连进度都不该重置：那些字节现在是新作业的续传基线。
	if !r.ownerIntact(job) {
		evidence.Emit("WARN", "delivery.local_discard_skipped", "作业已不是当前执行者，不删共享本地副本", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "local_path": job.LocalPath, "reason": reason,
		})
		return
	}
	r.mu.Lock()
	path := job.LocalPath
	job.PulledBytes = 0
	job.UploadedParts = nil
	job.Integrity = integrityUnverified
	job.IntegrityNote = reason
	r.mu.Unlock()
	if path != "" {
		_ = os.Remove(path)
	}
	_ = r.saveJob(job)
	evidence.Emit("WARN", "delivery.local_discarded", "本地副本校验失败，已丢弃并重置分片进度", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "local_path": path, "reason": reason,
	})
}

// platformHTTPClient 构造访问平台的 HTTP 客户端。
//
// 为什么需要它：平台在客户内网普遍用自签证书（staging 实测 acli.sangfor.com.cn:4443），
// 客户机的系统根证书库不可能预置该 CA，默认 Transport 必然
// `x509: certificate signed by unknown authority`，且失败点在分片 1——用户看到的是
// "上传中继失败"，与真实原因（证书不受信）毫无关系。
//
// 为什么不做全局开关：放宽校验的范围必须与"用户这一次的明确授权"等长。
// 授权随 job 落盘（TLSInsecure），只影响本任务的 client；job 结束即失效，
// 用户撤销后新 job 自动回到严格校验，不存在"一次放宽永久放宽"。
func platformHTTPClient(job *deliveryJob, timeout time.Duration) *http.Client {
	return uplink.HttpClientFor(job != nil && job.TLSInsecure, timeout)
}

// upload 分片上传；并发与单片预算按实测速率收敛，已传分片自动跳过。
func (r *deliveryRunner) upload(ctx context.Context, job *deliveryJob) error {
	if job.UploadToken == "" || job.PlatformURL == "" {
		return &deliveryAuthError{reason: "缺少上传令牌或平台地址"}
	}
	if job.PartCount <= 0 || job.ChunkSize <= 0 {
		return fmt.Errorf("分片参数缺失（part_count=%d chunk_size=%d）", job.PartCount, job.ChunkSize)
	}
	info, err := os.Stat(job.LocalPath)
	if err != nil {
		return fmt.Errorf("本地副本不存在: %w", err)
	}
	totalSize := info.Size()
	// 兜底：本地副本与提交基线不一致时（拉取后才被替换、或恢复旧作业时基线缺失后补齐），
	// 同样不能按新大小切片——平台会话不会跟着变。
	if err := sourceSizeGuard(job.SubmittedSize, totalSize); err != nil {
		return err
	}

	uploaded := job.uploadedSet()
	// ceiling 是"资源允许的上限"（内存/CPU），pace 是"这条链路实际撑得住的节奏"。
	// 两者取小：资源够但链路差时，以链路为准。
	ceiling := chunkConcurrencyFor(r.concurrency, job.ChunkSize, availableMemoryMB())
	r.mu.Lock()
	pace := job.Pace.Resolve(ceiling)
	r.mu.Unlock()
	client := platformHTTPClient(job, pace.Timeout())

	var sinceSave int
	res := uplink.Push(ctx, uplink.PushSpec{
		BaseURL:    uplink.NormalizeBaseURL(job.PlatformURL),
		Prefix:     uplink.DirectUploadPrefix,
		TaskID:     job.TaskID,
		Token:      job.UploadToken,
		LocalPath:  job.LocalPath,
		ChunkSize:  job.ChunkSize,
		PartCount:  job.PartCount,
		TotalSize:  totalSize,
		Uploaded:   uploaded,
		Workers:    pace.Parts,
		PartBudget: pace.Timeout(),
		Client:     client,
		Fence:      func() error { return r.fence(ctx, job) },
		OnPart: func(number int, _ int64) {
			// job 是共享结构（status() 会并发读快照，save() 也会 marshal 它），
			// 所有对 job 的读写都必须在 r.mu 下完成：addUploadedPart 改 UploadedParts，
			// 而 store.save 会 marshal 整个 job（含 UploadedParts）。锁外 save 会与
			// 其它 worker 的 addUploadedPart 形成 data race（CI -race 已抓到）。
			r.mu.Lock()
			job.addUploadedPart(number)
			sinceSave++
			shouldSave := sinceSave >= deliverySaveEveryParts
			if shouldSave {
				sinceSave = 0
				_ = r.saveJobLocked(job)
			}
			r.mu.Unlock()
		},
		Classify: classifyPartPut,
	})

	r.adjustPace(job, pace, uplink.RoundSample{
		UploadedParts: res.DoneParts,
		UploadedBytes: res.DoneBytes,
		TimedOutParts: res.TimedOutParts,
		Elapsed:       res.Elapsed,
	}, ceiling)

	// 只有"本轮正常收口"才可以把阶段推到 completing：被取消/被顶替的作业
	// 一片都没传也会走到这里，无条件覆写会让前端先看到"收尾中"再变失败。
	if ctx.Err() == nil && res.Err == nil && r.ownerIntact(job) {
		r.mu.Lock()
		job.Phase = phaseCompleting
		r.mu.Unlock()
		_ = r.saveJob(job)
	}

	if res.Err != nil {
		// 自签证书不能靠"前端记得传 tls_insecure"来兜底：前端从未传过该字段，
		// 于是客户内网自签平台上的每一次交付都停在「已传 0/N 片」。
		// 与桥日志直传通道保持一致：命中证书不受信就自动放宽一次并留痕，
		// 鉴权不放松（上传令牌仍绑定 task_id）。
		if !job.TLSInsecure && uplink.IsUntrustedCertificateError(res.Err) {
			r.mu.Lock()
			job.TLSInsecure = true
			job.Phase = phaseUploading
			job.LastError = "平台证书不受信，已放宽校验并重新上传"
			r.mu.Unlock()
			_ = r.saveJob(job)
			evidence.Emit("WARN", "delivery.tls_relaxed", "平台证书不受信，交付通道已放宽证书校验", "", job.CaseID, job.NodeIP, "", map[string]any{
				"job_id": job.JobID, "endpoint": job.PlatformURL, "reason": "self-signed-certificate",
			})
			// 归为 transient：此刻不需要人，只需要下一轮用放宽后的 client 重传。
			return &deliveryTransientError{reason: "平台证书不受信，已放宽校验，将自动重试"}
		}
		return fmt.Errorf("上传中断（已传 %d/%d 片）: %w", len(job.UploadedParts), job.PartCount, res.Err)
	}
	return nil
}

// adjustPace 把本轮实测结果收敛成下一轮的节奏并留痕。
//
// 留痕是硬要求：节奏一变，现场看到的"每片多久、几路并发"就变了，事后只有事件日志
// 能把"为什么这一轮突然慢了/快了"解释清楚。事件里同时记前后值与本轮样本，
// 便于用一条 SQL 复现收敛轨迹。
func (r *deliveryRunner) adjustPace(job *deliveryJob, prev uplink.Pace, sample uplink.RoundSample, ceiling int) {
	next := uplink.NextPace(prev, sample, job.ChunkSize, ceiling)
	if next == prev {
		return
	}
	r.mu.Lock()
	job.Pace = next
	_ = r.saveJobLocked(job)
	r.mu.Unlock()
	evidence.Emit("WARN", "delivery.pace_adjusted", "按实测速率调整上传节奏", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id":                  job.JobID,
		"chunk_size":              job.ChunkSize,
		"part_count":              job.PartCount,
		"parts_before":            prev.Parts,
		"parts_after":             next.Parts,
		"part_timeout_before_sec": prev.PartTimeout,
		"part_timeout_after_sec":  next.PartTimeout,
		"reason":                  next.Reason,
		"round_uploaded_parts":    sample.UploadedParts,
		"round_uploaded_bytes":    sample.UploadedBytes,
		"round_timed_out_parts":   sample.TimedOutParts,
		"round_elapsed_ms":        sample.Elapsed.Milliseconds(),
	})
}

// classifyPartPut 把分片 PUT 的 (状态码, 传输错误) 映射成交付语义：
// 分片级 401/403 同样是"必须重新认证"，不能当成普通失败盲重试：
// 旧实现把它混在"平台返回 HTTP 401"里，5 次退避后判终态，
// 用户看到的是"分片 3 上传失败"而不是"令牌失效，请重新认证"。
func classifyPartPut(status int, transportErr error) error {
	if transportErr != nil {
		return wrapTransientTransport(transportErr)
	}
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		return &deliveryAuthError{reason: fmt.Sprintf("上传令牌失效（HTTP %d）", status)}
	}
	if status < 200 || status >= 300 {
		return platformStatusError("平台返回", status)
	}
	return nil
}

// buildCompletePayload 组装收尾请求体。
//
// 硬约束：**不要放空 sha256**。平台对该字段有 `^[0-9a-f]{64}$` 的格式约束，
// 空串会在请求体校验层直接 422——业务代码都进不去，现场只看到
// "平台收尾失败 HTTP 422"，而 15 个分片已经全部传完（2026-10-27：1.4 GB 白传）。
// "不声明"用字段缺省表达，而不是用空串表达。
//
// size 同理：0 表示尚未量到，缺省即"不校验"，避免刚恢复的作业被 ge=1 拦下。
// 抽成纯函数是为了让这条契约能被单测钉住（字段一多就有人顺手加回去）。
func buildCompletePayload(job *deliveryJob) map[string]any {
	payload := map[string]any{"parts": job.UploadedParts}
	if job.SourceSize > 0 {
		payload["size"] = job.SourceSize
	}
	// 声明整包摘要，让平台的合并结果有可比对的对象。
	// 优先节点基线（端到端），没有基线时退本地副本摘要（至少覆盖 ②→③）；
	// 两者都为空就保持字段缺省，绝不能传空串（会 422，见上）。
	if sum := strings.TrimSpace(job.SourceSHA256); sum != "" {
		payload["sha256"] = sum
	} else if sum := strings.TrimSpace(job.LocalSHA256); sum != "" {
		payload["sha256"] = sum
	}
	return payload
}

// complete 调平台数据面收尾（用上传令牌，不需要客户 Cookie），平台随后自动做代传。
func (r *deliveryRunner) complete(ctx context.Context, job *deliveryJob) error {
	// 收尾是**不可撤销**的对外动作：平台收到即合并分片、启动代传。
	// 被顶替的作业若还能收尾，就会用旧分片集把新会话的合并结果盖掉。
	// 这道闸门同时也是平台侧 epoch fencing 之外的第二道防线（老平台 epoch=0）。
	if err := r.fence(ctx, job); err != nil {
		return err
	}
	payload := buildCompletePayload(job)
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	target := fmt.Sprintf("%s%s/%s/complete", uplink.NormalizeBaseURL(job.PlatformURL), uplink.DirectUploadPrefix, job.TaskID)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, target, strings.NewReader(string(body)))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Upload-Token", job.UploadToken)

	client := platformHTTPClient(job, deliveryCompleteTimeout)
	response, err := client.Do(request)
	if err != nil {
		if !job.TLSInsecure && uplink.IsUntrustedCertificateError(err) {
			r.mu.Lock()
			job.TLSInsecure = true
			job.Phase = phaseCompleting
			r.mu.Unlock()
			_ = r.saveJob(job)
			evidence.Emit("WARN", "delivery.tls_relaxed", "平台证书不受信，收尾通道已放宽证书校验", "", job.CaseID, job.NodeIP, "", map[string]any{
				"job_id": job.JobID, "endpoint": job.PlatformURL, "reason": "self-signed-certificate",
			})
			return &deliveryTransientError{reason: "平台证书不受信，已放宽校验，将自动重试"}
		}
		return wrapTransientTransport(err)
	}
	defer response.Body.Close()
	ackBody, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return &deliveryAuthError{reason: fmt.Sprintf("上传令牌失效（HTTP %d）", response.StatusCode)}
	}
	if response.StatusCode == http.StatusBadRequest || response.StatusCode == http.StatusUnprocessableEntity {
		// 4xx 是业务校验失败（大小/摘要不一致、parts 不完整），重试同样失败；
		// 且每次重试都要在服务端重新合并一遍整包，代价远高于一次重拉。
		detail := strings.TrimSpace(string(ackBody))
		if len(detail) > 400 {
			detail = detail[:400]
		}
		return &deliveryFatalError{reason: fmt.Sprintf("平台收尾校验未通过（HTTP %d）：%s", response.StatusCode, detail)}
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return platformStatusError("平台收尾失败", response.StatusCode)
	}

	// 平台回执里的 integrity_state 才是最终结论：真正做过整包比对的是服务端，
	// bridge 侧只能证明"拉下来的字节"没问题。
	var ack struct {
		IntegrityState string `json:"integrity_state"`
		SHA256         string `json:"sha256"`
	}
	if jsonErr := json.Unmarshal(ackBody, &ack); jsonErr == nil && strings.TrimSpace(ack.IntegrityState) != "" {
		r.mu.Lock()
		job.Integrity = strings.TrimSpace(ack.IntegrityState)
		if job.Integrity == integrityVerified {
			job.IntegrityNote = ""
		}
		r.mu.Unlock()
		_ = r.saveJob(job)
		evidence.Emit("INFO", "delivery.integrity_confirmed", "平台确认交付完整性", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "integrity": ack.IntegrityState, "sha256": ack.SHA256,
		})
	}
	return nil
}

// transferStageFor 把作业阶段映射成传输证据里的 stage。
//
// 为什么不能只有 fetching/uploading 两种：等待采集与"上一轮失败后等重排"这两个阶段
// 里，节点与桥之间一个字节都没流动。报 fetching 会让面板显示
// 「上传中 0/5 片 · 传输已停滞」——把"还没开工/在等重建"说成"传不动"，
// 现场据此会去查带宽和磁盘，方向完全错位（2026-11-24 Q2026112400942）。
func transferStageFor(phase deliveryPhase, uploaded int64) string {
	switch {
	case phase == phaseUploading || uploaded > 0:
		return "uploading"
	case phase == phaseWaitingCollect || phase == "" || phase == phaseRetrying:
		return "waiting"
	default:
		return "fetching"
	}
}

// streamEvidenceMetrics 每 2 秒采样一次作业进度，速率用 EMA 平滑、ETA 用剩余字节估算。
// 没有增量不补零：stalled 由平台按 60 秒阈值判定并写进 conditions。
func (r *deliveryRunner) streamEvidenceMetrics(ctx context.Context, job *deliveryJob) {
	reporter := evidence.Transfer()
	if !reporter.Enabled(job.TaskID) {
		return
	}
	ticker := time.NewTicker(reporter.MetricInterval())
	defer ticker.Stop()
	started := time.Now()
	lastAt := started
	var lastBytes int64
	var seq int64
	var rate float64
	var stalledSince time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			r.mu.Lock()
			phase := job.Phase
			total := job.SourceSize
			done := job.PulledBytes
			uploaded := int64(len(job.UploadedParts)) * job.ChunkSize
			r.mu.Unlock()
			if phase == phaseUploading || uploaded > 0 {
				done += uploaded
			}
			if total > 0 && done > total {
				done = total
			}
			elapsed := now.Sub(lastAt).Seconds()
			delta := done - lastBytes
			instant := float64(delta) / elapsed
			if instant < 0 {
				instant = 0
			}
			// 没有新字节就必须把速率归零：EMA 会残留一个"看似还在传"的数值，
			// 让 ETA 在停摆时继续倒数，是比显示「—」更危险的假象。
			if delta <= 0 {
				rate = 0
			} else if rate == 0 {
				rate = instant
			} else {
				rate = rate*0.7 + instant*0.3
			}
			if done == lastBytes {
				if stalledSince.IsZero() {
					stalledSince = now
				}
			} else {
				stalledSince = time.Time{}
			}
			stage := transferStageFor(phase, uploaded)
			metric := evidence.TransferMetric{
				Seq: seq, Stage: stage, BytesDone: done, ElapsedMS: now.Sub(started).Milliseconds(),
				Stalled: !stalledSince.IsZero() && now.Sub(stalledSince) >= time.Duration(reporter.StallSeconds())*time.Second,
				Source:  "bridge",
			}
			seq++
			if total > 0 {
				percent := float64(done) / float64(total) * 100
				metric.BytesTotal = &total
				metric.Percent = &percent
			}
			if rate > 0 {
				rounded := int64(rate)
				metric.RateBps = &rounded
				if total > done {
					eta := int64(float64(total-done) / rate)
					metric.EtaSec = &eta
				}
			}
			// 投递超时与失败告警都由实现方负责（它才认识票据与链路字段）。
			reporter.ReportTransferMetric(ctx, job.TaskID, metric)
			lastAt, lastBytes = now, done
		}
	}
}
