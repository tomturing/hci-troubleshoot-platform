package main

import (
	"errors"
	"regexp"
	"strings"
)

// ErrFixtureNotFound 表示没有命中任何 fixture，必须 fail closed（方案 6.3）。
var ErrFixtureNotFound = errors.New("fixture_not_found")

// LookupContext 是复合路由键（方案 5.2）。
// 至少使用 test_run_id + scenario_id + node_ip + acquisition_key 之一。
type LookupContext struct {
	ScenarioID     string
	TestRunID      string
	NodeIP         string
	AcquisitionKey string
}

// FixtureRouter 根据 LookupContext + CommandFingerprint 选择 fixture。
type FixtureRouter struct {
	fixtures []*Fixture
}

func NewFixtureRouter(fixtures []*Fixture) *FixtureRouter {
	return &FixtureRouter{fixtures: fixtures}
}

// Resolve 返回命中分数最高的 fixture；未命中返回 ErrFixtureNotFound。
// fail closed：绝不能返回空 stdout + exit 0 的"假成功"。
func (r *FixtureRouter) Resolve(ctx LookupContext, fp CommandFingerprint) (*Fixture, error) {
	var best *Fixture
	bestScore := 0
	for _, f := range r.fixtures {
		score := matchScore(ctx, fp, f)
		if score > bestScore {
			bestScore = score
			best = f
		}
	}
	if best == nil || bestScore <= 0 {
		return nil, ErrFixtureNotFound
	}
	return best, nil
}

// matchScore 对单个 fixture 打分。返回 0 表示不匹配（含强制场景隔离失败）。
func matchScore(ctx LookupContext, fp CommandFingerprint, f *Fixture) int {
	// 强制场景隔离：绑定了 scenario_id 的 fixture 仅当请求显式选择同一 scenario 才可命中；
	// 否则（请求无 scenario，或选了别的 scenario）该 fixture 不可命中，避免跨场景串线。
	if f.ScenarioID != "" && f.ScenarioID != ctx.ScenarioID {
		return 0
	}

	score := 0

	// acquisition / tool
	ak := fp.AcquisitionKey()
	switch {
	case f.AcquisitionKey != "" && ak != "" && f.AcquisitionKey == ak:
		score += 2
	case f.Tool != "" && fp.Tool != "" && f.Tool == "qfk_"+fp.Tool:
		score += 2
	case f.AcquisitionKey == "" && f.Tool == "":
		// 未限定工具，不加分也不扣分
	default:
		return 0 // 工具不匹配
	}

	// 精确场景命中加成：场景专属 fixture 优先于默认（未绑定 scenario）fixture
	if f.ScenarioID != "" && f.ScenarioID == ctx.ScenarioID {
		score += 5
	}

	// command_match 正则优先（匹配命令原始字符串）
	if f.CommandMatch != "" {
		if matched, _ := regexp.MatchString(f.CommandMatch, fp.Raw); !matched {
			return 0
		}
		score += 3
	}

	// 资源关键词软匹配
	if f.ResourceKeyword != "" && fp.ResourceKeyword != "" {
		if strings.Contains(fp.ResourceKeyword, f.ResourceKeyword) || strings.Contains(f.ResourceKeyword, fp.ResourceKeyword) {
			score += 1
		}
	}

	// host
	if f.TargetHost != "" && fp.Host != "" {
		if f.TargetHost != fp.Host {
			return 0
		}
		score += 1
	}

	// container
	if f.Container != "" && fp.Container != "" {
		if f.Container != fp.Container {
			return 0
		}
		score += 1
	}

	return score
}
