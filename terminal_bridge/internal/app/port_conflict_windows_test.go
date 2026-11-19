//go:build windows

package app

import (
	"net"
	"os"
	"syscall"
	"testing"
)

// Windows 侧端口占用判定以 Winsock errno 为准：
// 10048 是常规"地址已被占用"，10013 是"地址被独占/被系统保留"，两者都必须
// 进入端口自愈流程，否则现场表现为双击闪退（2026-11-19 客户机 127.0.0.1:9999）。
func TestIsAddressInUseWindowsErrno(t *testing.T) {
	cases := []struct {
		name  string
		errno syscall.Errno
		want  bool
	}{
		{"WSAEADDRINUSE", syscall.Errno(wsaErrAddrInUse), true},
		{"WSAEACCES", syscall.Errno(wsaErrAccess), true},
		{"WSAECONNREFUSED", syscall.Errno(10061), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := &net.OpError{
				Op:  "listen",
				Net: "tcp",
				Err: &os.SyscallError{Syscall: "bind", Err: tc.errno},
			}
			if got := isAddressInUse(err); got != tc.want {
				t.Fatalf("isAddressInUse(%v)=%v, 期望 %v", err, got, tc.want)
			}
		})
	}
}
