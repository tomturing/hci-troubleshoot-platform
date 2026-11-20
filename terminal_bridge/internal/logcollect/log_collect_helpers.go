package logcollect

// 跨域共享小工具：错误串渲染与首个非空值挑选（acli/logcollect/delivery/sftp 共用）。

import "strings"

func ErrString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func FirstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
