// Package metrics 提供无外部依赖的最小 Prometheus 指标端点。
package metrics

import (
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
)

type Metrics struct {
	ActiveSSHConnections atomic.Int64
	SSHConnectionsTotal  atomic.Uint64
	LeaseRejectTotal     atomic.Uint64
	CommandsTotal        atomic.Uint64
	CommandErrorsTotal   atomic.Uint64
	FixtureHitsTotal     atomic.Uint64
	FixtureMissesTotal   atomic.Uint64
	OverloadRejectsTotal atomic.Uint64
	InflightCommands     atomic.Int64
	QueueDepth           atomic.Int64
	StdoutBytesTotal     atomic.Uint64
	StderrBytesTotal     atomic.Uint64
}

func (m *Metrics) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		var b strings.Builder
		gauge(&b, "hci_sim_ssh_connections_active", m.ActiveSSHConnections.Load())
		counter(&b, "hci_sim_ssh_connections_total", m.SSHConnectionsTotal.Load())
		counter(&b, "hci_sim_lease_reject_total", m.LeaseRejectTotal.Load())
		counter(&b, "hci_sim_commands_total", m.CommandsTotal.Load())
		counter(&b, "hci_sim_command_errors_total", m.CommandErrorsTotal.Load())
		counter(&b, "hci_sim_fixture_hits_total", m.FixtureHitsTotal.Load())
		counter(&b, "hci_sim_fixture_misses_total", m.FixtureMissesTotal.Load())
		counter(&b, "hci_sim_overload_reject_total", m.OverloadRejectsTotal.Load())
		gauge(&b, "hci_sim_commands_inflight", m.InflightCommands.Load())
		gauge(&b, "hci_sim_queue_depth", m.QueueDepth.Load())
		counter(&b, "hci_sim_stdout_bytes_total", m.StdoutBytesTotal.Load())
		counter(&b, "hci_sim_stderr_bytes_total", m.StderrBytesTotal.Load())
		_, _ = w.Write([]byte(b.String()))
	})
}

func counter(b *strings.Builder, name string, value uint64) {
	fmt.Fprintf(b, "# TYPE %s counter\n%s %d\n", name, name, value)
}

func gauge(b *strings.Builder, name string, value int64) {
	fmt.Fprintf(b, "# TYPE %s gauge\n%s %d\n", name, name, value)
}
