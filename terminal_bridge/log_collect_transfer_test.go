package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestNormalizeArchivePath(t *testing.T) {
	cases := []struct {
		name    string
		input   string
		want    string
		wantErr bool
	}{
		{"合法路径", "/sf/data/local/acli/tmp/data/log_collect/Q20261022001.zip", "/sf/data/local/acli/tmp/data/log_collect/Q20261022001.zip", false},
		{"support 前缀还原", "/support/tmp/data/log_collect/Q20261022001.zip", "/sf/data/local/acli/tmp/data/log_collect/Q20261022001.zip", false},
		{"空路径", "  ", "", true},
		{"目录穿越", "/sf/data/local/acli/tmp/data/log_collect/../../etc/passwd", "", true},
		{"非 zip", "/sf/data/local/acli/tmp/data/log_collect/Q1.tar", "", true},
		{"shell 注入", "/sf/data/local/acli/tmp/data/log_collect/Q1.zip;rm -rf /", "", true},
		{"任意目录", "/etc/passwd.zip", "", true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizeArchivePath(tc.input)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("期望报错，实际通过：%s", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("意外错误：%v", err)
			}
			if got != tc.want {
				t.Fatalf("归一化结果不符：期望 %s，实际 %s", tc.want, got)
			}
		})
	}
}

func TestValidatePlatformURL(t *testing.T) {
	if _, err := validatePlatformURL("https://hci.example.com/"); err != nil {
		t.Fatalf("合法 https 被拒：%v", err)
	}
	if got, _ := validatePlatformURL("https://hci.example.com/"); got != "https://hci.example.com" {
		t.Fatalf("尾部斜杠未去除：%s", got)
	}
	for _, bad := range []string{"", "file:///etc/passwd", "ftp://host", "https://"} {
		if _, err := validatePlatformURL(bad); err == nil {
			t.Fatalf("非法地址未拦截：%s", bad)
		}
	}
}

func TestValidUploadedParts(t *testing.T) {
	got := validUploadedParts([]int{1, 3, 3, 999, 0, -1}, 5)
	if len(got) != 2 || !got[1] || !got[3] {
		t.Fatalf("越界与重复未被正确过滤：%v", got)
	}
}

func TestFileSHA256(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a.zip")
	payload := []byte("hello-log-collect")
	if err := os.WriteFile(path, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	want := sha256.Sum256(payload)
	got, err := fileSHA256(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != hex.EncodeToString(want[:]) {
		t.Fatalf("校验和不符：%s", got)
	}
}

func TestUploadArchivePartsSkipsUploadedAndRetries(t *testing.T) {
	var mu sync.Mutex
	received := map[string][]byte{}
	headers := map[string]string{}
	attempts := map[string]int{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		index := r.URL.Path

		mu.Lock()
		attempts[index]++
		attempt := attempts[index]
		// 第 2 片首次返回 500，验证重试
		if index == "/api/direct/log-collect-uploads/task-1/parts/2" && attempt == 1 {
			mu.Unlock()
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		received[index] = body
		headers[index] = r.Header.Get("X-Upload-Token") + "|" + r.Header.Get("X-Part-SHA256")
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	dir := t.TempDir()
	path := filepath.Join(dir, "a.zip")
	payload := []byte("0123456789abcdefghij") // 20 字节
	if err := os.WriteFile(path, payload, 0o600); err != nil {
		t.Fatal(err)
	}

	var progress []int
	err := uploadArchiveParts(
		context.Background(), server.URL, "task-1", "token-abc", path,
		8, 3, int64(len(payload)), map[int]bool{1: true},
		func(done int) { progress = append(progress, done) },
	)
	if err != nil {
		t.Fatalf("上传失败：%v", err)
	}

	// 第 1 片已传，跳过；只应上传第 2、3 片
	if _, ok := received["/api/direct/log-collect-uploads/task-1/parts/1"]; ok {
		t.Fatal("已传分片被重复上传")
	}
	if string(received["/api/direct/log-collect-uploads/task-1/parts/2"]) != "89abcdef" {
		t.Fatalf("第 2 片内容不符：%q", received["/api/direct/log-collect-uploads/task-1/parts/2"])
	}
	if string(received["/api/direct/log-collect-uploads/task-1/parts/3"]) != "ghij" {
		t.Fatalf("末片内容不符：%q", received["/api/direct/log-collect-uploads/task-1/parts/3"])
	}
	if attempts["/api/direct/log-collect-uploads/task-1/parts/2"] != 2 {
		t.Fatalf("500 未触发重试，实际次数 %d", attempts["/api/direct/log-collect-uploads/task-1/parts/2"])
	}
	if len(progress) != 2 || progress[0] != 2 || progress[1] != 3 {
		t.Fatalf("进度回调不符：%v", progress)
	}

	wantSHA := sha256.Sum256([]byte("89abcdef"))
	wantHeader := "token-abc|" + hex.EncodeToString(wantSHA[:])
	if headers["/api/direct/log-collect-uploads/task-1/parts/2"] != wantHeader {
		t.Fatalf("请求头不符：%s", headers["/api/direct/log-collect-uploads/task-1/parts/2"])
	}
}

func TestUploadArchivePartsFailsAfterRetries(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
	}))
	defer server.Close()

	dir := t.TempDir()
	path := filepath.Join(dir, "a.zip")
	if err := os.WriteFile(path, []byte("01234567"), 0o600); err != nil {
		t.Fatal(err)
	}

	err := uploadArchiveParts(context.Background(), server.URL, "task-2", "t", path,
		8, 1, 8, map[int]bool{}, func(int) {})
	if err == nil {
		t.Fatal("持续失败未返回错误")
	}
	if want := "分片 1 上传失败"; len(err.Error()) < len(want) || err.Error()[:len(want)] != want {
		t.Fatalf("错误信息不符：%v", err)
	}
}

func TestPutPartSendsExpectedHeaders(t *testing.T) {
	var gotToken, gotSHA, gotBody string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		gotBody = string(body)
		gotToken = r.Header.Get("X-Upload-Token")
		gotSHA = r.Header.Get("X-Part-SHA256")
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	sum := sha256.Sum256([]byte("payload"))
	err := putPart(context.Background(), server.Client(), server.URL+"/parts/1",
		"tok", hex.EncodeToString(sum[:]), []byte("payload"))
	if err != nil {
		t.Fatalf("putPart 失败：%v", err)
	}
	if gotToken != "tok" || gotSHA != hex.EncodeToString(sum[:]) || gotBody != "payload" {
		t.Fatalf("请求不符合预期：token=%s sha=%s body=%s", gotToken, gotSHA, gotBody)
	}
}
