//go:build !windows

package main

import (
	"net"
	"os"
	"syscall"
	"testing"
)

// Unix 侧端口占用判定以 errno 为准：即使错误链被包装、文案被改写，
// EADDRINUSE 也必须被识别为"端口不可用"，否则自愈链路整体失效。
func TestIsAddressInUseUnixErrno(t *testing.T) {
	wrapped := &net.OpError{
		Op:  "listen",
		Net: "tcp",
		Err: &os.SyscallError{Syscall: "bind", Err: syscall.EADDRINUSE},
	}
	if !isAddressInUse(wrapped) {
		t.Fatalf("EADDRINUSE 必须判定为端口不可用: %v", wrapped)
	}

	// 非占用类 errno 不得被误判，否则"绑不上"会被无谓地回退到备用端口。
	refused := &net.OpError{
		Op:  "listen",
		Net: "tcp",
		Err: &os.SyscallError{Syscall: "bind", Err: syscall.EADDRNOTAVAIL},
	}
	if isAddressInUse(refused) {
		t.Fatalf("EADDRNOTAVAIL 不应判定为端口不可用: %v", refused)
	}
}
