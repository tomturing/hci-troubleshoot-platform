//go:build !windows

package main

import (
	"errors"
	"syscall"
)

// isPortBindUnavailableErrno 判断 bind 失败是否属于"端口被占用"。
//
// Unix 侧本来就靠 errno 判定，但既有实现只匹配错误文案；一旦 errno 被上层包装成
// 自定义字符串（或文案被改写），判定就会失效。这里以 errno 为准、文案兜底。
// 只认 EADDRINUSE：EACCES（非 root 绑定特权端口）在 bridge 的端口取值域内不会出现，
// 真出现属于配置错误，应 fail-fast 让现场暴露，而不是悄悄换端口。
func isPortBindUnavailableErrno(err error) bool {
	return errors.Is(err, syscall.EADDRINUSE)
}
