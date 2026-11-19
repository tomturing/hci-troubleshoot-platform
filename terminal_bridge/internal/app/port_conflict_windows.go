//go:build windows

package app

import (
	"errors"
	"syscall"
)

// Windows 上与"该地址当前不可绑定"相关的 Winsock 错误码。
// 这里用数值而不是 syscall 里的符号常量，是为了避免不同 Go 版本对 Winsock
// 符号的命名差异（WSAEADDRINUSE 与 EADDRINUSE 并非在所有版本/GOOS 下都同时存在）。
const (
	wsaErrAddrInUse uintptr = 10048 // WSAEADDRINUSE：地址已被占用
	wsaErrAccess    uintptr = 10013 // WSAEACCES：地址被独占绑定或落在系统保留段，拒绝绑定
)

// isPortBindUnavailableErrno 从错误链里取 Winsock errno，判断是否为"端口不可绑定"。
//
// 为什么必须走 errno：bind 的错误文案来自 FormatMessage，随系统语言变化，
// 中文 Windows 上英文子串匹配必定落空；而 errno 是跨语言稳定的唯一事实来源。
// 10013 的典型成因：占用者用 SO_EXCLUSIVEADDRUSE 独占绑定、端口被 http.sys 注册，
// 或端口落在 Hyper-V/WinNAT 动态保留段（netsh int ipv4 show excludedportrange protocol=tcp）。
func isPortBindUnavailableErrno(err error) bool {
	var errno syscall.Errno
	if !errors.As(err, &errno) {
		return false
	}
	return uintptr(errno) == wsaErrAddrInUse || uintptr(errno) == wsaErrAccess
}
