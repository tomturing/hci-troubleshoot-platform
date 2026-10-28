package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// 这一组测试只覆盖「bridge → 平台」的字节搬运，不涉及 SSH、不涉及 acli。
//
// 为什么这样切：交付卡在现场的那一步永远是"字节没能从客户机写到平台"，
// 而它只依赖三样东西——本地已落地的包、平台会话参数、能连上平台的 HTTP 客户端。
// 三者都可以在进程内构造，因此这一段是**可以完整验证**的；
// 把它和 SSH/采集搅在一起测，现场一旦失败就分不清是证书、网络还是节点侧。

// fakeUploadPlatform 模拟平台的数据面：逐片接收、按序校验、最后收 complete。
//
// 用 httptest.NewTLSServer 而不是 NewServer：它的证书由临时自签 CA 签发，
// 系统根证书库必然不认，正好一比一复刻客户内网平台的表现。
type fakeUploadPlatform struct {
	server   *httptest.Server
	taskID   string
	token    string
	mu       sync.Mutex
	parts    map[int][]byte
	complete bool
	partSize int64
}

func newFakeUploadPlatform(t *testing.T, taskID, token string) *fakeUploadPlatform {
	t.Helper()
	fake := &fakeUploadPlatform{taskID: taskID, token: token, parts: make(map[int][]byte)}
	prefix := logCollectDirectUploadPrefix + "/" + taskID

	mux := http.NewServeMux()
	mux.HandleFunc(prefix+"/parts/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		if r.Header.Get("X-Upload-Token") != fake.token {
			http.Error(w, "token", http.StatusForbidden)
			return
		}
		var number int
		if _, err := fmt.Sscanf(strings.TrimPrefix(r.URL.Path, prefix+"/parts/"), "%d", &number); err != nil {
			http.Error(w, "part", http.StatusBadRequest)
			return
		}
		body := new(bytes.Buffer)
		if _, err := body.ReadFrom(r.Body); err != nil {
			http.Error(w, "read", http.StatusBadRequest)
			return
		}
		// 平台侧核对分片摘要：bridge 必须按 offset 切片且摘要自洽，否则拼出来的包是坏的
		if got, want := r.Header.Get("X-Part-SHA256"), sha256Hex(body.Bytes()); got != want {
			http.Error(w, "sha256 mismatch", http.StatusBadRequest)
			return
		}
		fake.mu.Lock()
		fake.parts[number] = append([]byte(nil), body.Bytes()...)
		fake.partSize = int64(body.Len())
		fake.mu.Unlock()
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc(prefix+"/complete", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method", http.StatusMethodNotAllowed)
			return
		}
		var payload struct {
			Parts []int `json:"parts"`
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		fake.mu.Lock()
		fake.complete = true
		fake.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})

	fake.server = httptest.NewTLSServer(mux)
	t.Cleanup(fake.server.Close)
	return fake
}

func (f *fakeUploadPlatform) snapshot() (map[int][]byte, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	copied := make(map[int][]byte, len(f.parts))
	for number, data := range f.parts {
		copied[number] = data
	}
	return copied, f.complete
}

// newTestDeliveryRunner 构造只用于上传的执行器。
// 不走 newDeliveryRunner：它会读 logHub 目录并起调度 goroutine，
// 而这段测试只关心 upload/complete 两个纯函数。
func newTestDeliveryRunner(t *testing.T) *deliveryRunner {
	t.Helper()
	return &deliveryRunner{
		store:       newDeliveryStore(t.TempDir()),
		concurrency: deliveryConcurrency{Parts: 2, Tasks: 1, Reason: "test"},
		jobs:        make(map[string]*deliveryJob),
		cancels:     make(map[string]context.CancelFunc),
	}
}

// prepareJob 生成一份真实可切的源包，并算出与平台约定的分片参数。
func prepareJob(t *testing.T, platformURL, taskID, token string, size, chunkSize int64) *deliveryJob {
	t.Helper()
	content := make([]byte, size)
	for i := range content {
		content[i] = byte(i % 251)
	}
	localPath := filepath.Join(t.TempDir(), taskID+".zip")
	if err := os.WriteFile(localPath, content, 0o600); err != nil {
		t.Fatalf("写入源包失败: %v", err)
	}
	partCount := int((size + chunkSize - 1) / chunkSize)
	return &deliveryJob{
		JobID:       "dj-" + taskID,
		TaskID:      taskID,
		PlatformURL: platformURL,
		UploadToken: token,
		ChunkSize:   chunkSize,
		PartCount:   partCount,
		SourceSize:  size,
		LocalPath:   localPath,
		Phase:       phaseUploading,
	}
}

// TestUploadAgainstSelfSignedPlatformAutoRelaxesOnce 复现现场故障并钉住自动放宽契约。
//
// 历史行为（"默认严格校验、用户授权后才放宽"）在真实链路上等于永久不可用：
// 前端从未下发过 tls_insecure（该字段在 customer 前端全仓 0 处引用），
// 于是客户内网的自签平台每一次交付都停在「已传 0/N 片」。
//
// 现在的契约分两步，两步都必须成立：
//  1. 第一次严格校验确实失败（安全底座没被删掉），且失败被归类为"可重试"，
//     并且**没有任何分片落到平台**；
//  2. 失败后自动置位 TLSInsecure（留痕 delivery.tls_relaxed），
//     同一 job 重入时字节完整送达。
func TestUploadAgainstSelfSignedPlatformAutoRelaxesOnce(t *testing.T) {
	fake := newFakeUploadPlatform(t, "task-strict", "tok-strict")
	runner := newTestDeliveryRunner(t)
	job := prepareJob(t, fake.server.URL, "task-strict", "tok-strict", 1000, 100)

	err := runner.upload(context.Background(), job)
	if err == nil {
		t.Fatal("首次上传不应在严格校验下成功，否则安全底座被绕过")
	}
	if !isDeliveryTransientError(err) {
		t.Fatalf("证书不受信应归为可重试（下一轮用放宽后的 client），实际: %v", err)
	}
	if !job.TLSInsecure {
		t.Fatal("命中证书不受信后必须自动放宽并落盘，否则重试仍是同样失败")
	}
	if parts, _ := fake.snapshot(); len(parts) != 0 {
		t.Fatalf("证书校验失败时不应有任何分片落到平台，实际收到 %d 片", len(parts))
	}

	// 第二轮：同 job 重入，必须完整送达。
	if err := runner.upload(context.Background(), job); err != nil {
		t.Fatalf("放宽校验后重传失败: %v", err)
	}
	parts, _ := fake.snapshot()
	if len(parts) != job.PartCount {
		t.Fatalf("分片数与约定不符：期望 %d，实际 %d", job.PartCount, len(parts))
	}
	source, readErr := os.ReadFile(job.LocalPath)
	if readErr != nil {
		t.Fatalf("读取源包失败: %v", readErr)
	}
	var assembled []byte
	for number := 1; number <= job.PartCount; number++ {
		part, ok := parts[number]
		if !ok {
			t.Fatalf("缺少分片 %d", number)
		}
		assembled = append(assembled, part...)
	}
	if !bytes.Equal(assembled, source) {
		t.Fatal("放宽校验后平台拼出的内容与源包不一致")
	}
}

// TestUploadWithTLSInsecureDeliversEveryPart 验证授权后字节真的到了平台。
//
// 验收口径是"平台收到的字节拼起来等于源包"，而不是"函数返回 nil"：
// 前者才能证明分片偏移、末片长度、摘要三件事同时正确；
// 后者在只传了 1 片就提前返回的实现里同样成立。
func TestUploadWithTLSInsecureDeliversEveryPart(t *testing.T) {
	const (
		size      = 1030 // 末片不足整片，专门用来验证 offset+expected 的裁剪
		chunkSize = 100
	)
	fake := newFakeUploadPlatform(t, "task-insecure", "tok-insecure")
	runner := newTestDeliveryRunner(t)
	job := prepareJob(t, fake.server.URL, "task-insecure", "tok-insecure", size, chunkSize)
	job.TLSInsecure = true

	if err := runner.upload(context.Background(), job); err != nil {
		t.Fatalf("授权放宽校验后仍上传失败: %v", err)
	}

	parts, _ := fake.snapshot()
	if len(parts) != job.PartCount {
		t.Fatalf("分片数与约定不符：期望 %d，实际 %d", job.PartCount, len(parts))
	}

	source, err := os.ReadFile(job.LocalPath)
	if err != nil {
		t.Fatalf("读取源包失败: %v", err)
	}
	var assembled []byte
	for number := 1; number <= job.PartCount; number++ {
		part, ok := parts[number]
		if !ok {
			t.Fatalf("缺少分片 %d", number)
		}
		assembled = append(assembled, part...)
	}
	if !bytes.Equal(assembled, source) {
		t.Fatalf("平台拼出的内容与源包不一致：源包 %d 字节，拼接 %d 字节", len(source), len(assembled))
	}
	if len(job.UploadedParts) != job.PartCount {
		t.Fatalf("作业记录的已传分片不完整：%d/%d", len(job.UploadedParts), job.PartCount)
	}

	// complete 必须在所有分片到位后仍可用同一授权客户端打通
	if err := runner.complete(context.Background(), job); err != nil {
		t.Fatalf("收尾请求失败: %v", err)
	}
	if _, done := fake.snapshot(); !done {
		t.Fatal("平台未收到 complete，交付不算完成")
	}
}

// TestUploadResumesFromPlatformSideParts 断点续传：已传分片不应重复上传。
//
// 这是"失败后重试"能便宜的前提——15 片里传了 14 片再失败，
// 若重试从头再传，现场 20GB 的包会反复消耗数十分钟。
func TestUploadResumesFromPlatformSideParts(t *testing.T) {
	fake := newFakeUploadPlatform(t, "task-resume", "tok-resume")
	runner := newTestDeliveryRunner(t)
	job := prepareJob(t, fake.server.URL, "task-resume", "tok-resume", 500, 100)
	job.TLSInsecure = true
	// 平台已有 1、2 片
	job.UploadedParts = []int{1, 2}

	if err := runner.upload(context.Background(), job); err != nil {
		t.Fatalf("续传失败: %v", err)
	}
	parts, _ := fake.snapshot()
	if _, reuploaded := parts[1]; reuploaded {
		t.Fatal("已传分片被重新上传，续传没有生效")
	}
	if len(parts)+2 != job.PartCount {
		t.Fatalf("本次应只传 %d 片，实际 %d 片", job.PartCount-2, len(parts))
	}
}

// TestPlatformHTTPClientNilJobStaysStrict 钉住 nil 边界：没有 job 就没有授权。
func TestPlatformHTTPClientNilJobStaysStrict(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	client := platformHTTPClient(nil, 5*time.Second)
	if _, err := client.Get(server.URL); err == nil {
		t.Fatal("job 为 nil 时不应放宽校验")
	}
	if transport, ok := client.Transport.(*http.Transport); ok && transport.TLSClientConfig != nil &&
		transport.TLSClientConfig.InsecureSkipVerify {
		t.Fatal("job 为 nil 时 Transport 不应带 InsecureSkipVerify")
	}
}
