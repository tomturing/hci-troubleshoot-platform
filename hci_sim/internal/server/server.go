// Package server 实现受控的多租户 SSH 仿真数据面。
package server

import (
	"bufio"
	"context"
	"crypto/sha256"
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
)

var allowedEnv = map[string]struct{}{
	"TRACEPARENT": {}, "TRACESTATE": {}, "HTP_EXEC_ID": {}, "HTP_TEST_RUN_ID": {},
	"HTP_NODE_IP": {}, "HTP_CONTAINER": {},
}

type Config struct {
	ListenAddress string
	HostSigner    ssh.Signer
	LeaseSecret   []byte
	Router        *fixture.Router
	Workers       int
	QueueSize     int
	Metrics       *metrics.Metrics
}

type Server struct {
	config     Config
	sshConfig  *ssh.ServerConfig
	tracker    *lease.Tracker
	jobs       chan commandJob
	workerWG   sync.WaitGroup
	listenerMu sync.Mutex
	listener   net.Listener
}

type commandJob struct {
	ctx     context.Context
	channel ssh.Channel
	claims  lease.Claims
	env     map[string]string
	command string
	done    chan struct{}
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
	if config.ListenAddress == "" || config.HostSigner == nil || len(config.LeaseSecret) < 32 || config.Router == nil {
		return nil, errors.New("hci-sim server 配置不完整")
	}
	if config.Workers < 1 || config.Workers > 256 {
		return nil, errors.New("worker 数必须在 1-256")
	}
	if config.QueueSize < 1 || config.QueueSize > 10000 {
		return nil, errors.New("queue size 必须在 1-10000")
	}
	if config.Metrics == nil {
		config.Metrics = &metrics.Metrics{}
	}
	s := &Server{
		config:  config,
		tracker: lease.NewTracker(),
		jobs:    make(chan commandJob, config.QueueSize),
	}
	s.sshConfig = &ssh.ServerConfig{
		PasswordCallback: func(meta ssh.ConnMetadata, password []byte) (*ssh.Permissions, error) {
			claims, err := lease.Validate(config.LeaseSecret, string(password), config.Router.ManifestHash(), time.Now())
			if err != nil || meta.User() != "sim" {
				config.Metrics.LeaseRejectTotal.Add(1)
				logEvent("WARN", "lease.rejected", map[string]any{"remote": meta.RemoteAddr().String(), "reason": safeError(err)})
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
	logEvent("INFO", "server.started", map[string]any{
		"address": s.config.ListenAddress, "workers": s.config.Workers,
		"queue_size": s.config.QueueSize, "fixture_manifest_hash": s.config.Router.ManifestHash(),
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
	release, err := s.tracker.AcquireSession(claims)
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
			s.submit(commandJob{ctx: ctx, channel: channel, claims: claims, env: cloneMap(env), command: payload.Command, done: make(chan struct{})})
			return
		default:
			_ = request.Reply(false, nil)
		}
	}
	_ = channel.Close()
}

func (s *Server) submit(job commandJob) {
	select {
	case s.jobs <- job:
		s.config.Metrics.QueueDepth.Add(1)
		<-job.done
	default:
		s.config.Metrics.OverloadRejectsTotal.Add(1)
		s.writeFailure(job.channel, exitOverloaded, "sim_overloaded", "hci-sim 执行队列已满")
	}
}

func (s *Server) worker(workerID int) {
	defer s.workerWG.Done()
	for job := range s.jobs {
		s.config.Metrics.QueueDepth.Add(-1)
		s.config.Metrics.InflightCommands.Add(1)
		s.execute(job, workerID)
		s.config.Metrics.InflightCommands.Add(-1)
		close(job.done)
	}
}

func (s *Server) execute(job commandJob, workerID int) {
	defer job.channel.Close()
	s.config.Metrics.CommandsTotal.Add(1)
	if err := s.tracker.ConsumeCommand(job.claims); err != nil {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		s.writeFailure(job.channel, exitPolicyDenied, "lease_quota_exceeded", err.Error())
		return
	}
	result, err := s.config.Router.Match(job.command, job.claims.FixtureVariant)
	if err != nil {
		s.config.Metrics.CommandErrorsTotal.Add(1)
		if err.Error() == "fixture_not_found" {
			s.config.Metrics.FixtureMissesTotal.Add(1)
			s.writeFailure(job.channel, exitFixtureNotFound, "fixture_not_found", "未发布的命令 fixture")
		} else {
			s.writeFailure(job.channel, exitPolicyDenied, "policy_denied", err.Error())
		}
		return
	}
	s.config.Metrics.FixtureHitsTotal.Add(1)
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
			attribute.String("fixture.manifest_hash", job.claims.FixtureManifestHash),
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
	if result.DelayMS > 0 {
		select {
		case <-time.After(time.Duration(result.DelayMS) * time.Millisecond):
		case <-job.ctx.Done():
			span.RecordError(job.ctx.Err())
			span.SetStatus(codes.Error, "cancelled")
			return
		}
	}
	stdoutBytes, stdoutErr := writeChunks(spanCtx, job.channel, []byte(result.Stdout), result.ChunkBytes)
	stderrBytes, stderrErr := writeChunks(spanCtx, job.channel.Stderr(), []byte(result.Stderr), result.ChunkBytes)
	s.config.Metrics.StdoutBytesTotal.Add(uint64(stdoutBytes))
	s.config.Metrics.StderrBytesTotal.Add(uint64(stderrBytes))
	exitCode := result.ExitCode
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
	_, _ = job.channel.SendRequest("exit-status", false, ssh.Marshal(exitStatus{Status: uint32(exitCode)}))
	logEvent("INFO", "exec.done", baseFields(job.claims, map[string]any{
		"trace_id": span.SpanContext().TraceID().String(), "exec_id": job.env["HTP_EXEC_ID"],
		"fixture_id": result.FixtureID, "exit_code": exitCode,
		"stdout_bytes": stdoutBytes, "stderr_bytes": stderrBytes,
		"stdout_sha256": contentHash(result.Stdout), "stderr_sha256": contentHash(result.Stderr),
		"duration_ms": time.Since(start).Milliseconds(),
	}))
}

func (s *Server) writeFailure(channel ssh.Channel, code int, kind, message string) {
	defer channel.Close()
	_, _ = io.WriteString(channel.Stderr(), fmt.Sprintf("hci-sim: %s: %s\n", kind, message))
	_, _ = channel.SendRequest("exit-status", false, ssh.Marshal(exitStatus{Status: uint32(code)}))
}

var markerPattern = regexp.MustCompile(`(__HCI_DONE_[A-Za-z0-9_]+__)`)

// controlledShell 只服务 Browser 初始连接和环境采集，不执行系统 shell。
func (s *Server) controlledShell(ctx context.Context, channel ssh.Channel, claims lease.Claims) {
	defer channel.Close()
	_, _ = io.WriteString(channel, fmt.Sprintf("HCI-SIM KBD %s / scenario %s\r\nsim@hci-sim$ ", s.config.Router.KBD().SupportID, claims.ScenarioID))
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
		exitCode := 0
		stdout := ""
		stderr := ""
		if strings.HasPrefix(command, "command -v acli ") {
			stdout = "__HCI_ACLI_OK__"
		} else if err := s.tracker.ConsumeCommand(claims); err != nil {
			exitCode, stderr = exitPolicyDenied, err.Error()
		} else if result, err := s.config.Router.Match(command, claims.FixtureVariant); err != nil {
			if err.Error() == "fixture_not_found" {
				exitCode, stderr = exitFixtureNotFound, "hci-sim: fixture_not_found"
			} else {
				exitCode, stderr = exitPolicyDenied, "hci-sim: "+err.Error()
			}
		} else {
			exitCode, stdout, stderr = result.ExitCode, result.Stdout, result.Stderr
		}
		if stdout != "" {
			_, _ = io.WriteString(channel, strings.ReplaceAll(stdout, "\n", "\r\n"))
		}
		if stderr != "" {
			_, _ = io.WriteString(channel, strings.ReplaceAll(stderr, "\n", "\r\n"))
		}
		if marker != "" {
			_, _ = fmt.Fprintf(channel, "\r\n%s:%d\r\n", marker, exitCode)
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

func writeChunks(ctx context.Context, writer io.Writer, value []byte, chunkSize int) (int, error) {
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
		"fixture_variant": claims.FixtureVariant, "fixture_manifest_hash": claims.FixtureManifestHash,
	}
	for key, value := range extra {
		fields[key] = value
	}
	return fields
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
