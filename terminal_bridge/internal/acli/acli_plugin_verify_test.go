package acli

// 安装后逐节点插件落盘校验的单测：安装器退出码 0 不等于每台节点都装好，
// 这里的判定决定"分发假成功"会不会再次被掩盖（Q2026111700614）。
// S4b（B8）：验证范围由 descriptor 声明，覆盖全部出厂内置插件，不再只验
// log_collect 单路径——其余插件的空目录同样会被"安装成功"掩盖。

import (
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"

	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/shared/remote"
)

func TestBuildPluginLandingProbeCommand(t *testing.T) {
	command := buildPluginLandingProbeCommand(acliLandingDescriptor())
	// 可执行入口必须判 -x：空目录残骸不算落盘。
	if !strings.Contains(command, "[ -x "+remote.ShellQuote(logCollectPluginBinary)+" ]") {
		t.Fatalf("必须校验插件可执行（-x），空目录残骸不算落盘：%s", command)
	}
	// 每个内置插件目录都必须被探测（B8：不再只验 log_collect 单路径）。
	for _, name := range acliBuiltinPlugins {
		dir := remote.ShellQuote(acliPluginBaseDir + "/" + name)
		if !strings.Contains(command, "[ -d "+dir+" ]") {
			t.Fatalf("内置插件 %s 的目录不在验证范围（B8）：%s", name, command)
		}
		if !strings.Contains(command, "ls -A "+dir) {
			t.Fatalf("目录目标必须判非空——空目录正是分发假成功的事故形态：%s", command)
		}
	}
	if !strings.Contains(command, "printf 'OK") || !strings.Contains(command, "printf 'MISS") {
		t.Fatalf("逐目标必须回显可判定的 OK/MISS 标记：%s", command)
	}
}

func TestParsePluginLandingProbe(t *testing.T) {
	d := acliLandingDescriptor()
	var okLines []string
	for _, f := range d.Files {
		okLines = append(okLines, "OK\t"+f.Path)
	}
	allOK := strings.Join(okLines, "\n") + "\n"
	present := parsePluginLandingProbe(allOK)
	for _, f := range d.Files {
		if !present[f.Path] {
			t.Fatalf("全部 OK 时 %s 应判落盘", f.Path)
		}
	}

	// B8 缺陷形态：只有 log_collect 落盘，其余插件目录为空/缺失。
	// 旧实现只验 log_collect 会把这种节点判成"全部落盘"。
	partial := "OK\t" + logCollectPluginBinary + "\nMISS\t" + acliPluginBaseDir + "/asys\n"
	present = parsePluginLandingProbe(partial)
	if !present[logCollectPluginBinary] {
		t.Fatal("log_collect 已落盘应判 present")
	}
	if present[acliPluginBaseDir+"/asys"] {
		t.Fatal("MISS 行不得判落盘")
	}
	missingCount := 0
	for _, f := range d.Files {
		if !present[f.Path] {
			missingCount++
		}
	}
	if missingCount != len(d.Files)-1 {
		t.Fatalf("除 log_collect 外全部目标应判缺失，got %d", missingCount)
	}

	// 读不到/没回显一律不算落盘，绝不把"没验过"当"验过了"。
	for _, output := range []string{"", "Permission denied\n", "OKAY\t/x\n", "OK\n", "ok\t/x\n"} {
		if got := parsePluginLandingProbe(output); len(got) != 0 {
			t.Fatalf("parsePluginLandingProbe(%q) 不应认任何落盘：%v", output, got)
		}
	}
	// Windows 换行与前后空白不影响判定。
	if got := parsePluginLandingProbe(" OK\t/x \r\n"); !got["/x"] {
		t.Fatal("CRLF/空白规整后仍应认 OK 标记")
	}
}

func TestAcliLandingDescriptorDeclaresAllTargets(t *testing.T) {
	d := acliLandingDescriptor()
	if d.Kind != reconcile.KindPluginBundle {
		t.Fatalf("落盘验证 descriptor 应为 plugin_bundle，got %q", d.Kind)
	}
	if len(d.Files) != len(acliBuiltinPlugins)+1 {
		t.Fatalf("目标数应为内置插件目录数+log_collect 可执行入口，got %d", len(d.Files))
	}
	seen := make(map[string]bool)
	for _, f := range d.Files {
		if seen[f.Path] {
			t.Fatalf("目标路径重复声明：%s", f.Path)
		}
		seen[f.Path] = true
	}
	if !seen[logCollectPluginBinary] {
		t.Fatal("log_collect 可执行入口必须保留强校验")
	}
}

// TestAcliBuiltinPluginsMatchBackend 钉住桥侧清单与后端 BUILTIN_PLUGINS 同源：
// 分发范围（安装器出厂内置）与验证范围（descriptor 声明）一旦漂移，
// B8 的"验证覆盖分发"就名存实亡。
func TestAcliBuiltinPluginsMatchBackend(t *testing.T) {
	raw, err := os.ReadFile("../../../backend/case-service/app/domain/acli_plugin_spec.py")
	if err != nil {
		t.Skipf("后端源码不可读（独立部署形态）：%v", err)
	}
	block := regexp.MustCompile(`(?s)BUILTIN_PLUGINS\s*=\s*\{(.*?)\}`).FindSubmatch(raw)
	if block == nil {
		t.Fatal("后端 BUILTIN_PLUGINS 清单未找到，守卫失效")
	}
	keys := regexp.MustCompile(`"([a-z_]+)"\s*:`).FindAllStringSubmatch(string(block[1]), -1)
	var backend []string
	for _, m := range keys {
		backend = append(backend, m[1])
	}
	bridge := append([]string(nil), acliBuiltinPlugins...)
	sort.Strings(backend)
	sort.Strings(bridge)
	if strings.Join(backend, ",") != strings.Join(bridge, ",") {
		t.Fatalf("内置插件清单漂移：backend=%v bridge=%v", backend, bridge)
	}
}

func TestMissingPluginNodes(t *testing.T) {
	states := []nodePluginState{
		{IP: "10.0.0.1", Present: true},
		{IP: "10.0.0.2", Present: false, Detail: "ssh: connect refused"},
		{IP: "10.0.0.3", Present: true},
		{IP: "10.0.0.4", Present: false},
	}
	got := missingPluginNodes(states)
	if len(got) != 2 || got[0] != "10.0.0.2" || got[1] != "10.0.0.4" {
		t.Fatalf("未落盘节点汇总错误：%v", got)
	}
	if missing := missingPluginNodes(nil); len(missing) != 0 {
		t.Fatalf("无节点清单时不应报缺失：%v", missing)
	}
	allOK := []nodePluginState{{IP: "10.0.0.1", Present: true}}
	if missing := missingPluginNodes(allOK); len(missing) != 0 {
		t.Fatalf("全部落盘时不应报缺失：%v", missing)
	}
}
