package delivery

// 节点命令健康度：把"会话还活着吗"从"有没有字节在动"里剥出来。
//
// 第一性原理
// ----------
// 等待采集阶段只有两种事实可测：节点命令能不能执行、采集进度有没有推进。
// 采集进度可以合法地长时间不动（多节点串行采集，一台跑一小时很正常），
// 所以它不能用来判"作业死了"；而"命令一条都执行不了"只有一种解释——链路已断。
// 因此判据取**命令连续失败次数**，不取字节增量。
//
// 对抗性审查
// ----------
//  1. 单次失败可能是节点正忙/瞬时抖动 → 累计到阈值才算，阈值取 3；
//  2. 成功一次就清零 → 偶发抖动不会累积成"假死"；
//  3. 用户取消/看门狗掐断也会让命令返回错误 → 调用方必须先判 ctx，
//     否则取消会被记成"会话不可用"并触发无意义的重排。
//
// 抽成独立类型是为了让"多少次算死""成功后是否清零"能被单测直接钉住——
// 现场形态是 149 分钟零字节，没有单测就只能靠等 2 小时来验证。

// commandFailTracker 统计节点命令的连续失败次数。
type commandFailTracker struct {
	limit  int
	streak int
}

// newCommandFailTracker 构造跟踪器；limit<=0 时退回默认阈值。
func newCommandFailTracker(limit int) *commandFailTracker {
	if limit <= 0 {
		limit = deliveryNodeCommandFailureLimit
	}
	return &commandFailTracker{limit: limit}
}

// fail 记录一次失败，返回当前连续失败次数与是否已判定会话不可用。
func (t *commandFailTracker) fail() (int, bool) {
	if t == nil {
		return 0, false
	}
	t.streak++
	return t.streak, t.streak >= t.limit
}

// reset 在任意一次成功观测后清零：只有"连续"失败才说明链路断了。
func (t *commandFailTracker) reset() {
	if t == nil {
		return
	}
	t.streak = 0
}

// streakNow 返回当前连续失败次数（用于日志/事件取证）。
func (t *commandFailTracker) streakNow() int {
	if t == nil {
		return 0
	}
	return t.streak
}
