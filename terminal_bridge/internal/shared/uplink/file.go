package uplink

// 本地文件与平台地址的校验原语（云面方向）。
//
// 阶段 2 A3 之前它们住在 logcollect：delivery 交付作业要算本地副本摘要、
// 要校验平台地址，于是交付域反向依赖采集域。两者都不认识节点，
// 属于云面/本地文件面原语，因此归 uplink。

import (
	"crypto/md5"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"hash"
	"io"
	"net/url"
	"os"
	"strings"
)

// ValidateURL 只接受 http/https 且必须带 host，避免 file:// 等协议被用于读本地。
func ValidateURL(raw string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(raw), "/")
	if trimmed == "" {
		return "", fmt.Errorf("platform_url 不能为空")
	}
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return "", fmt.Errorf("platform_url 解析失败: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("platform_url 仅支持 http/https")
	}
	if parsed.Host == "" {
		return "", fmt.Errorf("platform_url 缺少主机名")
	}
	return trimmed, nil
}

// FileDigest 计算整文件摘要。algo 只认 md5 与 sha256；未知值按 sha256 处理，
// 避免调用方传错算法时静默退回一个更弱的校验。
func FileDigest(path, algo string) (string, error) {
	var hasher hash.Hash
	if algo == "md5" {
		hasher = md5.New()
	} else {
		hasher = sha256.New()
	}
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	if _, err := io.Copy(hasher, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}
