package main

// 多槽位清单与目录探测回归测试：这里的每一条都对应一种"看起来成功、实际按错清单收集"
// 的失败形态，必须挡在改节点文件之前。

import (
	"strings"
	"terminal_bridge/internal/shared/models"
	"testing"
)

// 固定路径方案：探测脚本只检查 spcDeployDir 是否存在，不得再用 find。
func TestBuildSpcDirProbeUsesFixedPath(t *testing.T) {
	command := buildSpcFindCommand()
	if strings.Contains(command, "find ") {
		t.Fatalf("固定路径方案下不应再 find 探测：%s", command)
	}
	if !strings.Contains(command, spcDeployDir) {
		t.Fatalf("探测脚本缺少固定路径：%s", command)
	}
	if !strings.Contains(command, "[ -d ") {
		t.Fatalf("探测脚本缺少存在性检查：%s", command)
	}
}

// acli 重装会清掉插件目录（含 spc）：探测必须"缺失即创建"，否则被清的
// 固定目录会让自定义/默认场景下发永远卡在 spc_dir_missing（Q2026111700614）。
func TestBuildSpcDirProbeRecreatesMissingDir(t *testing.T) {
	command := buildSpcDirProbe()
	mkdir := strings.Index(command, "mkdir -p")
	if mkdir < 0 {
		t.Fatalf("缺失即创建：探测脚本必须含 mkdir -p：%s", command)
	}
	if !strings.Contains(command, "[ -d "+shellQuote(spcDeployDir)+" ] || mkdir -p "+shellQuote(spcDeployDir)) {
		t.Fatalf("mkdir 必须只在目录缺失时触发且指向固定路径：%s", command)
	}
	// 创建之后仍要回读存在性并回显路径，pickSpcDir 的解析语义不变。
	if !strings.Contains(command, "[ -d "+shellQuote(spcDeployDir)+" ] && printf") {
		t.Fatalf("创建后必须回读存在性并回显路径：%s", command)
	}
}

func TestParseSpcDirsRealDeployPath(t *testing.T) {
	const real = spcDeployDir
	if got := parseSpcDirs(real + "\n"); len(got) != 1 || got[0] != real {
		t.Fatalf("真实部署路径解析失败：%v", got)
	}
	if got, err := pickSpcDir(real + "\n"); err != nil || got != real {
		t.Fatalf("真实部署路径应命中：got=%s err=%v", got, err)
	}
}

// 现场双目录并存属正常现象：即使探测输出混入 aDeploy 下的同名 spc，
// 也必须只返回 acli 插件目录下的固定路径，不得再报"目录不唯一"。
func TestPickSpcDirIgnoresOtherSpcDirs(t *testing.T) {
	const other = "/sf/data/local/aDeploy/aDeploy/atools/log/support_bundle_cluster/spc"
	got, err := pickSpcDir(other + "\n" + spcDeployDir + "\n")
	if err != nil || got != spcDeployDir {
		t.Fatalf("双目录应只取固定路径：got=%q err=%v", got, err)
	}
	if dir, code := spcResolveDir(other + "\n" + spcDeployDir + "\n"); code != "" || dir != spcDeployDir {
		t.Fatalf("spcResolveDir 应只取固定路径且无歧义码：dir=%q code=%q", dir, code)
	}
}

func spcTestFile(slot, path string) models.SpcFile {
	content := `[{"action":"copy","path":"` + path + `"}]`
	return models.SpcFile{Slot: slot, Content: content, SHA256: spcSHA256(content)}
}

// 逐槽位载荷必须 fail-closed：任何一份清单不合规都不能去改节点文件。
func TestParseSpcFiles(t *testing.T) {
	t.Run("多槽位合法载荷按序返回并复算摘要", func(t *testing.T) {
		msg := models.InMessage{SpcFiles: []models.SpcFile{
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
		files, rejection := parseSpcFiles(models.InMessage{})
		if rejection != nil || files != nil {
			t.Fatalf("空载荷应走兼容路径：files=%v rejection=%+v", files, rejection)
		}
	})

	t.Run("与单槽位通道互斥", func(t *testing.T) {
		msg := models.InMessage{SpcJSON: `[{"action":"copy","path":"/var/log/a"}]`, SpcFiles: []models.SpcFile{spcTestFile("vm_start", "/var/log/b")}}
		_, rejection := parseSpcFiles(msg)
		if rejection == nil || rejection.ErrType != "spc_config_conflict" {
			t.Fatalf("两个通道同时下发必须拒绝：%+v", rejection)
		}
	})

	t.Run("非 acli 模块槽位被拒", func(t *testing.T) {
		_, rejection := parseSpcFiles(models.InMessage{SpcFiles: []models.SpcFile{spcTestFile("compute", "/var/log/a")}})
		if rejection == nil || rejection.ErrType != "invalid_spc" {
			t.Fatalf("compute 不在 -m 白名单必须拒绝：%+v", rejection)
		}
	})

	t.Run("重复槽位被拒", func(t *testing.T) {
		_, rejection := parseSpcFiles(models.InMessage{SpcFiles: []models.SpcFile{
			spcTestFile("vm_start", "/var/log/a"), spcTestFile("vm_start", "/var/log/b"),
		}})
		if rejection == nil {
			t.Fatal("重复槽位会导致同一文件写两次，必须拒绝")
		}
	})

	t.Run("摘要不符被拒", func(t *testing.T) {
		file := spcTestFile("vm_start", "/var/log/a")
		file.SHA256 = strings.Repeat("0", 64)
		_, rejection := parseSpcFiles(models.InMessage{SpcFiles: []models.SpcFile{file}})
		if rejection == nil || rejection.ErrType != "spc_digest_mismatch" {
			t.Fatalf("摘要不符必须拒绝：%+v", rejection)
		}
	})

	t.Run("非法清单内容被拒", func(t *testing.T) {
		file := models.SpcFile{Slot: "vm_start", Content: `{"action":"copy"}`, SHA256: spcSHA256(`{"action":"copy"}`)}
		_, rejection := parseSpcFiles(models.InMessage{SpcFiles: []models.SpcFile{file}})
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
	req := models.InMessage{
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
