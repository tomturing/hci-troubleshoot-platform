package main

import (
	"fmt"
	"strings"
)

// CommandFingerprint 是命令的规范化表示（方案 6.2）。
// 不用 strings.Contains 匹配命令，而是解析成结构化字段后再路由。
type CommandFingerprint struct {
	Raw             string
	Tool            string // log / system / service / vm ... （对应 qfk_<tool>）
	ResourceKeyword string
	Path            string
	Host            string
	Container       string
	PolicyVersion   string
	Args            map[string]string
}

// AcquisitionKey 返回归一化的 acquisition key（方案 6.2 路由键之一）。
func (fp CommandFingerprint) AcquisitionKey() string {
	if fp.Tool == "" {
		return ""
	}
	return "qfk_" + fp.Tool
}

// Canonical 返回用于 fixture 匹配的规范化字符串。
func (fp CommandFingerprint) Canonical() string {
	return fmt.Sprintf("tool=%s;acq=%s;keyword=%s;path=%s;host=%s;container=%s",
		fp.Tool, fp.AcquisitionKey(), fp.ResourceKeyword, fp.Path, fp.Host, fp.Container)
}

// ParseCommand 解析一条 shell 命令为 canonical fingerprint。
// 目前支持 `acli <tool> get [--key value|--key=value ...]` 形式（P0 最小解析器）。
func ParseCommand(raw string) CommandFingerprint {
	fp := CommandFingerprint{Raw: strings.TrimSpace(raw), Args: map[string]string{}}
	tokens := tokenizeQuoted(raw)

	if len(tokens) >= 2 && tokens[0] == "acli" {
		fp.Tool = strings.TrimPrefix(strings.TrimPrefix(tokens[1], "qfk_"), "qfk")
	}

	for i := 0; i < len(tokens); i++ {
		t := tokens[i]
		if !strings.HasPrefix(t, "-") {
			continue
		}
		key := strings.TrimLeft(t, "-")
		var val string
		if eq := strings.Index(t, "="); eq >= 0 {
			key = strings.TrimLeft(t[:eq], "-")
			val = t[eq+1:]
		} else if i+1 < len(tokens) {
			val = tokens[i+1]
			i++
		}
		switch key {
		case "keyword", "resource-keyword", "resource_keyword":
			fp.ResourceKeyword = val
		case "path":
			fp.Path = val
		case "host", "node", "node-ip":
			fp.Host = val
		case "container":
			fp.Container = val
		case "policy-version", "policy_version":
			fp.PolicyVersion = val
		default:
			fp.Args[key] = val
		}
	}
	return fp
}

// tokenizeQuoted 按空白分词，但尊重双/单引号内的空格。
func tokenizeQuoted(s string) []string {
	var tokens []string
	var cur strings.Builder
	inQuote := false
	var quoteChar byte
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case inQuote:
			if c == quoteChar {
				inQuote = false
			} else {
				cur.WriteByte(c)
			}
		case c == '"' || c == '\'':
			inQuote = true
			quoteChar = c
		case c == ' ' || c == '\t' || c == '\n' || c == '\r':
			if cur.Len() > 0 {
				tokens = append(tokens, cur.String())
				cur.Reset()
			}
		default:
			cur.WriteByte(c)
		}
	}
	if cur.Len() > 0 {
		tokens = append(tokens, cur.String())
	}
	return tokens
}
