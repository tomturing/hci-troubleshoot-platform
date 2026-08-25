// Package server 的虚拟机控制台截图（qkv_vm_console）仿真分支。
//
// 背景：terminal_bridge 的 vm_console_op 通道只执行代码常量表构造的固定操作，
// 其截图临时路径携带服务端动态生成的 capture_id（UUID），无法像既有命令那样
// 发布为静态 Fixture RouteKey（精确路由不接受通配）。因此本文件对 Bridge 固定
// 操作的精确形态做确定性仿真：只匹配与 Bridge 常量表逐 token 一致的命令，
// 其余命令一律落回既有严格 Fixture 路由（fail-closed 语义不变）。
//
// 测试形态约定（沿用 hci_sim 既有 variant 约定）：租约 FixtureVariant 包含
// "near-black" 时，base64 读取返回近黑画面 PPM；否则返回常规画面 PPM。
package server

import (
	"encoding/base64"
	"fmt"
	"regexp"
	"strings"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
	"hci_sim/internal/telemetry"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/crypto/ssh"
)

const (
	// 与 terminal_bridge 常量表保持一致的固定路径前缀。
	vmConsoleSimCaptureDir = "/sf/data/local/hci-diagnosis"

	// 近黑画面 variant 标记：沿用 hci_sim 以租约 variant 区分 fixture 形态的约定。
	vmConsoleSimNearBlackVariantMarker = "near-black"
)

var (
	// vtpsh Monitor URI 精确形态：/nodes/<host_node_id>/qemu/<vm_id>/monitor
	vmConsoleSimMonitorPathPattern = regexp.MustCompile(`^/nodes/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/qemu/[0-9]{1,20}/monitor$`)
	// screendump 目标文件精确形态：固定目录 + UUID 文件名。
	vmConsoleSimScreendumpPattern = regexp.MustCompile(`^screendump ` + vmConsoleSimCaptureDir + `/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.ppm$`)
	// 截图临时文件路径精确形态（test -f / base64 -w0 / rm -f 共用）。
	vmConsoleSimCapturePathPattern = regexp.MustCompile(`^` + vmConsoleSimCaptureDir + `/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.ppm$`)
)

// simulateVMConsoleCommand 识别 Bridge 固定操作命令并返回确定性仿真结果。
// 返回 (stdout, exitCode, matched)；matched=false 时调用方必须落回既有 Fixture 路由。
// 只匹配与 Bridge 常量表完全一致的 argv 形态，任何变形（多余参数、路径偏移、
// 非法标识符）都不匹配，从而被严格路由以 fixture_not_found 拒绝。
func simulateVMConsoleCommand(command string, claims lease.Claims) (string, int, bool) {
	argv, err := fixture.Lex(command)
	if err != nil || len(argv) == 0 {
		return "", 0, false
	}

	// vtpsh create /nodes/<host>/qemu/<vmid>/monitor --command "<monitor 指令>"
	if len(argv) == 5 && argv[0] == "vtpsh" && argv[1] == "create" && argv[3] == "--command" &&
		vmConsoleSimMonitorPathPattern.MatchString(argv[2]) && monitorPathNode(argv[2]) == claims.VirtualNodeID {
		switch {
		case vmConsoleSimScreendumpPattern.MatchString(argv[4]):
			return "", exitOK, true
		case argv[4] == "sendkey down":
			return "", exitOK, true
		}
		return "", 0, false
	}

	// test -f / base64 -w0 / rm -f 只接受固定目录下的 UUID 截图文件。
	if len(argv) == 3 && vmConsoleSimCapturePathPattern.MatchString(argv[2]) {
		switch argv[0] + " " + argv[1] {
		case "test -f":
			// 仿真宿主机上截图文件在 screendump 成功后立即可见。
			return "", exitOK, true
		case "rm -f":
			return "", exitOK, true
		case "base64 -w0":
			nearBlack := strings.Contains(strings.ToLower(claims.FixtureVariant), vmConsoleSimNearBlackVariantMarker)
			return base64.StdEncoding.EncodeToString(simulatedVMConsolePPM(nearBlack)) + "\n", exitOK, true
		}
	}
	return "", 0, false
}

// executeVMConsole 以与 Fixture 路由一致的可观测性约定回传固定操作的仿真结果：
// 输出配额预算、链路 Span、结构化日志、Run Result 事件与 exit-status。
func (s *Server) executeVMConsole(job *commandJob, stdout string, exitCode int, workerID int) commandOutcome {
	s.config.Metrics.VMConsoleSimsTotal.Add(1)
	outputBytes := int64(len(stdout))
	if outputBytes > int64(s.config.MaxOutputBytes) {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		outcome := commandOutcome{exitCode: exitInternal}
		s.respond(job, outcome, "sim_output_limit_exceeded", "vm_console 仿真输出超过 Runtime 限制")
		return outcome
	}
	if err := s.tracker.ReserveOutput(job.claims, outputBytes, time.Now()); err != nil {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		outcome := commandOutcome{exitCode: exitInternal}
		s.respond(job, outcome, "sim_output_limit_exceeded", err.Error())
		return outcome
	}
	spanCtx := telemetry.ContextFromEnv(job.env)
	spanCtx, span := otel.Tracer("hci-sim").Start(spanCtx, "hci-sim.ssh.vm_console",
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			attribute.String("exec.id", job.env["HTP_EXEC_ID"]),
			attribute.String("test_run.id", job.claims.TestRunID),
			attribute.String("scenario.id", job.claims.ScenarioID),
			attribute.String("lease.id", job.claims.LeaseID),
			attribute.String("fixture.variant", job.claims.FixtureVariant),
			attribute.String("virtual_node.id", job.env["HTP_NODE_IP"]),
			attribute.String("command.fingerprint", contentHash(job.command)),
			attribute.Int("worker.id", workerID),
		),
	)
	defer span.End()
	start := time.Now()
	logEvent("INFO", "vm_console.start", baseFields(job.claims, map[string]any{
		"trace_id": span.SpanContext().TraceID().String(), "exec_id": job.env["HTP_EXEC_ID"],
		"command_fingerprint": contentHash(job.command), "worker_id": workerID,
	}))
	stdoutBytes, stdoutErr := writeChunks(spanCtx, job.channel, []byte(stdout), 0, 0)
	s.config.Metrics.StdoutBytesTotal.Add(uint64(stdoutBytes))
	if stdoutErr != nil {
		exitCode = exitInternal
		span.SetStatus(codes.Error, "stream_write_failed")
	}
	if exitCode != 0 {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		span.SetStatus(codes.Error, "nonzero_exit")
	}
	span.SetAttributes(
		attribute.Int("process.exit.code", exitCode),
		attribute.Int64("exec.duration_ms", time.Since(start).Milliseconds()),
		attribute.Int("stdout.bytes", stdoutBytes),
	)
	outcome := commandOutcome{exitCode: exitCode}
	if job.mode == commandModeExec {
		_, _ = job.channel.SendRequest("exit-status", false, ssh.Marshal(exitStatus{Status: uint32(exitCode)}))
		_ = job.channel.Close()
	}
	logEvent("INFO", "vm_console.done", baseFields(job.claims, map[string]any{
		"trace_id": span.SpanContext().TraceID().String(), "exec_id": job.env["HTP_EXEC_ID"],
		"exit_code": exitCode, "stdout_bytes": stdoutBytes,
		"duration_ms": time.Since(start).Milliseconds(),
	}))
	s.recordEvent(job, "vm_console.done", fmt.Sprintf("vm_console:%d:%d", exitCode, stdoutBytes), span.SpanContext().TraceID().String())
	return outcome
}

// monitorPathNode 从 /nodes/<node>/qemu/<vmid>/monitor 中提取 <node>。
func monitorPathNode(path string) string {
	trimmed := strings.TrimPrefix(path, "/nodes/")
	if index := strings.Index(trimmed, "/"); index >= 0 {
		return trimmed[:index]
	}
	return trimmed
}

// simulatedVMConsolePPM 生成确定性的 P6 PPM 测试图。
// 常规画面为高亮度彩色渐变；近黑画面所有像素亮度 ≤ 2，用于验证
// Bridge/平台侧近黑检测与唤醒重截链路。
func simulatedVMConsolePPM(nearBlack bool) []byte {
	const width, height = 8, 6
	header := fmt.Sprintf("P6\n%d %d\n255\n", width, height)
	pixels := make([]byte, 0, width*height*3)
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			if nearBlack {
				// 近黑：保留极弱噪声，仍满足确定性近黑阈值。
				pixels = append(pixels, byte((x+y)%3), byte((x+y)%2), byte((x*y)%3))
				continue
			}
			// 常规：亮色渐变 + 白色文本带，模拟有有效画面的控制台。
			pixels = append(pixels, byte(64+x*24), byte(96+y*24), byte(200))
		}
	}
	return append([]byte(header), pixels...)
}
