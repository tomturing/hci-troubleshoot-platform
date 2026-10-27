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

// TestParseLogCollectModuleCatalog 用实机 help 原文验证场景目录解析。
// 对抗性审查要点：枚举值按两列排版（一行两个），且尾部带 "--help 未定义参数" 的报错行。
func TestParseLogCollectModuleCatalog(t *testing.T) {
	help := `用法: acli log_collect create [命令参数]

描述:
  create                                创建日志收集任务

命令参数:
  -s|--save-filename=STRING             必要参数，收集文件命名（无需文件后缀），使用icare单号，示例：Q2025052718316
  -m|--modules=STRING                   日志收集专项，使用英文逗号拼接指定的专项，示例：-m vm_suspend,network
                                        可供选择的专项，不设置则默认other，枚举值：
                                          vm_suspend（虚拟机挂起专项）              vm_start（虚拟机开机失败专项）
                                          host_offline（主机离线专项）              network（网络专项）
                                          storage（虚拟存储专项）                    slowdisk（卡慢盘专项，单天收集时间约4min）
                                          performance（性能专项）                    core（core文件专项）
                                          system（中台专项）                          other（默认收集专项，不收集上述n个场景日志）
  -d|--date-range=STRING                指定日期范围（使用英文逗号拼接起始日期，结束日期）

说明:
  -s 为必要参数，后续Icare单号

示例1: 收集网络专项
  acli log_collect create -s Q2025052777777 -m network -i 1.1.1.1 -d 2025-06-25,2025-06-28
错误: 未定义参数: --help
`
	options := parseLogCollectModuleCatalog(help)
	if len(options) != 10 {
		t.Fatalf("应解析出 10 个场景，实际 %d 个：%+v", len(options), options)
	}
	wantOrder := []string{"vm_suspend", "vm_start", "host_offline", "network", "storage", "slowdisk", "performance", "core", "system", "other"}
	for i, want := range wantOrder {
		if options[i].Value != want {
			t.Fatalf("第 %d 项应为 %s，实际 %s", i, want, options[i].Value)
		}
		if options[i].Label == "" {
			t.Fatalf("场景 %s 缺少中文标签", want)
		}
	}
	if options[5].Label != "卡慢盘专项，单天收集时间约4min" {
		t.Fatalf("slowdisk 标签解析错误：%q", options[5].Label)
	}
}

// TestParseLogCollectModuleCatalogFallback 覆盖 help 改版/无枚举时的降级信号。
func TestParseLogCollectModuleCatalogFallback(t *testing.T) {
	cases := []string{"", "命令不存在", "用法: acli log_collect create\n  -m|--modules=STRING 日志专项\n"}
	for _, raw := range cases {
		if got := parseLogCollectModuleCatalog(raw); len(got) != 0 {
			t.Fatalf("无枚举段落时应返回空（触发内置回退），实际：%+v", got)
		}
	}
	if got := builtinLogCollectModules(); len(got) != 10 {
		t.Fatalf("内置目录应覆盖全部 10 个场景，实际 %d 个", len(got))
	}
}

// TestParseLogCollectNodeList 用实机 JSON 原文验证节点清单解析。
func TestParseLogCollectNodeList(t *testing.T) {
	raw := `Sanfor:/aSV/host-80615f1e94e4(HOST-OS) /sf # 
{
  "data": [
    {
      "alert": "0",
      "controller": 1,
      "id": "host-80615f1e883a",
      "ip": "172.28.25.4",
      "master": 0,
      "name": "SVR_aCloud_682",
      "status": 1
    },
    {
      "alert": "0",
      "controller": 1,
      "id": "host-80615f1e94a6",
      "ip": "172.28.25.3",
      "master": 0,
      "name": "SVR_aCloud_681",
      "status": 1
    },
    {
      "alert": "0",
      "controller": 1,
      "id": "host-80615f1e94e4",
      "ip": "172.28.25.2",
      "master": 1,
      "name": "SVR_aCloud_680",
      "status": 1
    }
  ]
}`
	hosts := parseLogCollectNodeList(raw)
	if len(hosts) != 3 {
		t.Fatalf("应解析出 3 个节点，实际 %d 个：%+v", len(hosts), hosts)
	}
	if hosts[0].IP != "172.28.25.4" || hosts[0].Name != "SVR_aCloud_682" || hosts[0].Master {
		t.Fatalf("首个节点解析错误：%+v", hosts[0])
	}
	if !hosts[2].Master || hosts[2].IP != "172.28.25.2" {
		t.Fatalf("master 标记解析错误：%+v", hosts[2])
	}
}

// TestParseLogCollectNodeListDegrade 覆盖脏输出与非法 IP：只降级，不 panic、不污染清单。
func TestParseLogCollectNodeListDegrade(t *testing.T) {
	if got := parseLogCollectNodeList("error: command not found"); len(got) != 0 {
		t.Fatalf("非 JSON 应返回空，实际：%+v", got)
	}
	raw := `{"data":[{"ip":"172.28.25.9","name":"a"},{"ip":"172.28.25.9","name":"dup"},{"ip":"not-an-ip","name":"bad"}]}`
	hosts := parseLogCollectNodeList(raw)
	if len(hosts) != 1 || hosts[0].IP != "172.28.25.9" {
		t.Fatalf("应去重并过滤非法 IP，实际：%+v", hosts)
	}
}

// TestSessionByNodeIP 覆盖"会话键含工单号、采集按节点复用"的回退规则。
// 现场问题：用户在 SSH 终端或旧工单下登录过（键为 terminal-only@IP / 旧工单@IP），
// 换到新工单采集时精确键失配，直接报 session_missing（LC-ENV-006）；
// 而界面此时显示"已连接"，自相矛盾。
func TestSessionByNodeIP(t *testing.T) {
	target := &SSHSession{}
	prefixTrap := &SSHSession{}
	other := &SSHSession{}
	bridge := &Bridge{sessions: map[string]*SSHSession{}}
	bridge.sessions["terminal-only@172.28.25.1"] = target
	bridge.sessions["Q2026101401684@172.28.25.11"] = prefixTrap // 前缀相同的另一台主机，不得被误命中
	bridge.sessions["Q2026101401684@172.28.25.20"] = other

	got, key := bridge.sessionByNodeIP("172.28.25.1")
	if got != target {
		t.Fatalf("应按节点 IP 复用会话，实际 got=%v key=%s", got, key)
	}
	if key != "terminal-only@172.28.25.1" {
		t.Fatalf("应返回命中的原始键，实际：%s", key)
	}

	if got, _ := bridge.sessionByNodeIP("172.28.25.11"); got != prefixTrap {
		t.Fatal("前缀相同的主机必须精确匹配")
	}
	if got, _ := bridge.sessionByNodeIP("172.28.25.99"); got != nil {
		t.Fatal("不存在的节点必须返回 nil，由调用方报 session_missing")
	}
	if got, _ := bridge.sessionByNodeIP("  "); got != nil {
		t.Fatal("空节点不得命中任何会话")
	}
}

// 回归：cancel 曾经漏传 node_ip，精确键与 node_ip 两级回退全部失效，直接 session_missing，
// 结果是平台显示已取消、节点上采集任务仍在跑，后续 create 被插件互斥拒绝。
// 本工单只有一条 SSH 会话时，必须能兜底命中。
// 回归：acli 失败输出带颜色码（`\x1b[31mFATA\x1b[0m...`），
// 不清洗会污染状态行匹配，也会把乱码直接展示到面板。
func TestStripAnsi(t *testing.T) {
	raw := "\u001b[31mFATA\u001b[0m[0000] execing command in container: command terminated with exit code 1"
	cleaned := stripAnsi(raw)
	if strings.Contains(cleaned, "\u001b") {
		t.Fatalf("转义序列未清除：%q", cleaned)
	}
	if !strings.HasPrefix(cleaned, "FATA[0000]") {
		t.Fatalf("清洗后内容异常：%q", cleaned)
	}

	// 带颜色的状态行清洗后必须仍能被归一化识别
	status := stripAnsi("状态：\u001b[31m收集任务异常结束！\u001b[0m")
	got := classifyLogCollectNodeState(logCollectProgress{Status: strings.TrimPrefix(status, "状态：")})
	if got != logCollectNodeAbnormal {
		t.Fatalf("带 ANSI 的状态行应识别为 abnormal，实际 %s", got)
	}
}

func TestClassifyLogCollectNodeState(t *testing.T) {
	cases := []struct {
		name     string
		progress logCollectProgress
		want     string
	}{
		{"进行中", logCollectProgress{Status: "收集任务进行中！"}, logCollectNodeRunning},
		{"已完成", logCollectProgress{Status: "收集任务已完成！", ArchivePath: "/x.zip"}, logCollectNodeCompleted},
		// 回归：异常结束既不含"完成"也不含"失败"，早期前端因此空转到 1 小时超时
		{"异常结束", logCollectProgress{Status: "收集任务异常结束！"}, logCollectNodeAbnormal},
		{"收集失败", logCollectProgress{Status: "收集失败"}, logCollectNodeAbnormal},
		{"无任务", logCollectProgress{Status: "收集任务不存在！"}, logCollectNodeNone},
		{"状态行缺失但仍在跑", logCollectProgress{Hosts: []logCollectHost{{IP: "10.0.0.1", Status: "收集中"}}}, logCollectNodeRunning},
		// 识别不了就不动平台状态，绝不按猜测去 cancel 节点任务
		{"完全无法识别", logCollectProgress{}, logCollectNodeUnknown},
	}
	for _, tc := range cases {
		if got := classifyLogCollectNodeState(tc.progress); got != tc.want {
			t.Fatalf("%s: got %s want %s", tc.name, got, tc.want)
		}
	}
}

func TestResolveLogCollectSessionFallsBackToOnlySession(t *testing.T) {
	single := &SSHSession{address: "192.168.33.89:22"}
	bridge := &Bridge{sessions: map[string]*SSHSession{"Q2026102601505@192.168.33.89": single}}

	got, key, match := bridge.resolveLogCollectSession(InMessage{CaseID: "Q2026102601505", LogAction: "cancel"})
	if got != single || key != "Q2026102601505@192.168.33.89" || match != "only_session_for_case" {
		t.Fatalf("唯一会话应被兜底命中，实际 got=%v key=%s match=%s", got, key, match)
	}

	// 多节点时不猜：猜错会取消另一台机器上的采集任务
	multi := &Bridge{sessions: map[string]*SSHSession{
		"Q1@10.0.0.1": {address: "10.0.0.1:22"},
		"Q1@10.0.0.2": {address: "10.0.0.2:22"},
	}}
	if got, _, _ := multi.resolveLogCollectSession(InMessage{CaseID: "Q1", LogAction: "cancel"}); got != nil {
		t.Fatal("多节点且无 node_ip 时不得兜底，必须返回 nil 由调用方报 session_missing")
	}

	// 带了 node_ip 就走精确键，命中原因要能被日志区分
	if got, key, match := multi.resolveLogCollectSession(InMessage{CaseID: "Q1", NodeIP: "10.0.0.2"}); got == nil || key != "Q1@10.0.0.2" || match != "exact" {
		t.Fatalf("精确键应优先命中，实际 key=%s match=%s", key, match)
	}

	// 同节点其他工单的会话：按节点复用，不应落到 only_session_for_case
	reuse := &Bridge{sessions: map[string]*SSHSession{
		"Q1@10.0.0.1":            {address: "10.0.0.1:22"},
		"terminal-only@10.9.9.9": {address: "10.9.9.9:22"},
	}}
	got2, key2, match2 := reuse.resolveLogCollectSession(InMessage{CaseID: "Q2", NodeIP: "10.9.9.9"})
	if got2 == nil || key2 != "terminal-only@10.9.9.9" || match2 != "node_ip_suffix" {
		t.Fatalf("应按节点复用其他工单会话，实际 key=%s match=%s", key2, match2)
	}
}

// TestSessionByAddressHost 覆盖"会话键里不含 IP"的存量场景。
// ssh_connect 历史上不带 node_ip，键只剩 caseID；此时日志收集按 caseID@nodeIP
// 查不到，必须能回退到"会话实际连的主机地址"。
func TestSessionByAddressHost(t *testing.T) {
	legacy := &SSHSession{address: "172.28.25.1:22"}
	otherPort := &SSHSession{address: "172.28.25.7:2222"}
	bridge := &Bridge{sessions: map[string]*SSHSession{}}
	// 键故意不带 @IP，复现现场：终端连好了，但键只有 caseID
	bridge.sessions["terminal-only"] = legacy
	bridge.sessions["Q2026101401684@172.28.25.7"] = otherPort

	got, key := bridge.sessionByAddressHost("172.28.25.1")
	if got != legacy || key != "terminal-only" {
		t.Fatalf("应按会话地址命中存量会话，实际 got=%v key=%s", got, key)
	}
	if got, _ := bridge.sessionByAddressHost(" 172.28.25.7 "); got != otherPort {
		t.Fatal("地址匹配应忽略首尾空格与端口差异")
	}
	if got, _ := bridge.sessionByAddressHost("172.28.25.99"); got != nil {
		t.Fatal("未连接过的主机必须返回 nil")
	}
	// 无 address 的空会话不得被误命中
	bridge.sessions["broken"] = &SSHSession{}
	if got, _ := bridge.sessionByAddressHost(""); got != nil {
		t.Fatal("空节点不得命中")
	}
}

// TestFallbackLogCollectArchivePath 覆盖压缩包路径推导。
//
// 现场（Q2026101401683）：插件在「进行中/上传中」阶段不打印压缩包路径，
// 直到 100% 完成才给出，而那时 P1 已上传成功、插件已自删包，stat 必然失败。
// 因此路径必须能按 {case_id}.zip 推导出来，否则 archive_size 永远是「未记录」。
func TestFallbackLogCollectArchivePath(t *testing.T) {
	const prefix = "/sf/data/local/acli/tmp/data/log_collect/"

	if got := fallbackLogCollectArchivePath(" Q2026101401683 "); got != prefix+"Q2026101401683.zip" {
		t.Fatalf("应裁剪首尾空格并拼出约定路径，实际 %q", got)
	}
	if got := fallbackLogCollectArchivePath(""); got != "" {
		t.Fatalf("空 caseID 不得推导路径，实际 %q", got)
	}
	if got := fallbackLogCollectArchivePath("   "); got != "" {
		t.Fatalf("纯空格 caseID 不得推导路径，实际 %q", got)
	}

	// caseID 会拼进 shell 命令，任何能逃出归档目录的字符都必须被白名单挡掉
	for _, bad := range []string{
		"Q2026;rm -rf /",
		"Q2026$(id)",
		"../../etc/passwd",
		"Q2026 1001",
		"Q2026`id`",
		"Q2026|cat",
	} {
		if got := fallbackLogCollectArchivePath(bad); got != "" {
			t.Fatalf("非法 caseID %q 不得推导路径，实际 %q", bad, got)
		}
	}

	// 推导出来的路径必须能过 normalizeArchivePath（后续 stat 会再校验一次）
	path := fallbackLogCollectArchivePath("Q2026101401683")
	if _, err := normalizeArchivePath(path); err != nil {
		t.Fatalf("推导路径应能被 normalizeArchivePath 接受：%v", err)
	}
}
