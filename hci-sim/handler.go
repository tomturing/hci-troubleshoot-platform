package main

import (
	"encoding/binary"
	"encoding/json"
	"io"
	"log"
	"net"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

// Handler 处理每个 SSH session channel（方案 6.1：sshd / session manager）。
type Handler struct {
	router      *FixtureRouter
	leaseSecret string
}

func (h *Handler) handleChannel(ch ssh.NewChannel, remote net.Addr) {
	if ch.ChannelType() != "session" {
		_ = ch.Reject(ssh.UnknownChannelType, "unsupported channel type")
		return
	}
	channel, reqs, err := ch.Accept()
	if err != nil {
		return
	}

	env := map[string]string{}

	go func() {
		for req := range reqs {
			switch req.Type {
			case "exec":
				// RFC 4254: exec request payload = string(command)
				execCmd := string(req.Payload[4:])
				_ = req.Reply(true, nil)
				h.runExec(channel, execCmd, env, remote)
				return
			case "shell":
				_ = req.Reply(true, nil)
				h.runShell(channel, env, remote)
				return
			case "pty-req", "window-change":
				_ = req.Reply(true, nil)
			case "env":
				if name, value, ok := parseEnv(req.Payload); ok {
					env[name] = value
				}
				_ = req.Reply(true, nil)
			default:
				_ = req.Reply(true, nil)
			}
		}
		_ = channel.Close()
	}()
}

// runExec 处理一次 exec 请求（terminal_bridge ssh_exec_process 主路径）。
// stdout 通过 channel 直接写；stderr 通过 channel.Stderr() 写（方案 6.1 双通道隔离）。
func (h *Handler) runExec(ch ssh.Channel, raw string, env map[string]string, remote net.Addr) {
	ctx := buildLookupContext(env, remote)
	fp := ParseCommand(raw)

	fix, err := h.router.Resolve(ctx, fp)
	if err != nil {
		// fail closed：未知命令一律 exit 127 + fixture_not_found
		_, _ = io.WriteString(ch.Stderr(), "fixture_not_found\n")
		h.logEvent(ctx, fp, "fixture_not_found", 127, "")
		sendExitStatus(ch, 127)
		_ = ch.Close()
		return
	}

	// 可选：scenario lease 校验（P0 默认不强制）
	if h.leaseSecret != "" {
		if token := env["HCI_SIM_LEASE"]; !verifyLease(h.leaseSecret, token, ctx.ScenarioID) {
			_, _ = io.WriteString(ch.Stderr(), "lease_invalid\n")
			h.logEvent(ctx, fp, "lease_invalid", 127, fix.FixtureID)
			sendExitStatus(ch, 127)
			_ = ch.Close()
			return
		}
	}

	// 超时变体：模拟命令挂起，不返回 exit-status，等待对端超时断开
	if fix.Timeout {
		h.logEvent(ctx, fp, "timeout_begin", -1, fix.FixtureID)
		select {
		case <-time.After(10 * time.Minute):
		}
		_ = ch.Close()
		return
	}

	if fix.DelayProfileMS > 0 {
		time.Sleep(time.Duration(fix.DelayProfileMS) * time.Millisecond)
	}

	writeChunks(ch, fix.Stdout, fix.ChunkProfileMS)
	if fix.Stderr != "" {
		writeChunks(ch.Stderr(), fix.Stderr, fix.ChunkProfileMS)
	}

	h.logEvent(ctx, fp, "executed", fix.ExitCode, fix.FixtureID)
	sendExitStatus(ch, fix.ExitCode)
	_ = ch.Close()
}

// runShell 处理 shell 请求（逐行读取命令执行；P0 仅做最小支持）。
func (h *Handler) runShell(ch ssh.Channel, env map[string]string, remote net.Addr) {
	ctx := buildLookupContext(env, remote)
	buf := make([]byte, 0, 1024)
	tmp := make([]byte, 32)
	for {
		n, err := ch.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		for {
			idx := strings.IndexByte(string(buf), '\n')
			if idx < 0 {
				break
			}
			line := strings.TrimSpace(string(buf[:idx]))
			buf = buf[idx+1:]
			if line != "" && line != "exit" {
				h.runExecNoExit(ch, line, ctx)
			}
		}
		if err != nil {
			break
		}
	}
	_ = ch.Close()
}

func (h *Handler) runExecNoExit(ch ssh.Channel, raw string, ctx LookupContext) {
	fp := ParseCommand(raw)
	fix, err := h.router.Resolve(ctx, fp)
	if err != nil {
		_, _ = io.WriteString(ch.Stderr(), "fixture_not_found\n")
		return
	}
	writeChunks(ch, fix.Stdout, fix.ChunkProfileMS)
	if fix.Stderr != "" {
		writeChunks(ch.Stderr(), fix.Stderr, fix.ChunkProfileMS)
	}
}

// writeChunks 按行分块写入，可选每块延迟（模拟分块输出/慢链路）。
func writeChunks(w io.Writer, data string, chunkMS int) {
	if data == "" {
		return
	}
	lines := strings.Split(data, "\n")
	for i, line := range lines {
		if i > 0 {
			_, _ = io.WriteString(w, "\n")
		}
		_, _ = io.WriteString(w, line)
		if chunkMS > 0 {
			time.Sleep(time.Duration(chunkMS) * time.Millisecond)
		}
	}
}

// sendExitStatus 发送 SSH "exit-status" 请求（terminal_bridge 的 session.Wait 依赖此值）。
func sendExitStatus(ch ssh.Channel, code int) {
	payload := make([]byte, 4)
	binary.BigEndian.PutUint32(payload, uint32(code))
	_, _ = ch.SendRequest("exit-status", false, payload)
}

func buildLookupContext(env map[string]string, remote net.Addr) LookupContext {
	ctx := LookupContext{
		ScenarioID: env["HCI_SIM_SCENARIO_ID"],
		TestRunID:  env["HCI_SIM_TEST_RUN_ID"],
		NodeIP:     env["HCI_SIM_NODE_IP"],
	}
	if ctx.NodeIP == "" && remote != nil {
		ctx.NodeIP = remote.String()
	}
	if ak := env["HCI_SIM_ACQUISITION_KEY"]; ak != "" {
		ctx.AcquisitionKey = ak
	}
	return ctx
}

// parseEnv 解析 SSH "env" 请求 payload：string(name) string(value)。
func parseEnv(payload []byte) (string, string, bool) {
	if len(payload) < 4 {
		return "", "", false
	}
	nLen := binary.BigEndian.Uint32(payload[:4])
	payload = payload[4:]
	if uint32(len(payload)) < nLen+4 {
		return "", "", false
	}
	name := string(payload[:nLen])
	payload = payload[nLen:]
	if len(payload) < 4 {
		return "", "", false
	}
	vLen := binary.BigEndian.Uint32(payload[:4])
	payload = payload[4:]
	if uint32(len(payload)) < vLen {
		return "", "", false
	}
	return name, string(payload[:vLen]), true
}

// logEvent 输出结构化日志，便于把 trace_id -> exec_id -> fixture_id 关联起来。
func (h *Handler) logEvent(ctx LookupContext, fp CommandFingerprint, result string, code int, fixtureID string) {
	rec := map[string]interface{}{
		"simulation":   true,
		"backend":      "hci-sim",
		"scenario_id":  ctx.ScenarioID,
		"test_run_id":  ctx.TestRunID,
		"node_ip":      ctx.NodeIP,
		"acq_key":      fp.AcquisitionKey(),
		"command":      fp.Raw,
		"canonical":    fp.Canonical(),
		"result":       result,
		"exit_code":    code,
		"fixture_id":   fixtureID,
		"timestamp_ms": time.Now().UnixMilli(),
	}
	b, _ := json.Marshal(rec)
	log.Println(string(b))
}
