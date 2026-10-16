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
}

// TestWrapContainerCommandSimulationUnwrapsDispatcher 守护容器步骤仿真闭环：
// 服务端（agent-service container_exec.py）已把容器命令包成自包含派发壳
// （HCI_CONTAINER=...; HCI_USER_COMMAND='<真实命令>'; ...case...esac），
// 若 bridge 再按 container 名 container_exec 二次包裹，hci-sim 只会看到 tool=container_exec
// 的畸形 argv → fixture_not_found(exit 127)。故仿真会话必须解出 HCI_USER_COMMAND 内层裸命令。
func TestWrapContainerCommandSimulationUnwrapsDispatcher(t *testing.T) {
	dispatcher := "HCI_CONTAINER='vs-cp-manager'; HCI_CTR_NS='k8s.io'; HCI_USER_COMMAND='smartctl.real -a /dev/sdb'; export HCI_CONTAINER HCI_CTR_NS HCI_USER_COMMAND; HCI_RUNTIME=$(sh -lc 'printf container_exec'); case \"$HCI_RUNTIME\" in container_exec) exec container_exec -n \"$HCI_CONTAINER\" -c \"$HCI_USER_COMMAND\" -d ;; esac"

	got := wrapContainerCommand(dispatcher, "vs-cp-manager", true)
	if got != "smartctl.real -a /dev/sdb" {
		t.Fatalf("仿真会话应解出派发壳内层裸命令, got=%q", got)
	}
	if strings.ContainsAny(got, ";|&<>()") {
		t.Fatalf("解出的内层命令不应含 shell 操作符（否则撞 hci-sim 受限 lexer）, got=%q", got)
	}
}

// TestWrapContainerCommandRealContainerStillWraps 守护真机容器语义不被仿真分支污染：
// 真机会话（simulation=false）容器名非 host 时仍走 container_exec 包裹。
func TestWrapContainerCommandRealContainerStillWraps(t *testing.T) {
	ctr := wrapContainerCommand("acli --version", "vs-cp-manager", false)
	if !strings.Contains(ctr, "container_exec -n vs-cp-manager") {
		t.Fatalf("真机容器分支应走 container_exec 包装, got=%q", ctr)
	}
}

// TestScanFirstShellToken 覆盖内层用户命令解析的边界：相邻引号段、shlex 转义单引号、
// 双引号内转义、反斜杠转义操作符、以及顶层操作符即词尾。
func TestScanFirstShellToken(t *testing.T) {
	cases := []struct {
		in   string
		want string
		ok   bool
	}{
		{"'smartctl.real -a /dev/sdb'; export", "smartctl.real -a /dev/sdb", true},
		{`'a'\''b' ; next`, `a'b`, true},        // shlex 转义单引号
		{`"grep x\ty" tail`, `grep x\ty`, true}, // 双引号内非特殊转义保留反斜杠
		{`\;literal next`, ";literal", true},    // 反斜杠转义操作符为字面量
		{"bare-arg next", "bare-arg", true},     // 裸词遇空白结束
		{";leading", "", false},                 // 顶层操作符起头 → 空 token
		{"   ", "", false},                      // 全空白
	}
	for _, c := range cases {
		got, ok := scanFirstShellToken(c.in)
		if ok != c.ok || got != c.want {
			t.Errorf("scanFirstShellToken(%q)=(%q,%v) 期望(%q,%v)", c.in, got, ok, c.want, c.ok)
		}
	}
}
