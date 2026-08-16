package fixture

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
)

func testManifest(t *testing.T, routes []Route) []byte {
	t.Helper()
	manifest := Manifest{
		SchemaVersion: SchemaVersion,
		Bundle:        BundleRef{Status: "published"},
		KBD:           KBDRef{SupportID: "27123", Revision: 24, Checksum: "sha256:kbd"},
		Contracts:     Contracts{ToolRevision: "tool-r24", PolicyRevision: "policy-r2"},
		Variables:     map[string]string{"VM": "271230001"},
		Limits:        Limits{MaxRoutes: 100, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes:        routes,
	}
	manifest.Bundle.Digest = ComputeBundleDigest(manifest)
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestPublishedKBD27123BundleLoads(t *testing.T) {
	path := filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json")
	router, err := Load(path)
	if err != nil {
		t.Fatalf("published KBD 27123 bundle must load: %v", err)
	}
	if router.KBD().SupportID != "27123" || router.BundleDigest() == "" {
		t.Fatalf("unexpected published bundle identity: kbd=%+v digest=%q", router.KBD(), router.BundleDigest())
	}
}

func TestPublishedKBD27123BundleMatchesAgentSystemCommands(t *testing.T) {
	router, err := Load(filepath.Join("..", "..", "testdata", "kbd-27123-fixture-manifest.json"))
	if err != nil {
		t.Fatalf("load published bundle: %v", err)
	}
	for _, command := range []string{
		"acli --timeout 120 system lsof",
		"acli --timeout 120 system ps -p 9527 -o cmd=",
	} {
		result, matchErr := router.Match(command, "positive-realistic", "SIM-HCI-NODE-01", "host")
		if matchErr != nil {
			t.Fatalf("current Agent command %q must match published bundle: %v", command, matchErr)
		}
		if result.ExitCode != 0 || result.SignalID == "" {
			t.Fatalf("current Agent command %q returned invalid fixture result: %+v", command, result)
		}
	}
}

func TestPublishedKBD23821BundleMatchesAgentLogCommand(t *testing.T) {
	router, err := Load(filepath.Join("..", "..", "..", "deploy", "helm", "hci-sim", "files", "kbd-23821-fixture-manifest.json"))
	if err != nil {
		t.Fatalf("加载 KBD23821 发布 bundle 失败: %v", err)
	}
	legacyCommand := "acli log get -E -k 'Completed|info\\ block-jobs' -f sfvt_vtpdaemon.log -p /sf/log/18/vt -t '2023-09-18 17:19:07'"
	if _, err := router.Match(legacyCommand, "positive-realistic", "SIM-HCI-NODE-01", "host"); err == nil {
		t.Fatal("旧日志命令缺少连字符转义，不得命中精确路由")
	}
	command := "acli log get -E -k 'Completed|info\\ block\\-jobs' -f sfvt_vtpdaemon.log -p /sf/log/18/vt -t '2023-09-18 17:19:07'"
	result, err := router.Match(command, "positive-realistic", "SIM-HCI-NODE-01", "host")
	if err != nil {
		t.Fatalf("Agent 当前日志命令必须命中 KBD23821 发布 bundle: %v", err)
	}
	if result.ExitCode != 0 || result.FixtureID != "sig-002-qfk-log-delta-23821" {
		t.Fatalf("KBD23821 日志 fixture 返回无效结果: %+v", result)
	}
}

func route(id, variant string, argv []string) Route {
	return Route{
		ID: id, Variant: variant,
		RouteKey: RouteKey{Tool: "acli", AcquisitionKey: "acli:system", Argv: argv, Node: "SIM-NODE", Container: "host"},
		Result:   ResultDef{ExitCode: 0, Stdout: "vm={{VM}}\n"}, Fault: FaultDef{Type: FaultNone},
	}
}

func TestRouterSeparatesVariantsAndRendersVariables(t *testing.T) {
	argv := []string{"acli", "system", "lsof"}
	router, err := Parse(testManifest(t, []Route{route("positive", "positive-realistic", argv), route("negative", "negative", argv)}))
	if err != nil {
		t.Fatal(err)
	}
	positive, err := router.Match("acli system lsof", "positive-realistic", "SIM-NODE", "host")
	if err != nil || !strings.Contains(positive.Stdout, "271230001") {
		t.Fatalf("positive=%+v err=%v", positive, err)
	}
	negative, err := router.Match("acli system lsof", "negative", "SIM-NODE", "host")
	if err != nil || negative.FixtureID != "negative" {
		t.Fatalf("negative=%+v err=%v", negative, err)
	}
}

func TestRouterRejectsAmbiguousRoutes(t *testing.T) {
	argv := []string{"acli", "system", "lsof"}
	_, err := Parse(testManifest(t, []Route{route("one", "positive-realistic", argv), route("two", "positive-realistic", argv)}))
	if err == nil || !strings.Contains(err.Error(), "歧义") {
		t.Fatalf("expected ambiguity error, got %v", err)
	}
}

func TestParseRejectsUnknownFieldsAndDigestDrift(t *testing.T) {
	raw := testManifest(t, []Route{route("one", "positive-realistic", []string{"acli", "system", "lsof"})})
	if _, err := Parse(raw); err != nil {
		t.Fatal(err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	parsed["unknown"] = true
	unknown, _ := json.Marshal(parsed)
	if _, err := Parse(unknown); err == nil {
		t.Fatal("unknown field must fail closed")
	}
	parsed = map[string]any{}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	parsed["bundle"].(map[string]any)["digest"] = "sha256:bad"
	tampered, _ := json.Marshal(parsed)
	if _, err := Parse(tampered); err == nil || !strings.Contains(err.Error(), "digest") {
		t.Fatalf("digest drift must fail: %v", err)
	}
}

func TestLexRejectsShellAndNormalizesLongFlags(t *testing.T) {
	argv, err := Lex(`acli --formatter=json task get -l 1`)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"acli", "--formatter", "json", "task", "get", "-l", "1"}
	if !sameStrings(argv, want) {
		t.Fatalf("argv=%q want=%q", argv, want)
	}
	for _, command := range []string{"acli x; id", "acli $(id)", "acli 'unclosed", "acli x | cat", "acli\nx"} {
		if _, err := Lex(command); err == nil {
			t.Fatalf("command %q must be denied", command)
		}
	}
}

func TestRouteKeyBindsNodeContainerAndVariant(t *testing.T) {
	router, err := Parse(testManifest(t, []Route{route("one", "positive-realistic", []string{"acli", "system", "lsof"})}))
	if err != nil {
		t.Fatal(err)
	}
	for _, input := range []struct{ variant, node, container string }{{"negative", "SIM-NODE", "host"}, {"positive-realistic", "OTHER", "host"}, {"positive-realistic", "SIM-NODE", "vm"}} {
		if _, err := router.Match("acli system lsof", input.variant, input.node, input.container); err == nil {
			t.Fatalf("input %+v must not match", input)
		}
	}
}
