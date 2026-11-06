package main

// 停摆判定：把"作业还在干活吗"从墙钟里剥出来。
//
// 第一性原理：一个长任务只有两种结局——"在动（哪怕很慢）"和"死了"。墙钟分不开这两者，
// 它只会同时误杀慢的、纵容死的。唯一既排除死锁又不排斥慢网的可测事实是
// **单调量还在不在增长**（已传分片数、已拉字节）。
//
// 抽出 stallTracker 而不是把逻辑写在 goroutine 里，是为了让"多久算停摆""换阶段要不要
// 重新计时""第一次观察会不会误判"这些边界都能被单测直接钉住——看门狗本身没法在
// 单元测试里等 30 分钟。

import "time"

// stallTracker 用注入的时钟判定停摆，便于测试与复用。
type stallTracker struct {
	timeout time.Duration
	// sig 是上一次观察到的进度签名；since 是该签名首次出现的时刻。
	sig     string
	since   time.Time
	watched bool
	started bool
}

func newStallTracker(timeout time.Duration) *stallTracker {
	return &stallTracker{timeout: timeout}
}

// observe 喂入一次观察，返回是否已判定停摆。
//
// watched=false 表示"当前阶段不该用字节增量判定"（等采集、等平台合并）：
// 此时只重置计时，绝不计入停摆时长——否则一个正在正常等节点采集的作业
// 会被误判成死链。
func (t *stallTracker) observe(at time.Time, signature string, watched bool) bool {
	if !watched || !t.started {
		t.sig = signature
		t.since = at
		t.watched = watched
		t.started = true
		return false
	}
	if signature != t.sig {
		t.sig = signature
		t.since = at
		return false
	}
	return at.Sub(t.since) >= t.timeout
}
