package main

import (
	"strings"
	"testing"
)

// TestWrapContainerCommandSimulationSkipsPathPrefix 守护 sim-ssh 采集闭环关键修复：
// 真机非交互 SSH 靠 `export PATH=...:/sf/bin:/sf/sbin; ` 前缀找到 acli/smartctl，
// 但该前缀含 shell 操作符 ';'，会被 hci-sim 受限 lexer 拒绝 → policy_denied(exit 126)，
// 使仿真会话下所有 acli/smartctl 命令全线失败。故仿真会话（simulation=true）必须下发裸命令。
func TestWrapContainerCommandSimulationSkipsPathPrefix(t *testing.T) {
	const hostCmd = "acli --formatter json alert get -k 磁盘"

	// 真机会话：保留 PATH 前缀（既有行为不可回归）。
	realWrap := wrapContainerCommand(hostCmd, "host", false)
	if realWrap != `export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin"; `+hostCmd {
		t.Fatalf("真机会话应加 PATH 前缀, got=%q", realWrap)
	}

	// 仿真会话：必须原样裸命令，绝不含 ';'（否则撞 hci-sim policy_denied）。
	simWrap := wrapContainerCommand(hostCmd, "host", true)
	if simWrap != hostCmd {
		t.Fatalf("仿真会话应跳过 PATH 前缀, got=%q", simWrap)
	}

	// smartctl 同样受仿真豁免（覆盖 acli/smartctl 两类工具）。
	sc := wrapContainerCommand("smartctl.real -a /dev/sdb", "", true)
	if sc != "smartctl.real -a /dev/sdb" {
		t.Fatalf("仿真会话 smartctl 应裸命令, got=%q", sc)
	}

	// 非 host 容器分支不受 simulation 影响：仍包 container_exec（真实容器语义）。
	ctr := wrapContainerCommand("acli --version", "vs-cp-manager", true)
	if !strings.Contains(ctr, "container_exec") {
		t.Fatalf("容器分支应走 container_exec 包装, got=%q", ctr)
	}
}
