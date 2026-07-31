package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseCommand_ACLILogGet(t *testing.T) {
	cmd := `acli log get --path /sf/log/today/sfvt_vtpdaemon.log --keyword "too many file" --host node-001 --container host`
	fp := ParseCommand(cmd)
	if fp.Tool != "log" {
		t.Fatalf("Tool = %q, want log", fp.Tool)
	}
	if fp.AcquisitionKey() != "qfk_log" {
		t.Fatalf("AcquisitionKey = %q, want qfk_log", fp.AcquisitionKey())
	}
	if fp.ResourceKeyword != "too many file" {
		t.Fatalf("ResourceKeyword = %q", fp.ResourceKeyword)
	}
	if fp.Host != "node-001" || fp.Container != "host" {
		t.Fatalf("host/container 解析错误: %q/%q", fp.Host, fp.Container)
	}
}

func TestRouter_PositiveAndNegativeIsolation(t *testing.T) {
	dir := t.TempDir()
	pos := `{"fixture_id":"p","tool":"qfk_log","variant":"positive","command_match":"acli\\s+log\\s+get","stdout":"ERROR too many file descriptors","exit_code":0,"status":"published"}`
	neg := `{"fixture_id":"n","scenario_id":"scenario-0002","tool":"qfk_log","variant":"negative","command_match":"acli\\s+log\\s+get","stdout":"INFO ok","exit_code":0,"status":"published"}`
	if err := os.WriteFile(filepath.Join(dir, "pos.json"), []byte(pos), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "neg.json"), []byte(neg), 0o644); err != nil {
		t.Fatal(err)
	}

	fixtures, err := loadFixtures(dir)
	if err != nil {
		t.Fatal(err)
	}
	r := NewFixtureRouter(fixtures)
	fp := ParseCommand(`acli log get --keyword "too many file"`)

	// 默认（无 scenario）命中 positive
	f, err := r.Resolve(LookupContext{}, fp)
	if err != nil || f.FixtureID != "p" {
		t.Fatalf("默认应命中 positive, got %v err=%v", f, err)
	}

	// 指定 scenario-0002 命中 negative（演示隔离）
	f, err = r.Resolve(LookupContext{ScenarioID: "scenario-0002"}, fp)
	if err != nil || f.FixtureID != "n" {
		t.Fatalf("scenario-0002 应命中 negative, got %v err=%v", f, err)
	}
}

func TestRouter_FailClosed(t *testing.T) {
	dir := t.TempDir()
	data := `{"fixture_id":"p","tool":"qfk_log","command_match":"acli\\s+log\\s+get","stdout":"x","status":"published"}`
	_ = os.WriteFile(filepath.Join(dir, "p.json"), []byte(data), 0o644)
	fixtures, _ := loadFixtures(dir)
	r := NewFixtureRouter(fixtures)

	// 完全不匹配的命令必须 fail closed
	_, err := r.Resolve(LookupContext{}, ParseCommand("acli vm list"))
	if err != ErrFixtureNotFound {
		t.Fatalf("期望 ErrFixtureNotFound, got %v", err)
	}
}
