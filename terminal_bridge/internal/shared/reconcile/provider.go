package reconcile

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// provider.go —— 期望值来源的一等公民（收口设计 §2.3）。
//
// 把"从哪里获取期望状态"变成可插拔接口：WS 原文（config/spc）、
// HTTP Range 探测（acli 阶段一）、平台票据 manifest（acli 阶段二）
// 都实现同一个 Provider，骨架不关心来源。

// SyncRequest 是一次同步请求的输入。
//
// Raw 是前端透传的原文：**前端是不可信链路**，Provider 必须二次校验，
// 校验不过一律 fail-closed，不写节点。
type SyncRequest struct {
	Kind   string
	NodeIP string
	TaskID string
	Raw    string
	SHA256 string // 平台签发的摘要（可选；给了就必须自洽）
	Path   string // 目标路径（调用方按 task/部署隔离算好）
	Dir    string // 写前需重建的目录
	Mode   string
	// Slot 是 spc 槽位一类的强类型语义位（补丁 5：需要新语义就扩字段）。
	Slot     string
	Metadata map[string]string
}

// Provider 把"从哪里获取期望状态"变成可插拔的接口。
type Provider interface {
	Desired(ctx context.Context, req SyncRequest) ([]DesiredState, error)
}

// InlineMessage 用于 spc/config：期望状态由 WS InMessage 直接携带。
//
// Validate 必填：产品 schema（config 只允许 paths 键、spc 必须是合法数组）
// 留在调用方，骨架只负责"没有二次校验就不写节点"这条硬规则。
type InlineMessage struct {
	Validate func(raw string) error
	Policy   ReconcilePolicy
	Smoke    SmokeSpec
	Version  VersionSpec
}

// Desired 把原文变成单文件期望态。
func (p InlineMessage) Desired(ctx context.Context, req SyncRequest) ([]DesiredState, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if p.Validate == nil {
		return nil, fmt.Errorf("%s：inline 来源必须提供二次校验（前端是不可信链路）", CodeDescriptorInvalid)
	}
	if strings.TrimSpace(req.Raw) == "" {
		return nil, fmt.Errorf("%s：期望态原文为空", CodeDescriptorInvalid)
	}
	if err := p.Validate(req.Raw); err != nil {
		return nil, err
	}
	if req.SHA256 != "" {
		sum := sha256.Sum256([]byte(req.Raw))
		if !strings.EqualFold(hex.EncodeToString(sum[:]), strings.TrimSpace(req.SHA256)) {
			return nil, fmt.Errorf("%s：原文摘要与平台签发值不符", CodeDescriptorInvalid)
		}
	}
	if !strings.HasPrefix(req.Path, "/") || strings.Contains(req.Path, "..") {
		return nil, fmt.Errorf("%s：非法目标路径 %q", CodeDescriptorInvalid, req.Path)
	}
	d := DesiredState{
		Kind:     req.Kind,
		NodeIP:   req.NodeIP,
		Files:    []FileSpec{{Path: req.Path, Dir: req.Dir, Mode: req.Mode, Source: Source{Kind: SourceInline, Data: []byte(req.Raw)}}},
		Install:  InstallSpec{Kind: InstallNone},
		Smoke:    p.Smoke,
		Policy:   p.Policy,
		Version:  p.Version,
		Metadata: req.Metadata,
	}
	if d.Smoke.Kind == "" {
		d.Smoke.Kind = SmokeSHA256
	}
	return []DesiredState{d}, nil
}
