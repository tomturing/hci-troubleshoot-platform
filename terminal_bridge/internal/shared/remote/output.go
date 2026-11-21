package remote

// 命令输出的呈现原语：编码修复 + 头尾保留式截断。
//
// 为什么归 remote 而不是 terminal：它处理的是"节点上命令返回的原始字节"，
// 输入输出都是字符串，与终端会话、VM 控制台、PTY 都没有关系。原先住在
// terminal，导致 acli（4 处）与 logcollect（1 处）为了做一个诊断预览而
// 反向依赖终端域——`* -> terminal` 这条边的全部成因就是这两个函数。

import (
	"fmt"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
	"golang.org/x/text/transform"
)

// NormalizeOutputText 把命令输出修成合法 UTF-8。
//
// HCI 节点上的部分程序（老版本命令、locale 未设的 wrapper）会直接吐 GBK/GB18030
// 字节，json 编码后前端只能看到乱码或替换符；而合法 UTF-8 必须原样返回——
// 再走一遍解码会把合法的多字节序列当成 GB18030 二次解释。
func NormalizeOutputText(raw string) string {
	if raw == "" || utf8.ValidString(raw) {
		return raw
	}
	decoded, _, err := transform.String(simplifiedchinese.GB18030.NewDecoder(), raw)
	if err != nil || decoded == "" {
		return strings.ToValidUTF8(raw, "\uFFFD")
	}
	return decoded
}

// truncateKeepTail 头尾都保留：安装/校验的结论（成功与否、节点数、失败原因）都在输出尾部，
// 只截头部恰好丢掉最需要的信息。
func truncateKeepTail(s string, headBytes, tailBytes int) string {
	if headBytes < 0 || tailBytes < 0 || len(s) <= headBytes+tailBytes {
		return s
	}
	head := s[:headBytes]
	for len(head) > 0 && !utf8.ValidString(head) {
		head = head[:len(head)-1]
	}
	tail := s[len(s)-tailBytes:]
	for len(tail) > 0 && !utf8.ValidString(tail) {
		tail = tail[1:]
	}
	return head + fmt.Sprintf("\n…[省略 %d 字节]…\n", len(s)-len(head)-len(tail)) + tail
}

// FormatOutputPreview 预览统一出口：先修编码，再按头尾保留截断。
func FormatOutputPreview(raw string, headBytes, tailBytes int) string {
	return truncateKeepTail(NormalizeOutputText(raw), headBytes, tailBytes)
}
