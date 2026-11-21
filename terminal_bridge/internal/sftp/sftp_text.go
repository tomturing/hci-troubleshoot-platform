package sftp

// 域内私有小工具：从 logcollect 借用会让下载域反向依赖采集域（阶段 2 A3）。
// 判据与取舍见设计文档 §3.5。

import "strings"

// firstNonEmpty 返回第一个 trim 后非空的值。
func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
