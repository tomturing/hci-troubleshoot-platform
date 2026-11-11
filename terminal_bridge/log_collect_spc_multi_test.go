package main

// 多槽位清单与目录探测回归测试：这里的每一条都对应一种"看起来成功、实际按错清单收集"
// 的失败形态，必须挡在改节点文件之前。

import (
	"strings"
	"testing"
)

// 现场导出的 spc 目录在 /sf/data/local 下第 7 层，旧命令 maxdepth 6 会漏掉。
func TestBuildSpcFindCommandDepth(t *testing.T) {
	command := buildSpcFindCommand()
	if !strings.Contains(command, "-maxdepth 10") {
		t.Fatalf("目录探测深度不足：%s", command)
	}
	if !strings.Contains(command, "-path '*support_bundle_cluster*'") {
		t.Fatalf("目录探测缺少版本过滤：%s", command)
	}
}

func TestParseSpcDirsRealDeployPath(t *testing.T) {
	const real = "/sf/data/local/acli/plugins/log_collect/tools/lmt_collect_info/support_bundle_cluster/spc"
	if got := parseSpcDirs(real + "\n"); len(got) != 1 || got[0] != real {
		t.Fatalf("真实部署路径解析失败：%v", got)
	}
	if got, err := pickSpcDir(real + "\n"); err != nil || got != real {
		t.Fatalf("真实部署路径应唯一命中：got=%s err=%v", got, err)
	}
}

func spcTestFile(slot, path string) SpcFile {
	content := `[{"action":"copy","path":"` + path + `"}]`
	return SpcFile{Slot: slot, Content: content, SHA256: spcSHA256(content)}
}

// 逐槽位载荷必须 fail-closed：任何一份清单不合规都不能去改节点文件。
func TestParseSpcFiles(t *testing.T) {
	t.Run("多槽位合法载荷按序返回并复算摘要", func(t *testing.T) {
		msg := InMessage{SpcFiles: []SpcFile{
			spcTestFile("vm_start", "/var/log/vm.log"),
			spcTestFile("network", "/var/log/net.log"),
		}}
		files, rejection := parseSpcFiles(msg)
		if rejection != nil {
			t.Fatalf("合法载荷被拒：%+v", rejection)
		}
		if len(files) != 2 || files[0].Slot != "vm_start" || files[1].Slot != "network" {
			t.Fatalf("槽位顺序被改：%+v", files)
		}
		if files[0].SHA256 != spcSHA256(files[0].Content) {
			t.Fatalf("摘要未独立复算：%+v", files[0])
		}
	})

	t.Run("空载荷返回 nil 走单槽位兼容", func(t *testing.T) {
		files, rejection := parseSpcFiles(InMessage{})
		if rejection != nil || files != nil {
			t.Fatalf("空载荷应走兼容路径：files=%v rejection=%+v", files, rejection)
		}
	})

	t.Run("与单槽位通道互斥", func(t *testing.T) {
		msg := InMessage{SpcJSON: `[{"action":"copy","path":"/var/log/a"}]`, SpcFiles: []SpcFile{spcTestFile("vm_start", "/var/log/b")}}
		_, rejection := parseSpcFiles(msg)
		if rejection == nil || rejection.ErrType != "spc_config_conflict" {
			t.Fatalf("两个通道同时下发必须拒绝：%+v", rejection)
		}
	})

	t.Run("非 acli 模块槽位被拒", func(t *testing.T) {
		_, rejection := parseSpcFiles(InMessage{SpcFiles: []SpcFile{spcTestFile("compute", "/var/log/a")}})
		if rejection == nil || rejection.ErrType != "invalid_spc" {
			t.Fatalf("compute 不在 -m 白名单必须拒绝：%+v", rejection)
		}
	})

	t.Run("重复槽位被拒", func(t *testing.T) {
		_, rejection := parseSpcFiles(InMessage{SpcFiles: []SpcFile{
			spcTestFile("vm_start", "/var/log/a"), spcTestFile("vm_start", "/var/log/b"),
		}})
		if rejection == nil {
			t.Fatal("重复槽位会导致同一文件写两次，必须拒绝")
		}
	})

	t.Run("摘要不符被拒", func(t *testing.T) {
		file := spcTestFile("vm_start", "/var/log/a")
		file.SHA256 = strings.Repeat("0", 64)
		_, rejection := parseSpcFiles(InMessage{SpcFiles: []SpcFile{file}})
		if rejection == nil || rejection.ErrType != "spc_digest_mismatch" {
			t.Fatalf("摘要不符必须拒绝：%+v", rejection)
		}
	})

	t.Run("非法清单内容被拒", func(t *testing.T) {
		file := SpcFile{Slot: "vm_start", Content: `{"action":"copy"}`, SHA256: spcSHA256(`{"action":"copy"}`)}
		_, rejection := parseSpcFiles(InMessage{SpcFiles: []SpcFile{file}})
		if rejection == nil || rejection.ErrType != "invalid_spc" {
			t.Fatalf("非数组清单必须拒绝：%+v", rejection)
		}
	})
}

func TestSpcApplySpecFiles(t *testing.T) {
	single := spcApplySpec{Slot: "core", TaskID: "task-1", Content: `[{"action":"copy","path":"/var/log/a"}]`}
	if got := single.files(); len(got) != 1 || got[0].Slot != "core" || got[0].Content != single.Content {
		t.Fatalf("单槽位归一化失败：%+v", got)
	}
	multi := spcApplySpec{Slot: "core", Files: []spcSlotFile{{Slot: "vm_start"}, {Slot: "network"}}}
	if got := multi.files(); len(got) != 2 || got[0].Slot != "vm_start" || got[1].Slot != "network" {
		t.Fatalf("多槽位必须原样保序：%+v", got)
	}
}

// 多模块命令必须按槽位集合拼 -m，否则文件写了却没被采集。
func TestBuildLogCollectCreateCommandMultiModules(t *testing.T) {
	req := InMessage{
		TicketNo: "Q2026110302155",
		Modules:  []string{"vm_start", "network"},
		DateFrom: "2026-11-01",
		DateTo:   "2026-11-02",
		IPs:      "all",
	}
	command, errType, errMsg := buildLogCollectCreateCommand(req)
	if errType != "" {
		t.Fatalf("多模块下发被拒：%s %s", errType, errMsg)
	}
	if !strings.Contains(command, "vm_start,network") {
		t.Fatalf("命令未带上全部槽位：%s", command)
	}
}

func TestSpcHostTrailIncludesSlots(t *testing.T) {
	host := spcHostState{
		IP: "10.0.0.1", Written: true,
		Slots: []spcHostSlotState{{Slot: "vm_start", Written: true}, {Slot: "network", Written: true}},
	}
	item := spcHostTrailItem(host, "error")
	slots, ok := item["slots"].([]spcHostSlotState)
	if !ok || len(slots) != 2 {
		t.Fatalf("回执丢失逐槽位证据：%v", item)
	}
}
