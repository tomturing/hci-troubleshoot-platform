package remote

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// 远端写入协议（收口设计 §4.1）：
//
//	cat > ${tmp} && ${verify_cmd} && mv -f ${tmp} ${dest} && chmod ${mode} ${dest}
//	失败时：rm -f ${tmp}
//
// 退出码语义沿用 acli 写路径的既有协议（不是 delivery 的）：
//
//	20 = cat 失败（stdin 中断）
//	21 = 大小不一致
//	22 = 摘要不一致
//	23 = mv 失败
//
// 三条结构性要求，缺一不可：
//  1. 先校验再 mv——校验失败只删 tmp，正式路径上永远只会是完整包；
//  2. 以退出码表达结论——任何分支都不允许"静默成功"；
//  3. 回显 size/algo/digest——把远端事实写进日志，事后可复核。
//
// 校验降级必须显式上抛：sha256sum 缺失 → md5sum → size_only，
// WriteResult.Integrity 标注实际档位。静默降级等于把"没校验"伪装成"校验通过"。

const (
	// DefaultProgressStep 是上传进度上报粒度：过小刷屏，过大则长链路没有中间态。
	DefaultProgressStep = 2 << 20

	// 设备标记（shell 回显行）。acli 写路径的标记形态被 golden 逐字锁定，
	// 过渡期不改字节；新链路（config/spc/plugin）用 marker.go 的 SYNC_* 协议。
	WriteMark     = "__ACLI_WRITE "
	WriteOKMark   = "__ACLI_WRITE_OK"
	WriteFailMark = "__ACLI_WRITE_FAIL "
)

// WriteSpec 描述"把哪些字节写到节点上的哪个路径"。
//
// 为什么不是 reconcile.FileSpec：remote 禁止 import reconcile（§七），
// 期望值语义（Source 引用、Policy、Version）留在上层，这里只收"已解引用
// 的字节 + 期望摘要"。reconcile 负责把 FileSpec 降级/解引用成 WriteSpec。
type WriteSpec struct {
	Path    string // 目标路径
	TmpPath string // 临时路径；为空时按 Path + ".tmp." + UnixNano 生成
	Data    []byte // 要写入的字节（大文件由上层走 transfer 原语，不进这里）
	Size    int64  // 期望字节数
	SHA256  string // 期望 SHA-256（hex）
	MD5     string // 期望 MD5（hex），远端无 sha256sum 时的降级基线
	Mode    string // 权限位，空则 0755
	// OnProgress 报告已写入字节；nil 表示不上报。
	OnProgress ProgressFunc
	// ProgressStep 为 0 时用 DefaultProgressStep。
	ProgressStep int64
}

// WriteResult 是远端脚本回显的结构化事实。
type WriteResult struct {
	RemoteSize int64
	Algo       string
	Digest     string
	Moved      bool
	FailReason string
	Raw        string
	// Integrity 是校验强度自证：sha256 / md5 / size_only。
	Integrity string
	// TmpPath 是本次实际使用的临时路径，供失败清理与日志复核。
	TmpPath string
	// ExitCode 是远端脚本的退出码（20/21/22/23 见写入协议）。
	// 留它是为了"失败到底卡在哪一步"可查：只给 Reason 不给码，
	// 调用方就无法把现场与协议对上号。
	ExitCode int
}

// WriteError 表达写入/校验失败的结构化原因。
//
// 不带业务错误码（LC-*）：那是 main 包的 UI 契约，原语只给机器可判的 Reason，
// 由调用方映射成自己的码与文案。这样 remote 才能被反向链路复用。
type WriteError struct {
	Reason       string // size_mismatch | digest_mismatch | cat_failed | move_failed | remote_write_incomplete | size_after_move | sha256_after_move | md5_after_move | channel_broken | timeout
	Stage        string // upload | verify
	Retryable    bool
	LocalSize    int64
	RemoteSize   int64
	Algo         string
	LocalDigest  string
	RemoteDigest string
	Err          error
}

func (e *WriteError) Error() string {
	if e.Err != nil {
		return e.Reason + ": " + e.Err.Error()
	}
	return e.Reason
}

func (e *WriteError) Unwrap() error { return e.Err }

// BuildWriteCommand 构造"写入 → 校验 → 原子替换"的单条远端命令。
//
// digest 期望值只允许来自本地校验过的 hex，天然不含 shell 元字符；
// 路径一律经 ShellQuote，含单引号的路径不构成注入面。
func BuildWriteCommand(spec WriteSpec) string {
	return buildVerifiedMove("cat > "+ShellQuote(spec.TmpPath), spec.TmpPath, spec.Path,
		spec.Size, spec.SHA256, spec.MD5, spec.Mode)
}

// BuildArgvWriteCommand 构造"内容随命令走 argv → 校验 → 原子替换"的远端命令。
//
// 与 BuildWriteCommand 的唯一差异是填充方式：printf 把字节写进 tmp，而不是 cat 收 stdin。
// 存在的理由是传输形态：spc 扇出的脚本还要再经一跳 ssh 送到目标节点
// （ssh root@node '<script>'），那一跳没有可用的 stdin 通道，cat 只会读到 EOF
// 并写出空文件。校验/替换骨架仍是同一份 buildVerifiedMove，不允许另起一套。
//
// 代价必须写明：内容进 argv，因此调用方要保证 len(Data) 远小于 MAX_ARG_STRLEN
// （Linux 单参数 128KB）。超限场景一律走 BuildWriteCommand + WriteStdin。
// TmpPath 为空时用 Path+".tmp"：确定性命名让崩溃残留的 tmp 能被下一次写前自愈清掉。
func BuildArgvWriteCommand(spec WriteSpec) string {
	tmpPath := spec.TmpPath
	if tmpPath == "" {
		tmpPath = spec.Path + ".tmp"
	}
	fill := "printf '%s' " + ShellQuote(string(spec.Data)) + " > " + ShellQuote(tmpPath)
	return buildVerifiedMove(fill, tmpPath, spec.Path, spec.Size, spec.SHA256, spec.MD5, spec.Mode)
}

// CopySpec 描述"把节点上已有的文件搬到目标路径"。
//
// 与 WriteSpec 共用同一套校验/替换协议：备份还原、槽位搬迁同样是写入，
// 不能因为"字节没经过 SSH"就免掉先校验再 mv——cp 到一半断链同样留半截文件。
type CopySpec struct {
	SourcePath string
	Path       string
	TmpPath    string
	Size       int64
	SHA256     string
	MD5        string
	Mode       string
}

// BuildCopyCommand 构造"节点内复制 → 校验 → 原子替换"命令。
func BuildCopyCommand(spec CopySpec) string {
	return buildVerifiedMove("cp -p "+ShellQuote(spec.SourcePath)+" "+ShellQuote(spec.TmpPath),
		spec.TmpPath, spec.Path, spec.Size, spec.SHA256, spec.MD5, spec.Mode)
}

// buildVerifiedMove 是写入/复制共用的"填充 → 校验 → 替换"骨架。
//
// fill 是唯一的差异点（cat 收 stdin / cp 收节点内路径），其余逐字相同：
// 两套校验协议各自演化正是收口要消灭的东西。
func buildVerifiedMove(fill, tmpPath, destPath string, size int64, sha256Hex, md5Hex, mode string) string {
	quotedTmp := ShellQuote(tmpPath)
	quotedDest := ShellQuote(destPath)
	if mode == "" {
		mode = "0755"
	}
	return "umask 077; " +
		"if " + fill + "; then " +
		`SIZE=$(wc -c < ` + quotedTmp + ` 2>/dev/null | tr -d ' \r\n'); ` +
		`ALGO=size_only; DIG=""; ` +
		`if command -v sha256sum >/dev/null 2>&1; then ALGO=sha256; DIG=$(sha256sum ` + quotedTmp + ` 2>/dev/null | awk '{print $1}'); ` +
		`elif command -v md5sum >/dev/null 2>&1; then ALGO=md5; DIG=$(md5sum ` + quotedTmp + ` 2>/dev/null | awk '{print $1}'); fi; ` +
		`echo "` + WriteMark + `size=${SIZE} algo=${ALGO} digest=${DIG}"; ` +
		`if [ "$SIZE" != "` + strconv.FormatInt(size, 10) + `" ]; then echo "` + WriteFailMark + `size_mismatch"; rm -f ` + quotedTmp + `; exit 21; fi; ` +
		`if [ "$ALGO" = "sha256" ] && [ "$DIG" != "` + sha256Hex + `" ]; then echo "` + WriteFailMark + `digest_mismatch"; rm -f ` + quotedTmp + `; exit 22; fi; ` +
		`if [ "$ALGO" = "md5" ] && [ "$DIG" != "` + md5Hex + `" ]; then echo "` + WriteFailMark + `digest_mismatch"; rm -f ` + quotedTmp + `; exit 22; fi; ` +
		`if mv -f ` + quotedTmp + ` ` + quotedDest + ` && chmod ` + mode + ` ` + quotedDest + `; then echo "` + WriteOKMark + `"; ` +
		`else echo "` + WriteFailMark + `move_failed"; rm -f ` + quotedTmp + `; exit 23; fi; ` +
		`else echo "` + WriteFailMark + `cat_failed"; rm -f ` + quotedTmp + `; exit 20; fi`
}

// writeLineRe 解析远端校验回显。
//
// algo 字符集必须含数字——sha256/md5 本身带数字，早期写成 [a-z_] 会让正则
// 永远不匹配，"校验已通过"就变成不可判定的空结果（且看起来不报错）。
var writeLineRe = regexp.MustCompile(`size=([0-9]+)[ \t]+algo=([a-z0-9_]+)[ \t]+digest=([0-9a-zA-Z]*)`)

var ansiRe = regexp.MustCompile("\u001b\\[[0-9;]*m")

// StripANSI 去掉终端转义序列：保留可读文本，避免解析与展示被颜色码污染。
func StripANSI(text string) string {
	if text == "" {
		return text
	}
	return ansiRe.ReplaceAllString(text, "")
}

// ParseWriteOutput 从远端输出里提取校验回显。
//
// 取最后一条有效行：登录 banner、sudo 提示等噪声都可能出现在前面，
// 而脚本自身只会输出一条。
func ParseWriteOutput(output string) WriteResult {
	result := WriteResult{Raw: strings.TrimSpace(output)}
	lines := strings.Split(StripANSI(output), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if strings.Contains(line, WriteFailMark) {
			result.FailReason = strings.TrimSpace(strings.TrimPrefix(line[strings.Index(line, WriteFailMark):], WriteFailMark))
		}
		if strings.Contains(line, WriteOKMark) {
			result.Moved = true
		}
		if match := writeLineRe.FindStringSubmatch(line); match != nil {
			if result.RemoteSize == 0 {
				size, _ := strconv.ParseInt(match[1], 10, 64)
				result.RemoteSize = size
				result.Algo = match[2]
				result.Digest = strings.ToLower(match[3])
			}
		}
	}
	return result
}

// VerifyWrite 比对"本地事实"与"远端事实"，返回校验档位。
//
// 失败一律返回 *WriteError（Reason 可判），成功返回 sha256/md5/size_only。
// size_only 不阻断安装：装不上会由后续安装/版本校验兜住，但档位必须透传。
func VerifyWrite(spec WriteSpec, r WriteResult) (string, error) {
	if !r.Moved {
		reason := r.FailReason
		if reason == "" {
			reason = "remote_write_incomplete"
		}
		switch reason {
		case "size_mismatch":
			return "", &WriteError{Reason: reason, Stage: "verify", Retryable: true,
				LocalSize: spec.Size, RemoteSize: r.RemoteSize}
		case "digest_mismatch":
			return "", &WriteError{Reason: reason, Stage: "verify", Retryable: true,
				LocalSize: spec.Size, RemoteSize: r.RemoteSize, Algo: r.Algo,
				LocalDigest: spec.SHA256, RemoteDigest: r.Digest}
		case "cat_failed", "move_failed":
			return "", &WriteError{Reason: reason, Stage: "upload", Retryable: true}
		default:
			return "", &WriteError{Reason: "remote_write_incomplete", Stage: "upload", Retryable: true}
		}
	}
	if r.RemoteSize != spec.Size {
		return "", &WriteError{Reason: "size_after_move", Stage: "verify", Retryable: true,
			LocalSize: spec.Size, RemoteSize: r.RemoteSize}
	}
	switch r.Algo {
	case "sha256":
		if !strings.EqualFold(r.Digest, spec.SHA256) {
			return "", &WriteError{Reason: "sha256_after_move", Stage: "verify", Retryable: true,
				LocalSize: spec.Size, RemoteSize: r.RemoteSize, Algo: r.Algo,
				LocalDigest: spec.SHA256, RemoteDigest: r.Digest}
		}
		return "sha256", nil
	case "md5":
		if !strings.EqualFold(r.Digest, spec.MD5) {
			return "", &WriteError{Reason: "md5_after_move", Stage: "verify", Retryable: true,
				LocalSize: spec.Size, RemoteSize: r.RemoteSize, Algo: r.Algo,
				LocalDigest: spec.MD5, RemoteDigest: r.Digest}
		}
		return "md5", nil
	default:
		return "size_only", nil
	}
}

// WriteFile 把字节写入远端节点，并在 mv 之前完成落盘校验。
//
// 超时由调用方 ctx 控制（下载/上传/安装三段预算各自独立，合成一个总预算
// 会让任何一段变慢都表现成"另一段超时"）。ctx 取消时主动清理 tmp，
// 不留残包——否则下次续传会把半包当成"已传部分"。
func WriteFile(ctx context.Context, r Runner, spec WriteSpec) (WriteResult, error) {
	if spec.TmpPath == "" {
		spec.TmpPath = fmt.Sprintf("%s.tmp.%d", spec.Path, time.Now().UnixNano())
	}
	command := BuildWriteCommand(spec)
	reader := NewProgressReader(spec.Data, spec.ProgressStep, spec.OnProgress)

	stdout, code, err := r.WriteStdin(ctx, command, reader)
	result := ParseWriteOutput(stdout)
	result.TmpPath = spec.TmpPath
	result.ExitCode = code

	if ctxErr := ctx.Err(); ctxErr != nil {
		CleanupTmp(context.Background(), r, spec.TmpPath)
		return result, &WriteError{Reason: "timeout", Stage: "upload", Retryable: true, Err: ctxErr}
	}
	if err != nil {
		// 脚本以约定的非零码终结：结论已在 stdout 里，交给 VerifyWrite 判定。
		// code>0 是远端退出码；code<0 表示通道异常（没有退出码可言）。
		if code > 0 && result.FailReason != "" {
			return result, nil
		}
		return result, &WriteError{Reason: "channel_broken", Stage: "upload", Retryable: true, Err: err}
	}
	integrity, verifyErr := VerifyWrite(spec, result)
	result.Integrity = integrity
	return result, verifyErr
}

// CopyFile 在节点内把已有文件搬到目标路径，同样先校验再 mv。
func CopyFile(ctx context.Context, r Runner, spec CopySpec) (WriteResult, error) {
	if spec.TmpPath == "" {
		spec.TmpPath = fmt.Sprintf("%s.tmp.%d", spec.Path, time.Now().UnixNano())
	}
	stdout, code, err := r.RunQuickCommand(ctx, BuildCopyCommand(spec))
	result := ParseWriteOutput(stdout)
	result.TmpPath = spec.TmpPath
	result.ExitCode = code
	if ctxErr := ctx.Err(); ctxErr != nil {
		CleanupTmp(context.Background(), r, spec.TmpPath)
		return result, &WriteError{Reason: "timeout", Stage: "upload", Retryable: true, Err: ctxErr}
	}
	if err != nil {
		if code > 0 && result.FailReason != "" {
			return result, nil
		}
		return result, &WriteError{Reason: "channel_broken", Stage: "upload", Retryable: true, Err: err}
	}
	integrity, verifyErr := VerifyWrite(WriteSpec{Size: spec.Size, SHA256: spec.SHA256, MD5: spec.MD5}, result)
	result.Integrity = integrity
	return result, verifyErr
}

// EnsureDir 确保节点上的目录存在（acli 重装会清掉插件目录，写前必须能重建）。
func EnsureDir(ctx context.Context, r Runner, dir string) error {
	if r == nil || strings.TrimSpace(dir) == "" {
		return nil
	}
	res := Run(ctx, r, "mkdir -p "+ShellQuote(dir))
	if res.Err != nil {
		return res.Err
	}
	if res.ExitCode != 0 {
		return &ExitCodeError{Command: "mkdir -p " + ShellQuote(dir), ExitCode: res.ExitCode, Output: res.Stdout}
	}
	return nil
}

// CleanupTmp 尽力删除远端临时文件。失败不改变主结论，只由调用方记日志。
func CleanupTmp(ctx context.Context, r Runner, tmpPath string) {
	if r == nil || tmpPath == "" {
		return
	}
	cleanupCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	_, _, _ = r.RunQuickCommand(cleanupCtx, "rm -f "+ShellQuote(tmpPath))
}

// BuildChunkedArgvWriteCommands 把大内容拆成多条「argv 追加 + 校验 + 原子替换」命令。
//
// 为什么需要分块：脚本动辄几百 KB（collect_slow_disk.sh 359KB），而 argv 写入受
// Linux MAX_ARG_STRLEN（单参数 128KB）限制；一次性塞进 printf 会被 kernel 直接拒绝。
// 分块仍是同一条写入协议：先写确定性 tmp → 校验 size/sha256 → mv 覆盖，
// 只是把"填充"从一次 printf 拆成多次追加；校验与替换仍由 buildVerifiedMove 唯一实现。
//
// 关键实现约束：
//   - 按 UTF-8 字符边界切，绝不在多字节字符中间截断（否则写出的脚本是坏的）；
//   - 最后一条命令的 fill 用 ":"（恒真），只做校验+mv，避免把内容再传一遍；
//   - 调用方必须按顺序逐条执行；任一条失败都不得继续，否则 tmp 会拼进半截内容。
func BuildChunkedArgvWriteCommands(spec WriteSpec, chunkBytes int) []string {
	if chunkBytes <= 0 {
		chunkBytes = 32 << 10
	}
	tmpPath := spec.TmpPath
	if tmpPath == "" {
		tmpPath = spec.Path + ".tmp"
	}
	chunks := splitRunesByBytes(string(spec.Data), chunkBytes)
	commands := make([]string, 0, len(chunks)+1)
	for index, chunk := range chunks {
		operator := ">>"
		if index == 0 {
			operator = ">"
		}
		commands = append(commands, "printf '%s' "+ShellQuote(chunk)+" "+operator+" "+ShellQuote(tmpPath))
	}
	commands = append(commands, buildVerifiedMove(":", tmpPath, spec.Path, spec.Size, spec.SHA256, spec.MD5, spec.Mode))
	return commands
}

// splitRunesByBytes 按 UTF-8 字符边界把文本切成不超过 limit 字节的块。
// 空文本返回 nil：调用方应先拒绝空内容，而不是依赖本函数兜底。
func splitRunesByBytes(text string, limit int) []string {
	if text == "" {
		return nil
	}
	chunks := make([]string, 0, len(text)/limit+1)
	var builder strings.Builder
	size := 0
	for _, r := range text {
		width := len(string(r))
		if size > 0 && size+width > limit {
			chunks = append(chunks, builder.String())
			builder.Reset()
			size = 0
		}
		builder.WriteRune(r)
		size += width
	}
	if builder.Len() > 0 {
		chunks = append(chunks, builder.String())
	}
	return chunks
}

// Sha256Hex 计算分片校验和（与服务端 X-Part-SHA256 对齐）。
func Sha256Hex(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}
