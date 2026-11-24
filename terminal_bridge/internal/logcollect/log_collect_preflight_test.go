package logcollect

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// 目标节点快照只认点名模式：all/空 由节点侧决定集合，不能猜，否则会把
// 「本轮未参与的节点」误判成缺失。
func TestExpectedHostsFromIPs(t *testing.T) {
	cases := []struct {
		ips  string
		want []string
	}{
		{"", nil},
		{"all", nil},
		{" ALL ", nil},
		{"192.168.8.11,192.168.8.12", []string{"192.168.8.11", "192.168.8.12"}},
		{"192.168.8.11, 192.168.8.11 ,bad-ip", []string{"192.168.8.11"}},
	}
	for _, item := range cases {
		got := expectedHostsFromIPs(item.ips)
		if len(got) != len(item.want) {
			t.Fatalf("expectedHostsFromIPs(%q) = %v, want %v", item.ips, got, item.want)
		}
		for index := range item.want {
			if got[index] != item.want[index] {
				t.Fatalf("expectedHostsFromIPs(%q)[%d] = %q, want %q", item.ips, index, got[index], item.want[index])
			}
		}
	}
}

func TestParsePluginProbeOnlyAcceptsMissing(t *testing.T) {
	output := strings.Join([]string{
		"OK /sf/data/local/acli/plugins/log_collect/plugin",
		"MISSING /sf/data/local/acli/plugins/log_collect/manifest.json",
		"/bin/sh: line 1: warning",
		"",
	}, "\n")
	missing := parsePluginProbe(output)
	if len(missing) != 1 || missing[0] != "/sf/data/local/acli/plugins/log_collect/manifest.json" {
		t.Fatalf("只应解析出 1 条 MISSING：%v", missing)
	}
}

// 插件探测必须用 [ -s ]：空目录/空文件同样无法执行，不能判为完整。
// 但阻断清单只能含安装期产物：progress.json 由 sfd_main.py 在 create 之后生成，
// 全新安装时必然不存在，把它算进来等于每次 create 都被误拦（2026-11-24 事故）。
func TestPluginProbeOnlyChecksInstallArtifacts(t *testing.T) {
	command := buildPluginProbeCommand()
	if !strings.Contains(command, "[ -s ") {
		t.Fatalf("插件探测必须要求文件非空：\n%s", command)
	}
	for _, path := range logCollectInstallArtifacts() {
		if !strings.Contains(command, path) {
			t.Fatalf("探测命令缺少 %s：\n%s", path, command)
		}
	}
	if strings.Contains(command, logCollectRuntimeProgress) {
		t.Fatalf("运行时进度文件不得进入 create 前的阻断清单：\n%s", command)
	}
}

// 全新安装现场：plugin/manifest 都在、progress.json 尚未生成，create 必须放行；
// 反向边界：真缺 plugin 时仍必须拒绝，不能因为放开运行时文件而丢掉 P0 的防护。
func TestFreshInstallWithoutRuntimeProgressPasses(t *testing.T) {
	fresh := strings.Join([]string{
		"OK " + logCollectPluginBinary,
		"OK " + logCollectPluginManifest,
	}, "\n")
	if missing := parsePluginProbe(fresh); len(missing) != 0 {
		t.Fatalf("全新安装后 create 应放行，实际判缺失：%v", missing)
	}
	broken := strings.Join([]string{
		"MISSING " + logCollectPluginBinary,
		"OK " + logCollectPluginManifest,
	}, "\n")
	missing := parsePluginProbe(broken)
	if len(missing) != 1 || missing[0] != logCollectPluginBinary {
		t.Fatalf("plugin 缺失仍必须拒绝：%v", missing)
	}
}

// 判定层端到端仿真：真实 shell 执行生成的探测命令，把「命令 → 输出 → 阻断决策」
// 整条链跑通。全新安装现场只放 plugin/manifest，不建 tools 目录，进度文件必须缺席。
func TestPluginProbeCommandEndToEndOnFreshInstall(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("依赖 sh 的判定层仿真只在 POSIX 环境执行")
	}
	dir := t.TempDir()
	command := strings.ReplaceAll(buildPluginProbeCommand(), "/sf/data/local/acli/plugins/log_collect", dir)
	writeArtifact := func(name, content string) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatalf("准备安装期文件失败：%v", err)
		}
	}
	writeArtifact("plugin", "binary")
	writeArtifact("manifest.json", "{}")
	run := func() []string {
		out, err := exec.Command("sh", "-c", command).CombinedOutput()
		if err != nil {
			t.Fatalf("探测命令执行失败：%v\n%s", err, out)
		}
		return parsePluginProbe(string(out))
	}
	if missing := run(); len(missing) != 0 {
		t.Fatalf("全新安装无 progress.json 时应放行，实际判缺失：%v", missing)
	}
	if err := os.Remove(filepath.Join(dir, "plugin")); err != nil {
		t.Fatalf("移除 plugin 失败：%v", err)
	}
	missing := run()
	if len(missing) != 1 || !strings.HasSuffix(missing[0], "/plugin") {
		t.Fatalf("plugin 缺失时仍必须拒绝：%v", missing)
	}
}

// 运行时进度文件仍保留在失败现场快照里，用于区分「采集没启动」与「进度没落盘」。
func TestFailureSnapshotKeepsRuntimeProgressEvidence(t *testing.T) {
	command := buildFailureSnapshotCommand()
	if !strings.Contains(command, logCollectRuntimeProgress) {
		t.Fatalf("失败快照应保留运行时进度文件作为观测证据：%s", command)
	}
}

func TestParseFailureSnapshot(t *testing.T) {
	output := strings.Join([]string{
		"FILE /sf/data/local/acli/plugins/log_collect/log_collect.log 23308",
		"EMPTY /sf/data/local/acli/plugins/log_collect/plugin",
		"MISSING /sf/data/local/acli/plugins/log_collect/tools/lmt_collect_info/progress.json",
		"DISK /dev/sda1 100 100 0 100% /sf",
	}, "\n")
	files, disk := parseFailureSnapshot(output)
	if len(files) != 3 {
		t.Fatalf("应解析 3 条文件事实：%v", files)
	}
	if files[0]["size"] != 23308 || files[1]["empty"] != true || files[2]["exists"] != false {
		t.Fatalf("文件事实解析不对：%v", files)
	}
	if disk["raw"] != "/dev/sda1 100 100 0 100% /sf" {
		t.Fatalf("磁盘事实解析不对：%v", disk)
	}
}

// 备份失败必须同时留下 reason 与 stderr：只留 reason 时，现场无法区分
// 「文件不存在」「权限不足」「磁盘满」（Q2026112300134）。
func TestSpcFailureDetailKeepsReasonAndStderr(t *testing.T) {
	detail := SpcFailureDetail(
		"SPC_PREPARE_FAIL reason=backup_failed\n",
		"SPC_PREPARE_FAIL reason=backup_failed\ncp: cannot stat '/x/vm_suspend.json': No such file or directory\n",
		"backup_failed",
	)
	if !strings.Contains(detail, "reason=backup_failed") {
		t.Fatalf("detail 必须保留 reason：%q", detail)
	}
	if !strings.Contains(detail, "No such file or directory") {
		t.Fatalf("detail 必须保留 stderr 真因：%q", detail)
	}
}

// prepare 命令不能把 cp 的 stderr 丢进 /dev/null：那是 ENOENT/EACCES 的唯一来源。
func TestBuildSpcPrepareCommandKeepsCpStderr(t *testing.T) {
	command := buildSpcPrepareCommand("/x/core.json", "/x/core.json.platform.task.bak")
	cut := strings.Index(command, "cmp -s")
	if cut < 0 {
		t.Fatalf("prepare 命令缺少保真校验：%s", command)
	}
	// 只看「本次备份」那一段：前面的自愈 cp 允许 2>/dev/null（失败不致命），
	// 备份 cp 不行——它失败就代表没有原件可回滚，必须留真因。
	start := strings.Index(command, "if [ ! -f ")
	if start < 0 || start > cut {
		t.Fatalf("prepare 命令缺少备份分支：%s", command)
	}
	backupSegment := command[start:cut]
	if !strings.Contains(backupSegment, "cp -p") {
		t.Fatalf("prepare 命令缺少备份动作：%s", backupSegment)
	}
	if strings.Contains(backupSegment, "2>/dev/null") {
		t.Fatalf("备份 cp 不能丢弃 stderr：%s", backupSegment)
	}
}
