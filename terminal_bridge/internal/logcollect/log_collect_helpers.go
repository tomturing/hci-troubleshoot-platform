package logcollect

// 域内私有小工具：错误串渲染与首个非空值挑选。
//
// 为什么不再导出（阶段 2 A3）：这两个函数原先被 acli/delivery/sftp 反向借用，
// 使 logcollect 长得像公共库。设计文档 §3.5 的判据是"删掉某函数若会让链路产生
// 安全/正确性风险，它就不是 util，禁止进任何工具包"——errString 的 nil 安全
// 属于正确性，因此既不该进工具包，也不该由业务域互相借用。
// 各消费域改为本地私有副本（Go 谚语："a little copying is better than a little dependency"）。
//
// 保留在本包是因为采集编排内部有 40 余处使用，抽走只会增加噪音。

import "strings"

// errString 渲染错误为事件字段；nil 返回空串而不是 "nil" 或 panic。
func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// firstNonEmpty 返回第一个 trim 后非空的值，全部为空时返回空串。
func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
