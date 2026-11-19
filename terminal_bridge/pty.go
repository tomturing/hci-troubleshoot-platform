package main

import (
	"io"
	"strings"

	"golang.org/x/crypto/ssh"
)

const (
	ptyTerminalHeight = 40
	ptyTerminalWidth  = 160
)

// shouldUsePty 判定是否为本条命令分配伪终端。
//
// 第一性原理：是否需要 PTY 取决于"被执行程序是否检查 stdin 为 tty"，而不是调用方
// 的猜测。acli 插件命令在容器内 exec 时会做该检查，非 TTY 直接失败，因此默认对
// “acli“ 前缀命令开启；显式字段用于覆盖（例如需要在非 acli 命令上强制 PTY）。
func shouldUsePty(command string, explicit bool) bool {
	if explicit {
		return true
	}
	trimmed := strings.TrimSpace(command)
	return trimmed == "acli" || strings.HasPrefix(trimmed, "acli ")
}

// ptyTerminalModes 关闭回显与输出换行转换：
//   - ECHO=0：避免输入被回显进 stdout；
//   - OPOST=0 / ONLCR=0：禁止把 \n 改写成 \r\n，从源头减少 CRLF。
func ptyTerminalModes() ssh.TerminalModes {
	return ssh.TerminalModes{
		ssh.ECHO:  0,
		ssh.OPOST: 0,
		ssh.ONLCR: 0,
	}
}

// crlfNormalizingReader 把 PTY 输出的 CRLF 归一化为 LF。
//
// 即使关闭 ONLCR，仍可能有服务端或程序自身输出 CRLF；且 "\r\n" 可能被拆分到两次
// Read 之间，因此必须保留末字节 \r 的待定状态，不能简单对单个 chunk 做字符串替换。
type crlfNormalizingReader struct {
	src     io.Reader
	buf     []byte
	pending bool // 上一 chunk 以 \r 结尾，等待下一字节决定是否合并为 LF
	done    bool
}

func (r *crlfNormalizingReader) Read(p []byte) (int, error) {
	for len(r.buf) == 0 {
		if r.done {
			return 0, io.EOF
		}
		tmp := make([]byte, 4096)
		n, err := r.src.Read(tmp)
		if n > 0 {
			r.absorb(tmp[:n])
		}
		if err != nil {
			r.done = true
			r.flushPending()
			if len(r.buf) == 0 {
				return 0, err
			}
		}
	}
	n := copy(p, r.buf)
	r.buf = r.buf[n:]
	return n, nil
}

func (r *crlfNormalizingReader) absorb(chunk []byte) {
	data := chunk
	if r.pending {
		r.pending = false
		if len(data) > 0 {
			if data[0] == '\n' {
				r.buf = append(r.buf, '\n')
				data = data[1:]
			} else {
				r.buf = append(r.buf, '\r')
			}
		}
	}
	for i := 0; i < len(data); i++ {
		if data[i] != '\r' {
			r.buf = append(r.buf, data[i])
			continue
		}
		if i+1 < len(data) {
			if data[i+1] == '\n' {
				r.buf = append(r.buf, '\n')
				i++
			} else {
				r.buf = append(r.buf, '\r')
			}
			continue
		}
		r.pending = true
	}
}

// flushPending 在 EOF 时输出悬空的孤立 \r，保证不丢字节。
func (r *crlfNormalizingReader) flushPending() {
	if r.pending {
		r.buf = append(r.buf, '\r')
		r.pending = false
	}
}

// normalizePtyOutput 对最终输出做幂等归一化（reader 已处理流式场景，这里是兜底）。
func normalizePtyOutput(s string) string {
	return strings.ReplaceAll(s, "\r\n", "\n")
}

// execCommandIsolated 独立建立 SSH 连接与 Session 执行命令（双通道事务执行设计）。
// 独立连接是硬超时的必要条件：仅关闭共享连接上的 Session 时，部分 SSH 服务端会等待
// 远端进程自然退出，导致 deadline 已触发但调用仍被阻塞；关闭独立连接才能确定性中止。
