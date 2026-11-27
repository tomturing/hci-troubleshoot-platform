package main

import (
	"sort"
	"testing"

	"terminal_bridge/internal/app"
	"terminal_bridge/internal/shared/models"
)

// R0c 路由表守卫：注册式路由取代中央 switch 后，「支持哪些消息类型」以路由表为
// 唯一事实源。本清单逐字冻结旧 switch 的全部 18 种 type（设计文档写 17 种是
// bridge_log_token 加入前的旧口径），任何一种丢失/改名/多余注册都在这里失败。
func TestWSRouteTableCoversAllMessageTypes(t *testing.T) {
	want := []string{
		"acli_sync",
		"bridge_log_token",
		"delivery_cancel",
		"delivery_resume",
		"delivery_status",
		"delivery_submit",
		"list_sessions",
		"log_collect",
		"pong",
		"resume",
		"sftp_op",
		"ssh_connect",
		"ssh_disconnect",
		"ssh_exec_command",
		"ssh_exec_process",
		"ssh_inject_command",
		"ssh_input",
		"ssh_resize",
		"vm_console_op",
	}
	routes := app.WSRoutesSnapshot()
	got := make([]string, 0, len(routes))
	for k := range routes {
		got = append(got, k)
	}
	sort.Strings(got)
	if len(got) != len(want) {
		t.Fatalf("路由表数量不符: want %d, got %d (%v)", len(want), len(got), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("路由表清单漂移: want %v, got %v", want, got)
		}
	}
	for _, typ := range want {
		if routes[typ] == nil {
			t.Fatalf("消息类型 %s 的 handler 为 nil", typ)
		}
	}
}

// 未知消息类型必须静默忽略（与旧 switch 无 default 分支的行为一致），
// 不得 panic、不得误派发到相近类型。
func TestWSRouteUnknownTypeIgnored(t *testing.T) {
	for _, typ := range []string{"", "unknown", "ssh_connect2", "SSH_INPUT", "ping"} {
		if h := app.LookupWSRoute(typ); h != nil {
			t.Fatalf("未知类型 %q 不应有路由", typ)
		}
	}
}

// 重复注册必须启动期 fail-fast：两个域争抢同一消息类型时不能静默覆盖。
func TestWSRouteDuplicateRegistrationPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("重复注册应 panic")
		}
	}()
	app.RegisterWSRoute("ssh_input", func(ctx *app.WSContext, msg models.InMessage) {})
}
