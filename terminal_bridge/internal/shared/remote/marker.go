package remote

import (
	"fmt"
	"strings"
)

// marker.go —— 唯一设备标记协议（收口设计 §4.5，缺陷 B5）。
//
// 收口前桥里有四套设备 marker（ACLI_WRITE_*、ACP_*、SPC_*、delivery 自有），
// 三套 WS 回执格式，前端/relay/LC 各自解析。本文件把"设备回显行"这一层收成
// 唯一协议；WS 回执信封是另一层（reconcile/receipt.go），不在这里。
//
// 过渡期双协议：
//   - 新 marker → SYNC_{KIND}_{STATUS}（如 SYNC_CONFIG_OK、SYNC_PLUGIN_ERROR）
//   - 旧 marker → 原格式（__ACLI_WRITE_OK、SPC_WRITE_OK、ACP_RESULT_BEGIN …）
//   - 解析优先读新格式，读不到再降级读旧格式，并标注 Degraded=true
//
// **acli 写路径的标记字节不改**：__ACLI_WRITE / __ACLI_WRITE_OK /
// __ACLI_WRITE_FAIL 被 golden 测试逐字锁定（§八#13 验收分轨），改字节等于
// 让"命令没变"这条验收失去基准。因此 acli 写路径继续用 fs.go 里的旧标记，
// 由 Parse 的降级分支识别；新协议只用于 config/spc/plugin 新链路。

// Marker 状态枚举。
const (
	StatusOK       = "ok"
	StatusError    = "error"
	StatusProgress = "progress"
	StatusSkipped  = "skipped"
)

// Marker 是一次设备结论的结构化表达。
type Marker struct {
	Kind   string // acli | spc | config | plugin
	Status string // ok | error | progress | skipped
	// Fields 是标记行上的 key=value（reason/orig/sha/size/path …）。
	Fields map[string]string
	// Raw 是命中的原始行，供日志复核。
	Raw string
	// Degraded 为真表示结论来自旧格式降级解析，而非新协议。
	// 静默降级会让"协议已统一"变成不可验证的说法。
	Degraded bool
}

// Field 取标记字段，缺失返回空串。
func (m Marker) Field(key string) string {
	if m.Fields == nil {
		return ""
	}
	return m.Fields[key]
}

// splitFields 按空白切分标记行，但**单引号内的空白不切**。
//
// 为什么不能用 strings.Fields：Format 对含空格的值会加单引号（路径常带空格），
// 而 Fields 无视引号，会把 `path='/sf/a b.json'` 切成两段——字段静默丢一半，
// 且看起来"解析成功了"。切分必须与 ShellQuote 对称，并处理引号外的 `\'` 转义。
func splitFields(s string) []string {
	var out []string
	var cur strings.Builder
	inQuote := false
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, cur.String())
			cur.Reset()
		}
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '\\' && !inQuote {
			cur.WriteByte(c)
			if i+1 < len(s) {
				i++
				cur.WriteByte(s[i])
			}
			continue
		}
		if c == '\'' {
			inQuote = !inQuote
			cur.WriteByte(c)
			continue
		}
		if (c == ' ' || c == '\t') && !inQuote {
			flush()
			continue
		}
		cur.WriteByte(c)
	}
	flush()
	return out
}

// unquoteValue 还原被 ShellQuote 包裹的值（与 splitFields 同一套 POSIX 规则）。
//
// 不能用 strings.Trim(v, "'")：那会把 `'a'\”b'` 剥成 `a'\”b`，留下转义残渣。
func unquoteValue(v string) string {
	if !strings.HasPrefix(v, "'") {
		return v
	}
	var b strings.Builder
	inQuote := false
	for i := 0; i < len(v); i++ {
		c := v[i]
		if c == '\'' {
			inQuote = !inQuote
			continue
		}
		if c == '\\' && !inQuote {
			if i+1 < len(v) {
				i++
				b.WriteByte(v[i])
			}
			continue
		}
		b.WriteByte(c)
	}
	return b.String()
}

// Has 判断输出里是否出现过指定标记行（整行或行内子串）。
//
// 与旧 spcMarkerHas 同语义：按行扫描后做 Contains，避免 banner 里的
// 同名子串误命中（标记必须出现在独立回显行上）。
func Has(output, marker string) bool {
	for _, line := range strings.Split(StripANSI(output), "\n") {
		if strings.Contains(strings.TrimSpace(line), marker) {
			return true
		}
	}
	return false
}

// Field 从输出里取 key=value（旧协议 spcMarkerField 的同语义实现）。
//
// 只认独立成词的 key：`reason=x` 不应被 `noreason=y` 命中。
func Field(output, key string) string {
	for _, line := range strings.Split(StripANSI(output), "\n") {
		for _, token := range splitFields(strings.TrimSpace(line)) {
			if strings.HasPrefix(token, key+"=") {
				return unquoteValue(strings.TrimPrefix(token, key+"="))
			}
		}
	}
	return ""
}

// Format 生成新协议标记行：SYNC_{KIND}_{STATUS} key=value …
//
// kind/status 一律大写，字段按传入顺序输出（调用方负责稳定顺序，
// 便于 golden 对比）。值里含空格时用单引号包裹，避免被 Fields 切碎。
func Format(kind, status string, fields ...[2]string) string {
	var b strings.Builder
	fmt.Fprintf(&b, "SYNC_%s_%s", strings.ToUpper(kind), strings.ToUpper(status))
	for _, kv := range fields {
		value := kv[1]
		if strings.ContainsAny(value, " \t") {
			value = ShellQuote(value)
		}
		fmt.Fprintf(&b, " %s=%s", kv[0], value)
	}
	return b.String()
}

// legacyMarkers 是旧格式 → (kind, status) 的映射表。
//
// 只收录"结论性"标记；ACP_STATE_BEGIN/END 这类分隔符不是结论，
// 由调用方自己的分段解析处理，不进这张表。
var legacyMarkers = []struct {
	Token  string
	Kind   string
	Status string
}{
	{WriteOKMark, "acli", StatusOK},
	{WriteFailMark, "acli", StatusError},
	{ProbeMissingMark, "acli", StatusSkipped},
	{"SPC_PREPARE_OK", "spc", StatusOK},
	{"SPC_WRITE_OK", "spc", StatusOK},
	{"SPC_RESTORE_OK", "spc", StatusOK},
	{"SPC_RESTORE_SKIP", "spc", StatusSkipped},
	{"SPC_PREPARE_FAIL", "spc", StatusError},
	{"SPC_WRITE_FAIL", "spc", StatusError},
	{"SPC_RESTORE_FAIL", "spc", StatusError},
	{"ACP_ERROR=", "plugin", StatusError},
}

// Parse 从设备输出里提取结论标记。
//
// 优先新协议（SYNC_*），降级旧协议并标注 Degraded。取**最后一条**命中：
// 一次脚本可能先 progress 再 ok，最后的才是终态。
// 没有任何结论时返回零值 Marker（Kind 为空），调用方必须据此判"结论缺失"，
// 不得把"没读到标记"当成成功。
func Parse(kind, output string) Marker {
	clean := StripANSI(output)
	lines := strings.Split(clean, "\n")
	prefix := "SYNC_" + strings.ToUpper(kind) + "_"

	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		idx := strings.Index(line, prefix)
		if idx < 0 {
			continue
		}
		head := line[idx:]
		tokens := splitFields(head)
		status := strings.ToLower(strings.TrimPrefix(tokens[0], prefix))
		switch status {
		case StatusOK, StatusError, StatusProgress, StatusSkipped:
		default:
			continue
		}
		m := Marker{Kind: strings.ToLower(kind), Status: status, Raw: line, Fields: map[string]string{}}
		for _, token := range tokens[1:] {
			if eq := strings.Index(token, "="); eq > 0 {
				m.Fields[token[:eq]] = unquoteValue(token[eq+1:])
			}
		}
		return m
	}

	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		for _, legacy := range legacyMarkers {
			if legacy.Kind != strings.ToLower(kind) || !strings.Contains(line, legacy.Token) {
				continue
			}
			m := Marker{
				Kind: legacy.Kind, Status: legacy.Status, Raw: line,
				Degraded: true, Fields: map[string]string{},
			}
			for _, token := range splitFields(line) {
				if eq := strings.Index(token, "="); eq > 0 {
					m.Fields[token[:eq]] = unquoteValue(token[eq+1:])
				}
			}
			// 旧写入协议把原因写在标记后面（__ACLI_WRITE_FAIL size_mismatch），
			// 不是 key=value；补成 reason 字段，让上层判定口径一致。
			if legacy.Token == WriteFailMark {
				if rest := strings.TrimSpace(strings.TrimPrefix(line[strings.Index(line, WriteFailMark):], WriteFailMark)); rest != "" {
					m.Fields["reason"] = rest
				}
			}
			if legacy.Token == "ACP_ERROR=" {
				m.Fields["reason"] = strings.TrimPrefix(line[strings.Index(line, "ACP_ERROR="):], "ACP_ERROR=")
			}
			return m
		}
	}
	return Marker{}
}
