package logcollect

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

// TestHashFilePrefixStrictRebuildsDigest 续传摘要必须等于全量摘要。
//
// 差一个字节的偏移，得到的哈希就与源包基线对不上；而这种错只会在 20 GiB 传完
// 后才暴露，现场代价是整个包白拉一遍。因此在进程内把"已有部分 + 续传部分"的
// 组合结果钉死。
func TestHashFilePrefixStrictRebuildsDigest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "resume.bin")
	if err := os.WriteFile(path, []byte("0123456789"), 0o600); err != nil {
		t.Fatal(err)
	}

	hasher := sha256.New()
	if err := hashFilePrefixStrict(path, 4, hasher); err != nil {
		t.Fatalf("重建已有部分摘要失败: %v", err)
	}
	// 模拟 tail -c +5 续传剩下的 6 字节。
	if _, err := hasher.Write([]byte("456789")); err != nil {
		t.Fatal(err)
	}

	want := sha256.Sum256([]byte("0123456789"))
	if got := hex.EncodeToString(hasher.Sum(nil)); got != hex.EncodeToString(want[:]) {
		t.Fatalf("续传摘要与全量摘要不一致: got=%s want=%x", got, want)
	}
}

// TestHashFilePrefixStrictRejectsShortFile 本地残包不足时必须报错。
//
// 静默接受短读会让摘要只覆盖已有字节，最终"校验通过"的是一个更小的包。
func TestHashFilePrefixStrictRejectsShortFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "short.bin")
	if err := os.WriteFile(path, []byte("abc"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := hashFilePrefixStrict(path, 10, sha256.New()); err == nil {
		t.Fatal("残包长度不足必须报错")
	}
}

// TestFileDigest 摘要实现必须与标准值一致，否则整条链的比对基准都是错的。
func TestFileDigest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sample.bin")
	if err := os.WriteFile(path, []byte("hello"), 0o600); err != nil {
		t.Fatal(err)
	}
	md5Sum, err := FileDigest(path, "md5")
	if err != nil || md5Sum != "5d41402abc4b2a76b9719d911017c592" {
		t.Fatalf("md5 不符: %q err=%v", md5Sum, err)
	}
	shaSum, err := FileDigest(path, "sha256")
	if err != nil || shaSum != "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" {
		t.Fatalf("sha256 不符: %q err=%v", shaSum, err)
	}
}
