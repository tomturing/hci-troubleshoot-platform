package logcollect

// 节点侧 acli 日志文件采集（S7 证据链的「进程日志」页签）。
//
// 第一性原理：
//   - 排障要看 acli 自己的日志，而不是「它采了什么日志」。acli 把运行日志写在
//     /sf/log/today/acli 下：acli.log（内置插件初始化等）、cmd_output/*.log（命令输出）、
//     plugins/<name>/*.log（各插件进程日志）。这些文件在采集包里往往已被通配跳过或
//     整包加密，只有直接读文件才拿得到。
//   - 只依赖 SSH 会话读文件，不调用 acli：acli 自身坏掉（插件缺失、命令不可用）时，
//     恰恰是最需要这份日志的时候，不能再让它成为依赖。
//   - 目录结构随 acli 版本变化，用「固定深度 glob」而不是递归 find：HCI 宿主机是
//     极简系统，find 的 -printf/-newermt 不保证存在；三层 glob 足以覆盖上述三类路径。
//
// 对抗性审查：
//   - 文件名可能含空格，节点侧只输出路径，逐行按原样使用，绝不再按空格切分；
//   - 异常路径（..、越界绝对路径、非 .log）原地丢弃，平台侧还有白名单二次校验；
//   - 总量必须有界：超限按优先级截断并保留 truncated 事实，不能静默少收；
//   - 单次任务只跑一次（evidenceOnce），进度轮询不会把同一批文件重复入库。

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/sshx"
	"time"
)

const (
	// nodeLogRoot 是 acli 运行日志的根目录（固定路径，与 acli 安装布局同源）。
	nodeLogRoot = "/sf/log/today/acli"
	// nodeLogMaxDepth 是 glob 的最大层级：acli/*.log、acli/*/*.log、acli/*/*/*.log。
	nodeLogMaxDepth = 3
	// nodeLogMaxFiles 是单任务最多上报的文件数：覆盖 7 个插件 + 根日志 + cmd_output 仍有余量。
	nodeLogMaxFiles = 20
	// nodeLogFileBytes 与平台契约 max_process_log_file_bytes 保持一致（2 MiB）。
	nodeLogFileBytes = 2 * 1024 * 1024
	// nodeLogTaskBytes 与平台契约 max_process_log_task_bytes 保持一致（20 MiB）。
	nodeLogTaskBytes = 20 * 1024 * 1024
)

// nodeLogListCommand 用固定深度 glob 列出所有 .log 文件。
// 不用 find：精简系统上 find 的参数未必齐全，而 glob 由远端 shell 展开，零外部依赖。
func nodeLogListCommand() string {
	var builder strings.Builder
	for depth := 1; depth <= nodeLogMaxDepth; depth++ {
		glob := nodeLogRoot
		for index := 1; index < depth; index++ {
			glob += "/*"
		}
		glob += "/*.log"
		// [ -f ] 排除同名目录；[ -r ] 提前过滤不可读文件，避免每条都白跑一次 tail。
		builder.WriteString("for f in " + glob + "; do [ -f \"$f\" ] && [ -r \"$f\" ] && printf '%s\\n' \"$f\"; done; ")
	}
	builder.WriteString("true")
	return builder.String()
}

// nodeLogPriority 决定超限时先收哪些文件：
// 根日志与 log_collect 插件日志直接回答「采集为什么失败」，优先级最高；
// 其余插件日志与命令输出作为补充。同优先级按路径字典序，保证多次执行顺序稳定。
func nodeLogPriority(path string) int {
	relative := strings.TrimPrefix(path, nodeLogRoot+"/")
	switch {
	case relative == "acli.log":
		return 0
	case strings.HasPrefix(relative, "plugins/log_collect/"):
		return 1
	case strings.HasPrefix(relative, "cmd_output/"):
		return 2
	case strings.HasPrefix(relative, "plugins/"):
		return 3
	default:
		return 4
	}
}

// parseNodeLogPaths 把节点输出的路径列表归一化：越界、非 .log、含 .. 或 NUL 一律丢弃。
// 返回顺序即上报顺序（高优先级在前）。
func parseNodeLogPaths(raw string) []string {
	root := nodeLogRoot + "/"
	seen := map[string]bool{}
	paths := make([]string, 0, nodeLogMaxFiles)
	for _, line := range strings.Split(strings.ReplaceAll(raw, "\r\n", "\n"), "\n") {
		path := strings.TrimSpace(line)
		if path == "" || !strings.HasPrefix(path, root) || !strings.HasSuffix(path, ".log") {
			continue
		}
		if strings.Contains(path, "..") || strings.ContainsRune(path, 0) {
			continue
		}
		// 深度上限与 glob 对齐：多一层也不收，避免目录结构异常时无限遍历。
		if strings.Count(strings.TrimPrefix(path, root), "/") >= nodeLogMaxDepth {
			continue
		}
		if seen[path] {
			continue
		}
		seen[path] = true
		paths = append(paths, path)
	}
	sort.SliceStable(paths, func(i, j int) bool {
		left, right := nodeLogPriority(paths[i]), nodeLogPriority(paths[j])
		if left != right {
			return left < right
		}
		return paths[i] < paths[j]
	})
	return paths
}

// processLogPayload 是平台 /process-logs 端点的入参：路径、摘要与 gzip 正文。
type processLogPayload struct {
	Host         string `json:"host"`
	Path         string `json:"path"`
	Size         int    `json:"size"`
	SHA256       string `json:"sha256,omitempty"`
	ContentGzB64 string `json:"content_gz_b64,omitempty"`
	Truncated    bool   `json:"truncated"`
	Status       string `json:"status"`
	Error        string `json:"error,omitempty"`
	CollectedAt  string `json:"collected_at,omitempty"`
}

// collectNodeLogs 读取节点上 acli 目录下的全部 .log 并上报。
// 语义仍是「尽力而为的证据」：任何失败只留事件，绝不影响采集与交付主流程。
func collectNodeLogs(ctx context.Context, s *sshx.SSHSession, cfg EvidenceConfig) {
	if s == nil {
		return
	}
	key := "process-logs:" + cfg.TaskID
	if _, loaded := evidenceOnce.LoadOrStore(key, struct{}{}); loaded {
		return
	}
	listOut, _, code, err := s.RunQuickCommandStreams(ctx, nodeLogListCommand())
	if err != nil || code != 0 {
		evidence.Emit("WARN", "log_collect.process_logs_unavailable", "采集节点日志目录不可读", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "root": nodeLogRoot, "error": errString(err), "exit_code": code,
		})
		return
	}
	paths := parseNodeLogPaths(listOut)
	if len(paths) == 0 {
		evidence.Emit("INFO", "log_collect.process_logs_empty", "节点日志目录下没有可上报的 .log", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
			"task_id": cfg.TaskID, "root": nodeLogRoot,
		})
		return
	}
	var used int64
	count := 0
	skipped := 0
	for _, path := range paths {
		if count >= nodeLogMaxFiles || used >= nodeLogTaskBytes {
			// 剩余的按优先级靠后，直接计入 skipped 并留痕，不能静默丢弃。
			skipped++
			continue
		}
		// tail -c 取尾部：日志诊断关心最近发生了什么，截断必须切旧留新。
		content, _, contentCode, contentErr := s.RunQuickCommandStreams(ctx, "tail -c "+strconv.Itoa(nodeLogFileBytes)+" "+remote.ShellQuote(path))
		payload := processLogPayload{
			Host: cfg.NodeIP, Path: path, CollectedAt: time.Now().UTC().Format(time.RFC3339),
		}
		switch {
		case contentErr != nil || contentCode != 0:
			payload.Status = "error"
			payload.Error = firstNonEmpty(errString(contentErr), "节点侧读取失败")
		case strings.TrimSpace(content) == "":
			payload.Status = "empty"
		default:
			raw := []byte(content)
			if len(raw) > nodeLogFileBytes {
				raw = raw[:nodeLogFileBytes]
				payload.Truncated = true
			}
			var compressed bytes.Buffer
			writer := gzip.NewWriter(&compressed)
			_, _ = writer.Write(raw)
			_ = writer.Close()
			sum := sha256.Sum256(raw)
			payload.Status = "ok"
			payload.Size = len(raw)
			payload.SHA256 = hex.EncodeToString(sum[:])
			payload.ContentGzB64 = base64.StdEncoding.EncodeToString(compressed.Bytes())
			used += int64(len(raw))
		}
		postCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		_, postErr := cfg.PostEvidence(postCtx, cfg.TaskPath("process-logs"), payload, "")
		cancel()
		if postErr != nil {
			evidence.Emit("WARN", "log_collect.process_log_failed", "节点日志上报失败", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
				"task_id": cfg.TaskID, "path": path, "error": postErr.Error(),
			})
		}
		count++
	}
	evidence.Emit("INFO", "log_collect.process_logs_collected", "节点日志上报完成", cfg.TraceID, cfg.CaseID, cfg.NodeIP, cfg.CustomUI, map[string]any{
		"task_id": cfg.TaskID, "root": nodeLogRoot, "found": len(paths), "collected": count,
		"skipped": skipped, "bytes": used,
	})
}
