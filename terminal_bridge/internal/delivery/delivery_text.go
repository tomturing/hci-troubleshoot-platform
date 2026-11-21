package delivery

// 域内私有小工具。阶段 2 A3 之前这三者从 logcollect 借用，交付域因此反向依赖采集域。
//
// 设计文档 §3.5 的判据："删掉某函数若会让链路产生安全/正确性风险，它就不是 util，
// 禁止进任何工具包"；各域保留私有副本（Go 谚语 "a little copying is better than
// a little dependency"）。

import "strings"

// errString 渲染错误为事件字段；nil 返回空串而不是 "nil" 或 panic。
func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// firstNonEmpty 返回第一个 trim 后非空的值。
func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

// truncateForDiag 截短诊断文本，避免整段终端噪音进日志。
func truncateForDiag(text string, limit int) string {
	trimmed := strings.TrimSpace(text)
	if len(trimmed) <= limit {
		return trimmed
	}
	return trimmed[:limit] + "..."
}
