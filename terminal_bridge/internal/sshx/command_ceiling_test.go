package sshx

import (
	"context"
	"testing"
	"time"
)

// 没有 deadline 的 ctx 必须被套上兜底上限，否则半开连接会让命令永久挂起。
func TestCapCommandContextAppliesCeiling(t *testing.T) {
	capped, cancel := capCommandContext(context.Background(), 5*time.Second)
	defer cancel()
	deadline, ok := capped.Deadline()
	if !ok {
		t.Fatal("期望套上兜底 deadline，实际没有")
	}
	if remaining := time.Until(deadline); remaining > 5*time.Second || remaining <= 0 {
		t.Fatalf("兜底 deadline 剩余 %s，期望 (0, 5s]", remaining)
	}
}

// 调用方已有更紧的 deadline 时必须沿用，绝不放宽：探测类命令的 15 秒不能被放大。
func TestCapCommandContextKeepsTighterDeadline(t *testing.T) {
	parent, cancelParent := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelParent()
	capped, cancel := capCommandContext(parent, 20*time.Minute)
	defer cancel()
	if capped != parent {
		t.Fatal("调用方 deadline 更紧时应原样返回父 ctx")
	}
	deadline, _ := capped.Deadline()
	if remaining := time.Until(deadline); remaining > 2*time.Second {
		t.Fatalf("剩余 %s，期望 <= 2s", remaining)
	}
}

// 调用方 deadline 更宽松时必须收紧，否则"兜底"形同虚设。
func TestCapCommandContextTightensLooseDeadline(t *testing.T) {
	parent, cancelParent := context.WithTimeout(context.Background(), time.Hour)
	defer cancelParent()
	capped, cancel := capCommandContext(parent, time.Minute)
	defer cancel()
	deadline, ok := capped.Deadline()
	if !ok {
		t.Fatal("期望被收紧，实际没有 deadline")
	}
	if remaining := time.Until(deadline); remaining > time.Minute {
		t.Fatalf("剩余 %s，期望 <= 1m", remaining)
	}
}

// 配错环境变量不能变成"关掉保护"：非法值、过小值都退回默认。
func TestQuickCommandCeilingRejectsInvalidConfig(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want time.Duration
	}{
		{name: "未配置", raw: "", want: quickCommandCeilingDefault},
		{name: "非数字", raw: "abc", want: quickCommandCeilingDefault},
		{name: "零", raw: "0", want: quickCommandCeilingDefault},
		{name: "负数", raw: "-30", want: quickCommandCeilingDefault},
		{name: "过小", raw: "3", want: quickCommandCeilingDefault},
		{name: "合法覆盖", raw: "600", want: 10 * time.Minute},
	}
	for _, tc := range cases {
		t.Setenv("HCI_BRIDGE_COMMAND_CEILING_SEC", tc.raw)
		if got := quickCommandCeiling(); got != tc.want {
			t.Errorf("%s：ceiling=%s，期望 %s", tc.name, got, tc.want)
		}
	}
}

// 兜底上限必须明显大于探测预算，否则会误杀节点内文件复制。
func TestQuickCommandCeilingDefaultIsGenerous(t *testing.T) {
	if quickCommandCeilingDefault < 10*time.Minute {
		t.Fatalf("兜底上限 %s 过小，会误杀 GB 级文件复制", quickCommandCeilingDefault)
	}
}
