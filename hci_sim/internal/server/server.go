// Package server 实现受控的多租户 SSH 仿真数据面。
package server

import (
	"bufio"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"regexp"
	"strings"
	"sync"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
	"hci_sim/internal/metrics"
	"hci_sim/internal/telemetry"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/crypto/ssh"
)

const (
	exitOK              = 0
	exitPolicyDenied    = 126
	exitFixtureNotFound = 127
	exitOverloaded      = 75
	exitInternal        = 70
	exitTimeout         = 124
	exitCancelled       = 125
)

var allowedEnv = map[string]struct{}{
	"TRACEPARENT": {}, "TRACESTATE": {}, "HTP_EXEC_ID": {}, "HTP_TEST_RUN_ID": {},
	"HTP_NODE_IP": {}, "HTP_CONTAINER": {},
}

type Config struct {
	ListenAddress  string
	HostSigner     ssh.Signer
	LeaseSecret    []byte
	Pool           *fixture.BundlePool
	Router         *fixture.Router
	Workers        int
	QueueSize      int
	MaxOutputBytes int
	LeaseIssuer    string
	LeaseAudience  string
	Metrics        *metrics.Metrics
	Recorder       EventRecorder
}

// EventRecorder is deliberately narrower than the database Repository. The
// SSH data plane records only redacted event digests; Run Result is finalized
// by the control-plane runner after oracle evaluation.
type EventRecorder interface {
	RecordEvent(context.Context, string, int, string, string, string) error
}

type Server struct {
	config     Config
	sshConfig  *ssh.ServerConfig
	tracker    *lease.Tracker
	jobs       chan *commandJob
	workerWG   sync.WaitGroup
	listenerMu sync.Mutex
	listener   net.Listener
}

func routerForSupport(config Config, supportID string) *fixture.Router {
	if config.Pool != nil {
		return config.Pool.Get(supportID)
	}
	if config.Router != nil && config.Router.KBD().SupportID == supportID {
		return config.Router
	}
	return nil
}

func routerMatchesClaims(router *fixture.Router, claims lease.Claims) bool {
	if router == nil || router.KBD().Revision != claims.KBDRevision {
		return false
	}
	constantEqual := func(left, right string) bool {
		return subtle.ConstantTimeCompare([]byte(left), []byte(right)) == 1
	}
	return constantEqual(router.BundleDigest(), claims.BundleDigest) &&
		constantEqual(router.Contracts().ToolRevision, claims.ToolContractRevision) &&
		constantEqual(router.Contracts().PolicyRevision, claims.PolicyRevision)
}

type commandJob struct {
	ctx     context.Context
	channel ssh.Channel
	claims  lease.Claims
	env     map[string]string
	command string
	mode    commandMode
	done    chan commandOutcome
}

type commandMode uint8

const (
	commandModeExec commandMode = iota
	commandModeShell
)

type commandOutcome struct {
	exitCode  int
	cancelled bool
}

type envRequest struct {
	Name  string
	Value string
}

type execRequest struct {
	Command string
}

type exitStatus struct {
	Status uint32
}

func New(config Config) (*Server, error) {
	if config.ListenAddress == "" || config.HostSigner == nil || len(config.LeaseSecret) < 32 || (config.Pool == nil && config.Router == nil) {
		return nil, errors.New("hci-sim server 配置不完整")
	}
	if config.Workers < 1 || config.Workers > 256 {
		return nil, errors.New("worker 数必须在 1-256")
	}
	if config.QueueSize < 1 || config.QueueSize > 10000 {
		return nil, errors.New("queue size 必须在 1-10000")
	}
	if config.MaxOutputBytes < 1 || config.MaxOutputBytes > 64*1024*1024 {
		return nil, errors.New("Runtime 输出限制必须在 1-64MiB")
	}
	if config.LeaseIssuer == "" || config.LeaseAudience == "" {
		return nil, errors.New("Lease issuer 和 audience 不能为空")
	}
	if config.Metrics == nil {
		config.Metrics = &metrics.Metrics{}
	}
	s := &Server{
		config:  config,
		tracker: lease.NewTracker(),
		jobs:    make(chan *commandJob, config.QueueSize),
	}
	s.sshConfig = &ssh.ServerConfig{
		PasswordCallback: func(meta ssh.ConnMetadata, password []byte) (*ssh.Permissions, error) {
			claims, err := lease.Validate(config.LeaseSecret, string(password), "", config.LeaseIssuer, config.LeaseAudience, time.Now())
			router := routerForSupport(config, claims.SupportID)
			if err != nil || meta.User() != "sim" || !routerMatchesClaims(router, claims) {
				config.Metrics.LeaseRejectTotal.Add(1)
				reason := safeError(err)
				if err == nil {
					reason = "lease_bundle_contract_mismatch"
				}
				logEvent("WARN", "lease.rejected", map[string]any{"remote": meta.RemoteAddr().String(), "reason": reason})
				return nil, errors.New("场景租约认证失败")
			}
			encoded, _ := json.Marshal(claims)
			return &ssh.Permissions{Extensions: map[string]string{"hci-sim-claims": string(encoded)}}, nil
		},
	}
	s.sshConfig.AddHostKey(config.HostSigner)
	for workerID := 0; workerID < config.Workers; workerID++ {
		s.workerWG.Add(1)
		go s.worker(workerID)
	}
	return s, nil
}

func (s *Server) Serve(ctx context.Context) error {
	listener, err := net.Listen("tcp", s.config.ListenAddress)
	if err != nil {
		return err
	}
	s.listenerMu.Lock()
	s.listener = listener
	s.listenerMu.Unlock()
	bundles := []fixture.BundleInfo{}
	if s.config.Pool != nil {
		bundles = s.config.Pool.Bundles()
	} else if s.config.Router != nil {
		bundles = append(bundles, fixture.BundleInfo{SupportID: s.config.Router.KBD().SupportID, KBDRevision: s.config.Router.KBD().Revision, BundleDigest: s.config.Router.BundleDigest()})
	}
	logEvent("INFO", "server.started", map[string]any{
		"address": s.config.ListenAddress, "workers": s.config.Workers,
		"queue_size": s.config.QueueSize, "fixture_bundles": bundles, "fixture_bundle_count": len(bundles),
	})
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	for {
		connection, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		go s.handleConnection(ctx, connection)
	}
}

func (s *Server) Close() error {
	s.listenerMu.Lock()
	defer s.listenerMu.Unlock()
	if s.listener != nil {
		return s.listener.Close()
	}
	return nil
}

// RevokeLease 供受信任的控制面在 Run 取消或安全事件时撤销 Capability。
// 网络 API 不在 Runtime 数据面暴露，调用方必须经过控制面鉴权。
func (s *Server) RevokeLease(jti string, until time.Time) {
	s.tracker.Revoke(jti, until)
}

// Addr 返回实际监听地址，主要用于 P0 集成测试中的随机端口。
func (s *Server) Addr() string {
	s.listenerMu.Lock()
	defer s.listenerMu.Unlock()
	if s.listener == nil {
		return ""
	}
	return s.listener.Addr().String()
}

func (s *Server) handleConnection(parent context.Context, raw net.Conn) {
	connection, channels, requests, err := ssh.NewServerConn(raw, s.sshConfig)
	if err != nil {
		_ = raw.Close()
		return
	}
	defer connection.Close()
	claims, err := claimsFromPermissions(connection.Permissions)
	if err != nil {
		return
	}
	release, err := s.tracker.AcquireSession(claims, time.Now())
	if err != nil {
		logEvent("WARN", "lease.session_quota", baseFields(claims, map[string]any{"reason": err.Error()}))
		return
	}
	defer release()
	s.config.Metrics.ActiveSSHConnections.Add(1)
	s.config.Metrics.SSHConnectionsTotal.Add(1)
	defer s.config.Metrics.ActiveSSHConnections.Add(-1)
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	go ssh.DiscardRequests(requests)
	logEvent("INFO", "ssh.connected", baseFields(claims, map[string]any{"remote": connection.RemoteAddr().String()}))
	for newChannel := range channels {
		if newChannel.ChannelType() != "session" {
			_ = newChannel.Reject(ssh.UnknownChannelType, "hci-sim 只允许 session channel")
			continue
		}
		channel, channelRequests, err := newChannel.Accept()
		if err != nil {
			continue
		}
		go s.handleChannel(ctx, channel, channelRequests, claims)
	}
}

func (s *Server) handleChannel(ctx context.Context, channel ssh.Channel, requests <-chan *ssh.Request, claims lease.Claims) {
	env := make(map[string]string)
	for request := range requests {
		switch request.Type {
		case "env":
			var payload envRequest
			if err := ssh.Unmarshal(request.Payload, &payload); err != nil {
				_ = request.Reply(false, nil)
				continue
			}
			if _, ok := allowedEnv[payload.Name]; !ok || len(payload.Value) > 1024 {
				_ = request.Reply(false, nil)
				continue
			}
			env[payload.Name] = payload.Value
			_ = request.Reply(true, nil)
		case "pty-req":
			_ = request.Reply(true, nil)
		case "shell":
			_ = request.Reply(true, nil)
			s.controlledShell(ctx, channel, claims)
			return
		case "exec":
			var payload execRequest
			if err := ssh.Unmarshal(request.Payload, &payload); err != nil || strings.TrimSpace(payload.Command) == "" {
				_ = request.Reply(false, nil)
				return
			}
			_ = request.Reply(true, nil)
			s.submit(&commandJob{ctx: ctx, channel: channel, claims: claims, env: cloneMap(env), command: payload.Command, mode: commandModeExec, done: make(chan commandOutcome, 1)})
			return
		default:
			_ = request.Reply(false, nil)
		}
	}
	_ = channel.Close()
}

func (s *Server) submit(job *commandJob) commandOutcome {
	select {
	case s.jobs <- job:
		s.config.Metrics.QueueDepth.Add(1)
	case <-job.ctx.Done():
		outcome := commandOutcome{exitCode: exitCancelled, cancelled: true}
		s.respond(job, outcome, "sim_cancelled", "命令在入队前已取消")
		return outcome
	default:
		s.config.Metrics.OverloadRejectsTotal.Add(1)
		outcome := commandOutcome{exitCode: exitOverloaded}
		s.respond(job, outcome, "sim_overloaded", "hci-sim 执行队列已满")
		return outcome
	}
	select {
	case outcome := <-job.done:
		return outcome
	case <-job.ctx.Done():
		// worker 会在开始执行前复检 ctx；这里不关闭 channel，避免同 worker 竞争写入。
		return commandOutcome{exitCode: exitCancelled, cancelled: true}
	}
}

func (s *Server) worker(workerID int) {
	defer s.workerWG.Done()
	for job := range s.jobs {
		s.config.Metrics.QueueDepth.Add(-1)
		s.config.Metrics.InflightCommands.Add(1)
		outcome := s.execute(job, workerID)
		s.config.Metrics.InflightCommands.Add(-1)
		job.done <- outcome
	}
}

func (s *Server) execute(job *commandJob, workerID int) commandOutcome {
	if err := job.ctx.Err(); err != nil {
		outcome := commandOutcome{exitCode: exitCancelled, cancelled: true}
		s.respond(job, outcome, "sim_cancelled", "命令已取消")
		return outcome
	}
	s.config.Metrics.CommandsTotal.Add(1)
	if err := s.tracker.AuthorizeCommand(job.claims, time.Now()); err != nil {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		outcome := commandOutcome{exitCode: exitPolicyDenied}
		s.respond(job, outcome, "lease_quota_exceeded", err.Error())
		return outcome
	}
	router := routerForSupport(s.config, job.claims.SupportID)
	if !routerMatchesClaims(router, job.claims) {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		s.config.Metrics.FixtureMissesTotal.Add(1)
		outcome := commandOutcome{exitCode: exitFixtureNotFound}
		s.respond(job, outcome, "fixture_not_found", "租约绑定的 Fixture Bundle 不可用")
		return outcome
	}
	result, err := router.Match(job.command, job.claims.FixtureVariant, job.claims.VirtualNodeID, job.claims.Container)
	if err != nil {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		if err.Error() == "fixture_not_found" {
			s.config.Metrics.FixtureMissesTotal.Add(1)
			outcome := commandOutcome{exitCode: exitFixtureNotFound}
			s.respond(job, outcome, "fixture_not_found", "未发布的命令 fixture")
			return outcome
		} else {
			outcome := commandOutcome{exitCode: exitPolicyDenied}
			s.respond(job, outcome, "policy_denied", err.Error())
			return outcome
		}
	}
	s.config.Metrics.FixtureHitsTotal.Add(1)
	outputBytes := int64(len(result.Stdout) + len(result.Stderr))
	if outputBytes > int64(router.OutputLimit()) || (s.config.MaxOutputBytes > 0 && outputBytes > int64(s.config.MaxOutputBytes)) {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		outcome := commandOutcome{exitCode: exitInternal}
		s.respond(job, outcome, "sim_output_limit_exceeded", "fixture 输出超过 Runtime 限制")
		return outcome
	}
	if err := s.tracker.ReserveOutput(job.claims, outputBytes, time.Now()); err != nil {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		outcome := commandOutcome{exitCode: exitInternal}
		s.respond(job, outcome, "sim_output_limit_exceeded", err.Error())
		return outcome
	}
	spanCtx := telemetry.ContextFromEnv(job.env)
	spanCtx, span := otel.Tracer("hci-sim").Start(spanCtx, "hci-sim.ssh.exec",
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			attribute.String("exec.id", job.env["HTP_EXEC_ID"]),
			attribute.String("test_run.id", job.claims.TestRunID),
			attribute.String("scenario.id", job.claims.ScenarioID),
			attribute.String("lease.id", job.claims.LeaseID),
			attribute.String("fixture.id", result.FixtureID),
			attribute.String("fixture.variant", job.claims.FixtureVariant),
			attribute.String("fixture.bundle_digest", job.claims.BundleDigest),
			attribute.String("command.fingerprint", result.CommandFingerprint),
			attribute.String("virtual_node.id", job.env["HTP_NODE_IP"]),
			attribute.Int("worker.id", workerID),
		),
	)
	defer span.End()
	start := time.Now()
	logEvent("INFO", "exec.start", baseFields(job.claims, map[string]any{
		"trace_id": span.SpanContext().TraceID().String(), "exec_id": job.env["HTP_EXEC_ID"],
		"fixture_id": result.FixtureID, "signal_id": result.SignalID,
		"command_fingerprint": result.CommandFingerprint, "worker_id": workerID,
	}))
	if result.Fault.Type == fixture.FaultTimeout {
		// 故障超时以确定性状态立即返回，绝不让 worker 被虚拟等待占满。
		outcome := commandOutcome{exitCode: exitTimeout}
		s.respond(job, outcome, "sim_timeout", "fixture 注入超时")
		span.SetStatus(codes.Error, "timeout")
		return outcome
	}
	if result.Fault.AfterMS > 0 {
		timer := time.NewTimer(time.Duration(result.Fault.AfterMS) * time.Millisecond)
		select {
		case <-timer.C:
		case <-job.ctx.Done():
			timer.Stop()
			span.RecordError(job.ctx.Err())
			span.SetStatus(codes.Error, "cancelled")
			outcome := commandOutcome{exitCode: exitCancelled, cancelled: true}
			s.respond(job, outcome, "sim_cancelled", "命令执行已取消")
			return outcome
		}
	}
	stdoutValue, stderrValue := []byte(result.Stdout), []byte(result.Stderr)
	if result.Fault.Type == fixture.FaultTruncate && result.Fault.MaxBytes > 0 {
		stdoutValue = truncate(stdoutValue, result.Fault.MaxBytes)
		stderrValue = truncate(stderrValue, max(0, result.Fault.MaxBytes-len(stdoutValue)))
	}
	stdoutBytes, stdoutErr := writeChunks(spanCtx, job.channel, stdoutValue, result.ChunkBytes, result.ChunkIntervalMS)
	stderrBytes, stderrErr := writeChunks(spanCtx, job.channel.Stderr(), stderrValue, result.ChunkBytes, result.ChunkIntervalMS)
	s.config.Metrics.StdoutBytesTotal.Add(uint64(stdoutBytes))
	s.config.Metrics.StderrBytesTotal.Add(uint64(stderrBytes))
	exitCode := result.ExitCode
	if result.Fault.Type == fixture.FaultPermission && exitCode == 0 {
		exitCode = 13
	}
	if result.Fault.Type == fixture.FaultNonzeroExit && exitCode == 0 {
		exitCode = 1
	}
	if result.Fault.Type == fixture.FaultDisconnect {
		_ = job.channel.Close()
		return commandOutcome{exitCode: exitInternal}
	}
	if stdoutErr != nil || stderrErr != nil {
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
		attribute.Int("stdout.bytes", stdoutBytes), attribute.Int("stderr.bytes", stderrBytes),
	)
	outcome := commandOutcome{exitCode: exitCode}
	if job.mode == commandModeExec {
		_, _ = job.channel.SendRequest("exit-status", false, ssh.Marshal(exitStatus{Status: uint32(exitCode)}))
		_ = job.channel.Close()
	}
	logEvent("INFO", "exec.done", baseFields(job.claims, map[string]any{
		"trace_id": span.SpanContext().TraceID().String(), "exec_id": job.env["HTP_EXEC_ID"],
		"fixture_id": result.FixtureID, "exit_code": exitCode,
		"stdout_bytes": stdoutBytes, "stderr_bytes": stderrBytes,
		"stdout_sha256": contentHash(result.Stdout), "stderr_sha256": contentHash(result.Stderr),
		"duration_ms": time.Since(start).Milliseconds(),
	}))
	s.recordEvent(job, "exec.done", fmt.Sprintf("%s:%d:%d:%d", result.FixtureID, exitCode, stdoutBytes, stderrBytes), span.SpanContext().TraceID().String())
	return outcome
}

func (s *Server) respond(job *commandJob, outcome commandOutcome, kind, message string) {
	s.recordEvent(job, kind, fmt.Sprintf("%d:%s", outcome.exitCode, message), "")
	_, _ = io.WriteString(job.channel.Stderr(), fmt.Sprintf("hci-sim: %s: %s\n", kind, message))
	if job.mode == commandModeExec {
		_, _ = job.channel.SendRequest("exit-status", false, ssh.Marshal(exitStatus{Status: uint32(outcome.exitCode)}))
		_ = job.channel.Close()
	}
}

func (s *Server) recordEvent(job *commandJob, eventType, value, traceID string) {
	if s.config.Recorder == nil || job == nil || job.claims.TestRunID == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := s.config.Recorder.RecordEvent(ctx, job.claims.TestRunID, 1, eventType, "sha256:"+contentHash(value), traceID); err != nil {
		logEvent("WARN", "run.event_persist_failed", baseFields(job.claims, map[string]any{"event_type": eventType, "reason": safeError(err)}))
	}
}

var markerPattern = regexp.MustCompile(`(__HCI_DONE_[A-Za-z0-9_]+__)`)

// controlledShell 只服务 Browser 初始连接和环境采集，不执行系统 shell。
func (s *Server) controlledShell(ctx context.Context, channel ssh.Channel, claims lease.Claims) {
	defer channel.Close()
	_, _ = io.WriteString(channel, fmt.Sprintf("HCI-SIM KBD %s / scenario %s\r\nsim@hci-sim$ ", claims.SupportID, claims.ScenarioID))
	scanner := bufio.NewScanner(channel)
	scanner.Buffer(make([]byte, 4096), 256*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(strings.TrimSuffix(scanner.Text(), "\r"))
		if line == "" {
			_, _ = io.WriteString(channel, "sim@hci-sim$ ")
			continue
		}
		if line == "exit" || line == "logout" {
			return
		}
		command, marker := splitMarkerCommand(line)
		// shell 与 exec 使用同一队列、租约复验、RouteKey 与输出配额；shell 仅负责 marker/prompt 协议。
		outcome := s.submit(&commandJob{ctx: ctx, channel: channel, claims: claims, command: command, mode: commandModeShell, done: make(chan commandOutcome, 1)})
		if marker != "" {
			_, _ = fmt.Fprintf(channel, "\r\n%s:%d\r\n", marker, outcome.exitCode)
		}
		if outcome.cancelled {
			return
		}
		select {
		case <-ctx.Done():
			return
		default:
			_, _ = io.WriteString(channel, "sim@hci-sim$ ")
		}
	}
}

func splitMarkerCommand(line string) (string, string) {
	separator := "; status=$?; printf"
	if index := strings.Index(line, separator); index >= 0 {
		marker := markerPattern.FindString(line[index:])
		return strings.TrimSpace(line[:index]), marker
	}
	return line, ""
}

func writeChunks(ctx context.Context, writer io.Writer, value []byte, chunkSize, intervalMS int) (int, error) {
	if chunkSize <= 0 {
		chunkSize = len(value)
	}
	if chunkSize == 0 {
		return 0, nil
	}
	written := 0
	for len(value) > 0 {
		select {
		case <-ctx.Done():
			return written, ctx.Err()
		default:
		}
		current := chunkSize
		if current > len(value) {
			current = len(value)
		}
		n, err := writer.Write(value[:current])
		written += n
		if err != nil {
			return written, err
		}
		value = value[current:]
		if intervalMS > 0 && len(value) > 0 {
			timer := time.NewTimer(time.Duration(intervalMS) * time.Millisecond)
			select {
			case <-timer.C:
			case <-ctx.Done():
				timer.Stop()
				return written, ctx.Err()
			}
		}
	}
	return written, nil
}

func claimsFromPermissions(permissions *ssh.Permissions) (lease.Claims, error) {
	if permissions == nil {
		return lease.Claims{}, errors.New("SSH permissions 为空")
	}
	var claims lease.Claims
	if err := json.Unmarshal([]byte(permissions.Extensions["hci-sim-claims"]), &claims); err != nil {
		return lease.Claims{}, err
	}
	return claims, nil
}

func cloneMap(source map[string]string) map[string]string {
	target := make(map[string]string, len(source))
	for key, value := range source {
		target[key] = value
	}
	return target
}

func baseFields(claims lease.Claims, extra map[string]any) map[string]any {
	fields := map[string]any{
		"simulation": true, "execution_mode": claims.ExecutionMode, "simulation_backend": "hci-sim",
		"lease_id": claims.LeaseID, "test_run_id": claims.TestRunID, "scenario_id": claims.ScenarioID,
		"fixture_variant": claims.FixtureVariant, "fixture_bundle_digest": claims.BundleDigest,
	}
	for key, value := range extra {
		fields[key] = value
	}
	return fields
}

func truncate(value []byte, maxBytes int) []byte {
	if maxBytes < 0 {
		return nil
	}
	if len(value) <= maxBytes {
		return value
	}
	return value[:maxBytes]
}

func max(left, right int) int {
	if left > right {
		return left
	}
	return right
}

func logEvent(level, event string, fields map[string]any) {
	entry := map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "level": level, "event": event, "service": "hci-sim"}
	for key, value := range fields {
		entry[key] = value
	}
	encoded, err := json.Marshal(entry)
	if err != nil {
		log.Printf("hci-sim log encode error: %v", err)
		return
	}
	log.Print(string(encoded))
}

func safeError(err error) string {
	if err == nil {
		return "invalid_user"
	}
	return err.Error()
}

func contentHash(value string) string {
	sum := sha256.Sum256([]byte(value))
	return fmt.Sprintf("%x", sum[:])
}
