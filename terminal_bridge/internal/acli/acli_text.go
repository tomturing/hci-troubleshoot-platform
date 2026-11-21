package acli

// 域内私有小工具。阶段 2 A3 之前它们从 logcollect 借用，等于让安装域反向依赖采集域。
//
// 设计文档 §3.5 的判据："删掉某函数若会让链路产生安全/正确性风险，它就不是 util，
// 禁止进任何工具包"——errString 的 nil 安全属于正确性，所以既不进工具包，
// 也不该由业务域互相借用；各域保留私有副本（Go 谚语 "a little copying is better
// than a little dependency"）。

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
