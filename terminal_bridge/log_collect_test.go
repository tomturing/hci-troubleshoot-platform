package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

// TestBuildLogCollectCreateCommand 覆盖参数白名单与命令转义。
// 插件不防注入，bridge 是最后一道防线：任何非法输入都必须被拒绝，绝不能进入命令行。
func TestBuildLogCollectCreateCommand(t *testing.T) {
	base := InMessage{
		TicketNo: "Q20261022001",
		Modules:  []string{"network"},
		DateFrom: "2026-10-21",
		DateTo:   "2026-10-22",
		IPs:      "172.28.25.1,172.28.25.2",
	}

	t.Run("最小合法参数", func(t *testing.T) {
		command, errType, errMsg := buildLogCollectCreateCommand(base)
		if errType != "" {
			t.Fatalf("不应拒绝：%s %s", errType, errMsg)
		}
		for _, fragment := range []string{
			"acli log_collect create",
			"-s 'Q20261022001'",
			"-m 'network'",
			"-d '2026-10-21,2026-10-22'",
			"-i '172.28.25.1,172.28.25.2'",
		} {
			if !strings.Contains(command, fragment) {
				t.Fatalf("命令缺少片段 %q，实际：%s", fragment, command)
			}
		}
	})

	t.Run("默认专项 other 且去重", func(t *testing.T) {
		req := base
		req.Modules = []string{"other", "other", "core"}
		command, errType, _ := buildLogCollectCreateCommand(req)
		if errType != "" {
			t.Fatalf("不应拒绝：%s", errType)
		}
		if !strings.Contains(command, "-m 'other,core'") {
			t.Fatalf("专项应去重且保持顺序，实际：%s", command)
		}
	})

	t.Run("额外路径与上传地址", func(t *testing.T) {
		req := base
		req.ExtraPaths = []string{"/sf/data/local/a", "/tmp/b.txt"}
		req.RemoteURL = "http://acli.sangfor.com.cn:5888/"
		command, errType, _ := buildLogCollectCreateCommand(req)
		if errType != "" {
			t.Fatalf("不应拒绝：%s", errType)
		}
		if !strings.Contains(command, "-p '/sf/data/local/a,/tmp/b.txt'") {
			t.Fatalf("额外路径拼装错误：%s", command)
		}
		if !strings.Contains(command, "-u 'http://acli.sangfor.com.cn:5888/'") {
			t.Fatalf("上传地址拼装错误：%s", command)
		}
	})

	rejectCases := []struct {
		name    string
		mutate  func(*InMessage)
		errType string
	}{
		{"工单号含分号", func(r *InMessage) { r.TicketNo = "Q1;rm -rf /" }, "invalid_ticket"},
		{"工单号含空格", func(r *InMessage) { r.TicketNo = "Q1 2" }, "invalid_ticket"},
		{"工单号超长", func(r *InMessage) { r.TicketNo = strings.Repeat("A", 33) }, "invalid_ticket"},
		{"未知专项", func(r *InMessage) { r.Modules = []string{"rootkit"} }, "invalid_module"},
		{"日期格式错误", func(r *InMessage) { r.DateFrom = "2026/10/21" }, "invalid_date"},
		{"日期倒置", func(r *InMessage) { r.DateFrom = "2026-10-23" }, "invalid_date"},
		{"超过 30 天", func(r *InMessage) { r.DateFrom = "2026-08-01" }, "invalid_date"},
		{"非法 IP", func(r *InMessage) { r.IPs = "1.2.3.999" }, "invalid_ips"},
		{"注入型 IP", func(r *InMessage) { r.IPs = "1.1.1.1;id" }, "invalid_ips"},
		{"相对路径", func(r *InMessage) { r.ExtraPaths = []string{"sf/data"} }, "invalid_path"},
		{"路径注入", func(r *InMessage) { r.ExtraPaths = []string{"/sf/x;id"} }, "invalid_path"},
		{"路径穿越", func(r *InMessage) { r.ExtraPaths = []string{"/sf/../etc/shadow"} }, "invalid_path"},
		{"非 http 上传地址", func(r *InMessage) { r.RemoteURL = "ftp://a/b" }, "invalid_remote_url"},
		{"上传地址含空格", func(r *InMessage) { r.RemoteURL = "http://a b" }, "invalid_remote_url"},
	}

	for _, tc := range rejectCases {
		t.Run(tc.name, func(t *testing.T) {
			req := base
			tc.mutate(&req)
			if _, errType, _ := buildLogCollectCreateCommand(req); errType != tc.errType {
				t.Fatalf("期望拒绝类型 %s，实际 %s", tc.errType, errType)
			}
		})
	}
}

func TestShellQuote(t *testing.T) {
	if got := shellQuote("plain"); got != "'plain'" {
		t.Fatalf("普通值转义错误：%s", got)
	}
	if got := shellQuote("a'b"); got != `'a'\''b'` {
		t.Fatalf("单引号转义错误：%s", got)
	}
}

// TestParseLogCollectProgress 用实机输出样本锁定解析契约。
func TestParseLogCollectProgress(t *testing.T) {
	t.Run("无任务", func(t *testing.T) {
		raw := "状态：收集任务不存在！\n压缩包路径（完成日志下载后请手动删除压缩包）：\n总进度：\n"
		got := parseLogCollectProgress(raw)
		if !got.ParseOK {
			t.Fatal("应解析成功")
		}
		if got.Status != "收集任务不存在！" {
			t.Fatalf("状态解析错误：%q", got.Status)
		}
		if got.Percent != 0 || got.Done != 0 || got.Total != 0 {
			t.Fatalf("无任务时进度应为零：%+v", got)
		}
	})

	t.Run("进行中并带上传状态", func(t *testing.T) {
		raw := strings.Join([]string{
			"状态：收集任务进行中！",
			"压缩包路径（完成日志下载后请手动删除压缩包）：/sf/data/local/acli/tmp/data/log_collect/Q20261022001.zip",
			"总进度：63% - 2/3（已收集完成/收集总数）",
			"总耗时：00:12:31",
			"172.28.25.2  收集完成  100%",
			"172.28.25.3  收集中  45%",
			"172.28.25.4  收集失败  -",
			"日志收集结果发送状态：",
			"  发送地址：http://acli.sangfor.com.cn:5888/",
			"  状态：发送中",
		}, "\n")

		got := parseLogCollectProgress(raw)
		if got.Status != "收集任务进行中！" {
			t.Fatalf("任务状态错误：%q", got.Status)
		}
		if got.Percent != 63 || got.Done != 2 || got.Total != 3 {
			t.Fatalf("总进度解析错误：%+v", got)
		}
		if got.ArchivePath != "/sf/data/local/acli/tmp/data/log_collect/Q20261022001.zip" {
			t.Fatalf("压缩包路径错误：%q", got.ArchivePath)
		}
		if got.TotalTime != "00:12:31" {
			t.Fatalf("总耗时错误：%q", got.TotalTime)
		}
		if len(got.Hosts) != 3 {
			t.Fatalf("主机明细数量错误：%d", len(got.Hosts))
		}
		if got.Hosts[0].IP != "172.28.25.2" || got.Hosts[0].Status != "收集完成" || got.Hosts[0].Percent != "100%" {
			t.Fatalf("主机明细解析错误：%+v", got.Hosts[0])
		}
		if got.UploadURL != "http://acli.sangfor.com.cn:5888/" || got.UploadState != "发送中" {
			t.Fatalf("上传状态错误：url=%q state=%q", got.UploadURL, got.UploadState)
		}
	})

	t.Run("文案变更时降级为原文不透传错误", func(t *testing.T) {
		raw := "version 2 output without known labels"
		got := parseLogCollectProgress(raw)
		if got.ParseOK {
			t.Fatal("未知文案不应判定解析成功")
		}
		if got.Raw != raw {
			t.Fatal("必须保留原文供前端展示")
		}
	})

	t.Run("空输出", func(t *testing.T) {
		if got := parseLogCollectProgress(""); got.ParseOK {
			t.Fatal("空输出不应判定解析成功")
		}
	})
}

// TestManualLogCollectProbe 是现场集成验证入口（默认 skip，CI 不执行）。
//
//	HCI_TEST_ADDR=172.28.25.1:22 HCI_TEST_USER=root HCI_TEST_KEY=~/.ssh/id_ed25519 \
//	  go test -run TestManualLogCollectProbe -v ./...
//
// 验证内容：PTY 下 acli 插件可执行、版本/架构探测可用、progress 文本可被结构化解析。
func TestManualLogCollectProbe(t *testing.T) {
	addr := strings.TrimSpace(os.Getenv("HCI_TEST_ADDR"))
	if addr == "" {
		t.Skip("未设置 HCI_TEST_ADDR，跳过现场集成验证")
	}
	user := strings.TrimSpace(os.Getenv("HCI_TEST_USER"))
	if user == "" {
		user = "root"
	}
	keyPath := strings.TrimSpace(os.Getenv("HCI_TEST_KEY"))
	if keyPath == "" {
		keyPath = filepath.Join(os.Getenv("HOME"), ".ssh", "id_ed25519")
	}
	key, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatalf("读取私钥失败：%v", err)
	}
	signer, err := ssh.ParsePrivateKey(key)
	if err != nil {
		t.Fatalf("解析私钥失败：%v", err)
	}
	client, err := ssh.Dial("tcp", addr, &ssh.ClientConfig{
		User:            user,
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), // 仅本地手工验证使用
		Timeout:         10 * time.Second,
	})
	if err != nil {
		t.Fatalf("连接失败：%v", err)
	}
	defer client.Close()

	session := &SSHSession{client: client}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	probe := session.probeLogCollectEnv(ctx, logCollectDefaultLogServer)
	t.Logf("probe: %+v", probe)
	if probe.Architecture == "" {
		t.Fatal("架构探测为空")
	}
	if !probe.AcliInstalled {
		t.Fatal("未探测到 acli 安装")
	}

	output, exitCode, err := session.runCommandPty(ctx, "acli log_collect progress", 30*time.Second)
	if err != nil {
		t.Fatalf("progress 执行失败：%v", err)
	}
	progress := parseLogCollectProgress(output)
	t.Logf("progress: exit=%d parse_ok=%v status=%q", exitCode, progress.ParseOK, progress.Status)
	if !progress.ParseOK {
		t.Fatalf("progress 解析失败，原始输出：%s", output)
	}
}

func TestInMessageLogCollectFields(t *testing.T) {
	payload := `{"type":"log_collect","log_action":"create","ticket_no":"Q20261022001","modules":["network","core"],"date_from":"2026-10-21","date_to":"2026-10-22","ips":"all","extra_paths":["/sf/x"],"remote_url":"http://acli.sangfor.com.cn:5888/"}`
	var msg InMessage
	if err := json.Unmarshal([]byte(payload), &msg); err != nil {
		t.Fatalf("反序列化失败：%v", err)
	}
	if msg.LogAction != "create" || msg.TicketNo != "Q20261022001" || len(msg.Modules) != 2 || msg.IPs != "all" {
		t.Fatalf("字段解析错误：%+v", msg)
	}
	if msg.RemoteURL != "http://acli.sangfor.com.cn:5888/" {
		t.Fatalf("上传地址解析错误：%q", msg.RemoteURL)
	}
}
