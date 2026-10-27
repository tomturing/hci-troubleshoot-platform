package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// SFTP 文件下载的单元测试。
//
// 关注点全部落在"会在客户机器上造成真实损害"的地方：
// 本地文件名清洗（路径穿越/覆盖）、落点规划（续传/幂等/冲突）、
// 流式拷贝（哈希、取消、半成品保留）、任务登记（并发/去重/断连回收）。

func TestSanitizeSftpLocalName(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"路径穿越被压成 basename", "../../../etc/passwd", "passwd"},
		{"空名字回退", "", "download.bin"},
		{"当前/上级目录回退", "..", "download.bin"},
		{"控制字符被替换", "log\x00\x01.txt", "log__.txt"},
		{"Windows 保留设备名加前缀", "CON", "_CON"},
		{"Windows 保留设备名带扩展名", "com1.log", "_com1.log"},
		{"结尾点与空格被裁剪", "report.txt. ", "report.txt"},
		{"非法字符被替换", `a:b*c?d"e<f>g|h`, "a_b_c_d_e_f_g_h"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := sanitizeSftpLocalName(tc.in); got != tc.want {
				t.Fatalf("sanitizeSftpLocalName(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestSanitizeSftpLocalNameKeepsExtensionWhenTruncated(t *testing.T) {
	base := strings.Repeat("a", 300) + ".zip"
	got := sanitizeSftpLocalName(base)
	if !strings.HasSuffix(got, ".zip") {
		t.Fatalf("超长文件名必须保留扩展名，got=%q", got)
	}
	if runes := []rune(got); len(runes) > sftpMaxLocalNameRunes {
		t.Fatalf("超长文件名未被截断，长度=%d", len(runes))
	}
}

// 反斜杠在 POSIX 远端是合法文件名字符，不能当路径分隔符切。
// 但落盘后必须仍是一个单一文件名，绝不能带出目录成分。
func TestSanitizeSftpLocalNameNeutralizesBackslashes(t *testing.T) {
	got := sanitizeSftpLocalName(`..\..\windows\system32\config`)
	if strings.ContainsAny(got, `/\\`) {
		t.Fatalf("反斜杠必须被替换为安全字符，got=%q", got)
	}
	// 真正的判定标准是“拼进目录后仍停在该目录内”，而不是字面上不含点。
	dir := t.TempDir()
	joined := filepath.Join(dir, got)
	if filepath.Dir(joined) != filepath.Clean(dir) {
		t.Fatalf("文件名逃出了落地目录: %s", joined)
	}
}

func TestNormalizeSftpRemotePath(t *testing.T) {
	cases := []struct {
		name     string
		in       string
		want     string
		wantType string
	}{
		{"普通绝对路径", "/var/log/messages", "/var/log/messages", ""},
		{"相对路径拒绝", "var/log/messages", "", sftpErrInvalidPath},
		{"空路径拒绝", "   ", "", sftpErrInvalidPath},
		{"控制字符拒绝", "/tmp/a\nb", "", sftpErrInvalidPath},
		{"路径归一化", "/sf/data/../data/local/x.zip", "/sf/data/local/x.zip", ""},
		{"根目录保留", "/", "/", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, errType, _ := normalizeSftpRemotePath(tc.in)
			if got != tc.want || errType != tc.wantType {
				t.Fatalf("normalizeSftpRemotePath(%q) = (%q,%q), want (%q,%q)", tc.in, got, errType, tc.want, tc.wantType)
			}
		})
	}
}

func TestNormalizeSftpRemotePathRejectsOverlong(t *testing.T) {
	overlong := "/" + strings.Repeat("a", sftpMaxRemotePathLen)
	if _, errType, _ := normalizeSftpRemotePath(overlong); errType != sftpErrInvalidPath {
		t.Fatalf("超长路径必须被拒绝，got=%q", errType)
	}
}

func TestPlanSftpLocalTargetFreshDirectory(t *testing.T) {
	dir := t.TempDir()
	target, err := planSftpLocalTarget(dir, "/sf/data/local/acli/tmp/log.zip", 1024)
	if err != nil {
		t.Fatalf("planSftpLocalTarget 失败: %v", err)
	}
	if target.FinalPath != filepath.Join(dir, "log.zip") {
		t.Fatalf("落地路径错误: %s", target.FinalPath)
	}
	if target.PartPath != target.FinalPath+".part" {
		t.Fatalf("临时文件必须是 .part，got=%s", target.PartPath)
	}
	if target.ResumeFrom != 0 || target.AlreadyComplete {
		t.Fatalf("新下载不应续传/跳过: %+v", target)
	}
}

func TestPlanSftpLocalTargetNeverOverwritesExistingFile(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "log.zip")
	if err := os.WriteFile(existing, []byte("user-owned"), 0o600); err != nil {
		t.Fatal(err)
	}

	target, err := planSftpLocalTarget(dir, "/tmp/log.zip", 1024)
	if err != nil {
		t.Fatalf("planSftpLocalTarget 失败: %v", err)
	}
	if target.FinalPath == existing {
		t.Fatal("同名文件大小不一致时必须换名，绝不能覆盖用户已有文件")
	}
	if filepath.Base(target.FinalPath) != "log-2.zip" {
		t.Fatalf("冲突时应使用 -2 后缀，got=%s", filepath.Base(target.FinalPath))
	}
}

func TestPlanSftpLocalTargetIdempotentWhenAlreadyComplete(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "log.zip")
	if err := os.WriteFile(existing, bytes.Repeat([]byte("x"), 1024), 0o600); err != nil {
		t.Fatal(err)
	}
	target, err := planSftpLocalTarget(dir, "/tmp/log.zip", 1024)
	if err != nil {
		t.Fatalf("planSftpLocalTarget 失败: %v", err)
	}
	if !target.AlreadyComplete || target.FinalPath != existing {
		t.Fatalf("同名且同大小应判定已完成: %+v", target)
	}
}

func TestPlanSftpLocalTargetResumesFromPartial(t *testing.T) {
	dir := t.TempDir()
	part := filepath.Join(dir, "log.zip.part")
	if err := os.WriteFile(part, bytes.Repeat([]byte("x"), 400), 0o600); err != nil {
		t.Fatal(err)
	}
	target, err := planSftpLocalTarget(dir, "/tmp/log.zip", 1024)
	if err != nil {
		t.Fatalf("planSftpLocalTarget 失败: %v", err)
	}
	if target.ResumeFrom != 400 {
		t.Fatalf("应从已有片段续传，got=%d", target.ResumeFrom)
	}
}

func TestPlanSftpLocalTargetDropsOversizedPartial(t *testing.T) {
	dir := t.TempDir()
	part := filepath.Join(dir, "log.zip.part")
	if err := os.WriteFile(part, bytes.Repeat([]byte("x"), 4096), 0o600); err != nil {
		t.Fatal(err)
	}
	target, err := planSftpLocalTarget(dir, "/tmp/log.zip", 1024)
	if err != nil {
		t.Fatalf("planSftpLocalTarget 失败: %v", err)
	}
	if target.ResumeFrom != 0 {
		t.Fatalf("残留片段比远端还大时必须丢弃重来，got=%d", target.ResumeFrom)
	}
	if _, statErr := os.Stat(part); !os.IsNotExist(statErr) {
		t.Fatal("过大的残留片段应被删除")
	}
}

// fakeSftpRemote 用于目录列表与命名排序测试。
type fakeSftpRemote struct {
	infos []os.FileInfo
	wd    string
	err   error
}

func (f *fakeSftpRemote) ListDir(string) ([]os.FileInfo, error) { return f.infos, f.err }
func (f *fakeSftpRemote) Stat(string) (os.FileInfo, error)      { return nil, f.err }
func (f *fakeSftpRemote) Open(string) (sftpSeekReader, error)   { return nil, f.err }
func (f *fakeSftpRemote) Getwd() (string, error)                { return f.wd, f.err }
func (f *fakeSftpRemote) Close() error                          { return nil }

type fakeFileInfo struct {
	name  string
	size  int64
	isDir bool
}

func (f fakeFileInfo) Name() string       { return f.name }
func (f fakeFileInfo) Size() int64        { return f.size }
func (f fakeFileInfo) Mode() os.FileMode  { return 0o644 }
func (f fakeFileInfo) ModTime() time.Time { return time.Unix(1700000000, 0) }
func (f fakeFileInfo) IsDir() bool        { return f.isDir }
func (f fakeFileInfo) Sys() any           { return nil }

func TestListSftpDirectoryOrdersDirectoriesFirst(t *testing.T) {
	remote := &fakeSftpRemote{infos: []os.FileInfo{
		fakeFileInfo{name: "b.log", size: 10},
		fakeFileInfo{name: "A-dir", isDir: true},
		fakeFileInfo{name: ".", isDir: true},
		fakeFileInfo{name: "..", isDir: true},
		fakeFileInfo{name: "a.log", size: 20},
	}}
	entries, truncated, err := listSftpDirectory(remote, "/sf/data", 100)
	if err != nil {
		t.Fatalf("listSftpDirectory 失败: %v", err)
	}
	if truncated {
		t.Fatal("未超上限不应标记截断")
	}
	if len(entries) != 3 {
		t.Fatalf("应过滤 . 与 ..，got=%d 项", len(entries))
	}
	if !entries[0].IsDir || entries[0].Name != "A-dir" {
		t.Fatalf("目录必须排在最前，got=%+v", entries[0])
	}
	if entries[1].Name != "a.log" || entries[2].Name != "b.log" {
		t.Fatalf("同类型应按名称排序，got=%v,%v", entries[1].Name, entries[2].Name)
	}
	if entries[0].Path != "/sf/data/A-dir" {
		t.Fatalf("目录项 path 拼接错误: %s", entries[0].Path)
	}
}

func TestSftpDefaultRemoteDirUsesLoginHome(t *testing.T) {
	remote := &fakeSftpRemote{wd: "/home/acli/"}
	if got := sftpDefaultRemoteDir(remote); got != "/home/acli" {
		t.Fatalf("应落到远端 home 并规范化，got=%q", got)
	}
}

func TestSftpDefaultRemoteDirFallsBackToRoot(t *testing.T) {
	// Getwd 报错（受限账号 / 不实现 realpath）时不能把错误抛给前端，
	// 否则面板首次打开就只能看着一句失败提示。
	remote := &fakeSftpRemote{err: os.ErrPermission}
	if got := sftpDefaultRemoteDir(remote); got != "/" {
		t.Fatalf("Getwd 失败应回落根目录，got=%q", got)
	}
}

func TestListSftpDirectoryTruncates(t *testing.T) {
	infos := make([]os.FileInfo, 0, 5)
	for _, name := range []string{"1", "2", "3", "4", "5"} {
		infos = append(infos, fakeFileInfo{name: name, size: 1})
	}
	entries, truncated, err := listSftpDirectory(&fakeSftpRemote{infos: infos}, "/tmp", 3)
	if err != nil {
		t.Fatalf("listSftpDirectory 失败: %v", err)
	}
	if !truncated || len(entries) != 3 {
		t.Fatalf("超上限应截断并标记，got truncated=%v len=%d", truncated, len(entries))
	}
}

func TestCopyRemoteToPartComputesWholeFileHash(t *testing.T) {
	dir := t.TempDir()
	part := filepath.Join(dir, "a.bin")
	content := []byte("hello sftp download")
	written, sum, err := copyRemoteToPart(context.Background(), bytes.NewReader(content), part, 0, int64(len(content)), nil)
	if err != nil {
		t.Fatalf("copyRemoteToPart 失败: %v", err)
	}
	if written != int64(len(content)) {
		t.Fatalf("写入字节数错误: %d", written)
	}
	expected := sha256.Sum256(content)
	if sum != hex.EncodeToString(expected[:]) {
		t.Fatalf("哈希错误: %s", sum)
	}
	got, err := os.ReadFile(part)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, content) {
		t.Fatal("落盘内容与远端不一致")
	}
}

func TestCopyRemoteToPartResumeHashesExistingBytes(t *testing.T) {
	dir := t.TempDir()
	part := filepath.Join(dir, "a.bin")
	full := []byte("0123456789abcdef")
	head := full[:6]
	if err := os.WriteFile(part, head, 0o600); err != nil {
		t.Fatal(err)
	}

	written, sum, err := copyRemoteToPart(context.Background(), bytes.NewReader(full[6:]), part, int64(len(head)), int64(len(full)), nil)
	if err != nil {
		t.Fatalf("续传失败: %v", err)
	}
	if written != int64(len(full)) {
		t.Fatalf("续传后总字节错误: %d", written)
	}
	// 关键：哈希必须覆盖"已有片段 + 新到达字节"，否则校验值会骗人。
	expected := sha256.Sum256(full)
	if sum != hex.EncodeToString(expected[:]) {
		t.Fatalf("续传哈希未覆盖已有片段: %s", sum)
	}
	got, err := os.ReadFile(part)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, full) {
		t.Fatalf("续传后内容错误: %q", string(got))
	}
}

func TestCopyRemoteToPartKeepsPartialOnCancel(t *testing.T) {
	dir := t.TempDir()
	part := filepath.Join(dir, "a.bin")
	ctx, cancel := context.WithCancel(context.Background())

	reader := &cancelAfterFirstRead{reader: bytes.NewReader(bytes.Repeat([]byte("x"), 4096)), cancel: cancel}
	written, _, err := copyRemoteToPart(ctx, reader, part, 0, 4096, nil)
	if err == nil || !errors.Is(err, context.Canceled) {
		t.Fatalf("取消必须返回 context.Canceled，got=%v", err)
	}
	if written == 0 {
		t.Fatal("取消前已写入的字节必须保留，便于续传")
	}
	if _, statErr := os.Stat(part); statErr != nil {
		t.Fatalf("取消后 .part 必须保留: %v", statErr)
	}
}

type cancelAfterFirstRead struct {
	reader io.Reader
	cancel context.CancelFunc
	done   bool
}

func (c *cancelAfterFirstRead) Read(p []byte) (int, error) {
	n, err := c.reader.Read(p)
	if !c.done {
		c.done = true
		c.cancel()
	}
	return n, err
}

func TestPercentOf(t *testing.T) {
	cases := []struct {
		done, total int64
		want        int
	}{
		{0, 100, 0},
		{50, 100, 50},
		{100, 100, 100},
		{150, 100, 100},
		{10, 0, 0},
	}
	for _, tc := range cases {
		if got := percentOf(tc.done, tc.total); got != tc.want {
			t.Fatalf("percentOf(%d,%d) = %d, want %d", tc.done, tc.total, got, tc.want)
		}
	}
}

func TestResolveSftpDownloadDirHonoursEnv(t *testing.T) {
	dir := t.TempDir()
	custom := filepath.Join(dir, "custom-downloads")
	t.Setenv("HCI_BRIDGE_SFTP_DIR", custom)
	got, err := resolveSftpDownloadDir()
	if err != nil {
		t.Fatalf("resolveSftpDownloadDir 失败: %v", err)
	}
	if got != custom {
		t.Fatalf("应优先使用显式配置目录，got=%s", got)
	}
	if info, statErr := os.Stat(got); statErr != nil || !info.IsDir() {
		t.Fatalf("目录必须被创建: %v", statErr)
	}
}

func TestSftpMaxBytesFromEnv(t *testing.T) {
	t.Setenv("HCI_BRIDGE_SFTP_MAX_BYTES", "2048")
	if got := sftpMaxBytes(); got != 2048 {
		t.Fatalf("上限应取自环境变量，got=%d", got)
	}
	t.Setenv("HCI_BRIDGE_SFTP_MAX_BYTES", "not-a-number")
	if got := sftpMaxBytes(); got != sftpDefaultMaxBytes {
		t.Fatalf("非法配置必须回落默认值，got=%d", got)
	}
}

func TestRegisterSftpTaskEnforcesLimits(t *testing.T) {
	bridge := newBridge()
	_, cancel1, errType := bridge.registerSftpTask(sftpTaskKey("Q1", "t1"), "conn-1", "Q1")
	if errType != "" {
		t.Fatalf("首个任务应注册成功: %s", errType)
	}
	defer cancel1()

	if _, _, errType := bridge.registerSftpTask(sftpTaskKey("Q1", "t1"), "conn-1", "Q1"); errType != sftpErrDuplicateTask {
		t.Fatalf("同任务重复提交应被拒绝，got=%q", errType)
	}

	_, cancel2, errType := bridge.registerSftpTask(sftpTaskKey("Q1", "t2"), "conn-1", "Q1")
	if errType != "" {
		t.Fatalf("第二个任务应注册成功: %s", errType)
	}
	defer cancel2()

	if _, _, errType := bridge.registerSftpTask(sftpTaskKey("Q2", "t3"), "conn-2", "Q2"); errType != sftpErrBusy {
		t.Fatalf("超过并发上限应被拒绝，got=%q", errType)
	}

	if running := bridge.runningSftpTasks(); running != 2 {
		t.Fatalf("运行中任务数应为 2，got=%d", running)
	}
}

func TestCancelSftpTasksForConn(t *testing.T) {
	bridge := newBridge()
	ctx1, cancel1, _ := bridge.registerSftpTask(sftpTaskKey("Q1", "t1"), "conn-1", "Q1")
	defer cancel1()
	ctx2, cancel2, _ := bridge.registerSftpTask(sftpTaskKey("Q1", "t2"), "conn-2", "Q1")
	defer cancel2()

	if cancelled := bridge.cancelSftpTasksForConn("conn-1"); cancelled != 1 {
		t.Fatalf("应只取消本连接的任务，got=%d", cancelled)
	}
	select {
	case <-ctx1.Done():
	case <-time.After(time.Second):
		t.Fatal("conn-1 的任务应被取消")
	}
	select {
	case <-ctx2.Done():
		t.Fatal("conn-2 的任务不应被取消")
	default:
	}
}

func TestCancelUnknownSftpTaskIsNotError(t *testing.T) {
	bridge := newBridge()
	if bridge.cancelSftpTask(sftpTaskKey("Q1", "missing")) {
		t.Fatal("取消不存在的任务应返回 false，由调用方回包告知已结束")
	}
}

// TestDropSftpRemoteClientIfIdle 验证“死通道回收”不会误伤并发的另一个下载。
func TestDropSftpRemoteClientIfIdle(t *testing.T) {
	bridge := newBridge()
	session := &SSHSession{sftp: &fakeSftpRemote{}}

	// 无并发任务：直接丢弃，让下次请求重新协商
	if !bridge.dropSftpRemoteClientIfIdle(session, false) {
		t.Fatal("无并发任务时应丢弃缓存通道")
	}
	if session.sftp != nil {
		t.Fatal("通道应已被清空，否则后续请求会一直撞死通道")
	}

	// 有旁人任务：列表失败不得关闭通道（否则会提前杀断另一个下载）
	session.sftp = &fakeSftpRemote{}
	bridge.sftpTasks[sftpTaskKey("Q1", "other")] = &sftpTask{cancel: func() {}}
	if bridge.dropSftpRemoteClientIfIdle(session, false) {
		t.Fatal("有并发任务时不应丢弃通道")
	}
	if session.sftp == nil {
		t.Fatal("有并发任务时通道必须保留")
	}

	// allowSelf：任务表里只有自己（下载场景），允许丢弃
	if !bridge.dropSftpRemoteClientIfIdle(session, true) {
		t.Fatal("只剩自己一个任务时应允许丢弃")
	}

	// allowSelf 但还有旁人：仍然不能丢
	session.sftp = &fakeSftpRemote{}
	bridge.sftpTasks[sftpTaskKey("Q1", "self")] = &sftpTask{cancel: func() {}}
	if bridge.dropSftpRemoteClientIfIdle(session, true) {
		t.Fatal("还有并发任务时不得丢弃通道")
	}
	if session.sftp == nil {
		t.Fatal("还有并发任务时通道必须保留")
	}
}

func TestSftpWireContract(t *testing.T) {
	// 协议字段名是前后端的硬契约：改名等于静默功能失效，这里锁死。
	encoded, err := json.Marshal(OutMessage{
		Type: "sftp_transfer_progress", CaseID: "Q1", ExecID: "task-1", SftpAction: "download",
		SftpData: sftpTransferPayload{TaskID: "task-1", Phase: "downloading", Bytes: 1, TotalBytes: 2, Percent: 50},
	})
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	for _, key := range []string{`"type"`, `"exec_id"`, `"sftp_action"`, `"sftp_data"`, `"task_id"`, `"total_bytes"`, `"percent"`} {
		if !strings.Contains(string(encoded), key) {
			t.Fatalf("协议缺少字段 %s: %s", key, encoded)
		}
	}

	var inbound InMessage
	if err := json.Unmarshal([]byte(`{"type":"sftp_op","case_id":"Q1","exec_id":"t1","sftp_action":"download","sftp_path":"/tmp/a.zip","node_ip":"10.0.0.1"}`), &inbound); err != nil {
		t.Fatalf("解析入站消息失败: %v", err)
	}
	if inbound.SftpAction != "download" || inbound.SftpPath != "/tmp/a.zip" {
		t.Fatalf("入站字段解析错误: %+v", inbound)
	}
}
