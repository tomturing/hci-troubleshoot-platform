package remote

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"os"
	"path/filepath"
)

// transfer.go —— 大文件拉取原语（收口设计 §4.3）。
//
// 收口前桥里有两份"从节点拉压缩包"的实现（log_collect_transfer.pullArchive
// 与 delivery_runner.pull 各自的外壳），断点续传、边拉边摘要、字节数收敛
// 三件事只在前者里做对。本文件把这三件事收成唯一实现，两个调用方都委托它。
//
// 三条不可退让的语义：
//  1. **断点依据是本地文件已有大小**，用 `tail -c +offset+1` 从断点续读，
//     不依赖 SFTP，也不依赖服务器支持 range；
//  2. **边拉边算摘要**：20 GiB 拉完再读一遍磁盘要数十秒到数分钟，且这段
//     时间里文件仍可能被外部改写；流式摘要与写入吃同一段 buffer，才是
//     "校验的就是传过去的"；
//  3. **字节数在拉取层就收敛**：tail 被源包截断/被 TMOUT 打断都会提前 EOF，
//     不在这里拦住就会把短包当完整包继续上传。

// DefaultPullBlock 是单次读取的缓冲大小。
const DefaultPullBlock = 4 << 20

// ErrPullSizeMismatch 表示拉到的字节数与源包不一致。
//
// 必须是可 errors.Is 判定的哨兵：调用方要据此区分"包被清理了"与
// "本地磁盘满了"，两者的处置完全不同（前者重拉，后者先腾空间）。
var ErrPullSizeMismatch = errors.New("拉取字节数与源包不一致")

// PullSpec 描述一次拉取。
type PullSpec struct {
	RemotePath string
	LocalPath  string
	// TotalSize 是远端源包大小；<=0 表示未知（stat 失败），此时无法判定完成。
	TotalSize int64
	// BlockSize 为 0 时用 DefaultPullBlock。
	BlockSize int64
	// OnProgress 报告已落地字节；nil 表示不上报。
	OnProgress ProgressFunc
}

// PullResult 是一次拉取的事实。
type PullResult struct {
	Written int64
	SHA256  string
	// Offset 是本次开始前的本地已有字节数（续传起点）。
	Offset int64
	// Complete 为真表示本地已持有完整包，未发生任何网络读取。
	Complete bool
	// Discarded 为真表示本地残包被判定不可信并已删除。
	Discarded bool
}

// PullStart 描述一次下载的起点决策。
type PullStart struct {
	// Offset 是本次从节点读取的起始字节位置（本地已有字节数）。
	Offset int64
	// Discard 为真表示本地残包不可信，必须先删除再从头下载。
	Discard bool
	// Complete 为真表示本地已持有完整包，无需任何网络读取。
	Complete bool
}

// ResolvePullStart 依据两个物理事实决定续传起点：本地文件大小、远端源包大小。
//
// 三种边界各自的含义：
//   - 本地为空或小于源包：正常续传，从已有字节处接着读；
//   - 本地恰好等于源包：已传完，直接收工（重跑交付时能省掉整包重传）；
//   - 本地大于源包：**必定**不是本次的包——同一份数据不可能是自己的前缀，
//     只能是被同名覆盖/截断过的旧产物，追加进去会得到永远校验不过的坏包。
//
// remoteSize <= 0 表示远端大小未知（stat 失败）：此时无法判定完成，
// 也不该丢弃本地数据（可能是正常的半包），一律按续传处理。
func ResolvePullStart(localSize, remoteSize int64) PullStart {
	if remoteSize <= 0 {
		return PullStart{Offset: localSize}
	}
	if localSize > remoteSize {
		return PullStart{Offset: 0, Discard: true}
	}
	if localSize == remoteSize {
		return PullStart{Offset: localSize, Complete: true}
	}
	return PullStart{Offset: localSize}
}

// BuildPullCommand 构造从 offset 处续读的命令。
//
// 为什么 offset<=0 用 cat 而不是 tail -c +1：受限 shell 的命令白名单里确有
// cat、未必有 tail（同源证据：Q2026102900165 现场 stat 就是 exit 127，而
// ls/cat 可用）。首次交付必然从 0 开始，这条替换覆盖绝大多数路径，且不存在
// 语义差异——不必等现场因为没有 tail 再挂一次。
func BuildPullCommand(remotePath string, offset int64) string {
	if offset <= 0 {
		return fmt.Sprintf("cat %s", ShellQuote(remotePath))
	}
	// tail -c +N 的 N 为 1-based 起始字节位置。
	return fmt.Sprintf("tail -c +%d %s", offset+1, ShellQuote(remotePath))
}

// HashFilePrefixStrict 把 path 的前 n 字节喂给 hasher。
//
// 续传时本地已有 offset 字节，必须先把它们重新摘要一遍，否则最终哈希只覆盖
// 后半段——那比不提供哈希更糟：看起来"有校验"，实际永远对不上。
//
// 短读就是残包失真，必须报错（与 sftp 下载侧允许 .part 短读的口径不同）。
func HashFilePrefixStrict(path string, n int64, hasher hash.Hash) error {
	if n <= 0 {
		return nil
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	written, err := io.Copy(hasher, io.LimitReader(file, n))
	if err != nil {
		return err
	}
	if written != n {
		return fmt.Errorf("本地残包长度不足：期望 %d，实际 %d", n, written)
	}
	return nil
}

// ProgressStep 决定进度上报的字节步进。
//
// 固定 64 MiB 在小包上等于"全程不上报"：88 MB 的包从头到尾一次都不会触发，
// 失败时 job.PulledBytes 恒为 0，而错误文案里却写着"已拉 9.3 MB"
// （2026-11-03 Q2026110300988）。按总量的 1% 计算并夹在 [4 MiB, 64 MiB]：
// 小包有足够粒度，20 GiB 的包也不会产生上万次 WS 消息与 job 落盘。
func ProgressStep(total int64) int64 {
	const minStep int64 = 4 << 20
	const maxStep int64 = 64 << 20
	if total <= 0 {
		return maxStep
	}
	step := total / 100
	if step < minStep {
		return minStep
	}
	if step > maxStep {
		return maxStep
	}
	return step
}

// Pull 把节点上的文件续传到本地，同时返回整包 SHA-256。
//
// r 必须实现 StreamRunner；只实现 Runner 的执行器无法流式拉取，
// 这里显式报错而不是退化成"整包读进内存"。
func Pull(ctx context.Context, r Runner, spec PullSpec) (PullResult, error) {
	sr, ok := r.(StreamRunner)
	if !ok {
		return PullResult{}, errors.New("执行器不支持流式拉取（缺少 ReadStream）")
	}
	result := PullResult{}
	if err := os.MkdirAll(filepath.Dir(spec.LocalPath), 0o700); err != nil {
		return result, err
	}

	var localSize int64
	if info, err := os.Stat(spec.LocalPath); err == nil {
		localSize = info.Size()
	}
	start := ResolvePullStart(localSize, spec.TotalSize)
	result.Offset = start.Offset
	result.Discarded = start.Discard
	result.Complete = start.Complete
	if start.Discard {
		os.Remove(spec.LocalPath)
	}

	hasher := sha256.New()
	if start.Offset > 0 {
		if err := HashFilePrefixStrict(spec.LocalPath, start.Offset, hasher); err != nil {
			return result, fmt.Errorf("重建本地摘要失败: %w", err)
		}
	}
	report := spec.OnProgress
	if report == nil {
		report = func(int64, int64) {}
	}
	if start.Complete {
		report(start.Offset, spec.TotalSize)
		result.Written = start.Offset
		result.SHA256 = hex.EncodeToString(hasher.Sum(nil))
		return result, nil
	}
	offset := start.Offset

	stdout, wait, err := sr.ReadStream(ctx, BuildPullCommand(spec.RemotePath, offset))
	if err != nil {
		result.Written = offset
		return result, fmt.Errorf("启动下载失败: %w", err)
	}
	defer stdout.Close()

	file, err := os.OpenFile(spec.LocalPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		result.Written = offset
		return result, fmt.Errorf("打开本地文件失败: %w", err)
	}
	defer file.Close()

	// MultiWriter 保证 fwrite 与摘要吃的是同一段 buffer，不存在"文件写了、哈希没算"。
	writer := io.MultiWriter(file, hasher)
	written := offset
	lastReport := offset
	step := ProgressStep(spec.TotalSize)
	block := spec.BlockSize
	if block <= 0 {
		block = DefaultPullBlock
	}
	buffer := make([]byte, block)

	for {
		if ctx.Err() != nil {
			_ = stdout.Close()
			result.Written = written
			return result, ctx.Err()
		}
		n, readErr := stdout.Read(buffer)
		if n > 0 {
			if _, err := writer.Write(buffer[:n]); err != nil {
				_ = stdout.Close()
				result.Written = written
				return result, fmt.Errorf("写入本地文件失败: %w", err)
			}
			written += int64(n)
			if written-lastReport >= step {
				lastReport = written
				report(written, spec.TotalSize)
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			_ = stdout.Close()
			result.Written = written
			return result, fmt.Errorf("拉取中断: %w", readErr)
		}
	}
	result.Written = written

	if err := wait(); err != nil {
		return result, fmt.Errorf("拉取命令失败（源包可能已被清理）: %w", err)
	}
	if spec.TotalSize > 0 && written != spec.TotalSize {
		return result, fmt.Errorf("%w：期望 %d，实际 %d", ErrPullSizeMismatch, spec.TotalSize, written)
	}
	report(written, spec.TotalSize)
	result.SHA256 = hex.EncodeToString(hasher.Sum(nil))
	return result, nil
}
