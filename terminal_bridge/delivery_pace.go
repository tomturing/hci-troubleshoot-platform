package main

// 分片上传的「节奏」决策：并发数 × 单片墙钟预算。
//
// 第一性原理
// ----------
// 单片能不能传上去，只取决于一个不等式：
//
//	chunkSize / 单路可用速率  <  单片超时
//
// 三个量里 chunkSize 由平台会话固定（8 MiB，续传中途不可改），所以现场能动的只有
// 后两个。而这两个量恰恰是**互相耦合**的：总上行被 N 路并发均分，单路速率 ≈ 总带宽/N。
// 于是在带宽受限的链路上，提高并发会让每一路都更慢——并发不是"更多车道"，
// 而是"把同一根管子切得更细"。2026-11-06 Q2026110600112 就是这个死结：
// 1.94 GB 的包，实测总上行约 34 KB/s，并发 2 路各 19 KB/s，8 MiB 需要 440 s，
// 而单片超时 300 s × 3 次重试 = 每轮 15 分钟必然在同一片上空转，平台侧 19/233 片
// 从此再没动过——两轮结束时刻 13:17:59 与 13:33:02 精确等于 3×300s。
//
// 所以正确的模型不是"挑一个更长的超时"，而是**让节奏随实测速率收敛**：
//   - 有实测吞吐时，按 chunkSize/单路速率 反推超时，并把并发降到"单路速率够用"的最小值；
//   - 连一片都没过去时，先降并发（把管子还给单路），再把超时翻倍。
//
// 单侧收敛（只降不升）是刻意的：健康链路永远不会触发（没有超时证据），
// 而一旦降下来就说明这条链路确实撑不住并发，来回调整只会让进度抖动。
// 收敛结果随 job 落盘，跨重试轮次保持——否则每一轮都从 300 s/并发 2 重新开始，
// 15 分钟一轮的学费要一直交下去。

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"time"
)

const (
	// 单片墙钟预算的上限。封顶而不是无限放大：TCP 黑洞时用户要能等到一个结论，
	// 不能看起来永远在"上传中"。20 分钟 ≈ 8 MiB 在 7 KB/s 下刚好够。
	paceMaxPartTimeout = 20 * time.Minute
	// 超时安全系数：预测需求时间 × 1.6 作为预算，吸收速率抖动。
	paceSafetyFactor = 1.6
)

// paceMinPartTimeout 是单片预算下限，默认等于既有常量（生产行为不变）。
//
// 之所以做成变量而不是常量：弱网收敛的唯一验收方式是把"链路慢到必然超时"
// 真实跑一遍（见 delivery_weak_link_test.go），而 5 分钟的常量会让这种用例
// 跑上十几分钟。测试里临时改成 1 秒，生产路径永远读默认值。
var paceMinPartTimeout = logCollectPartTimeout

// uploadPace 是「这一轮按什么节奏传」的决策结果，随 job 落盘。
type uploadPace struct {
	Parts int `json:"parts"`
	// PartTimeout 用秒存：job 文件是给人 cat 的，纳秒数字读不出含义。
	PartTimeout int64 `json:"part_timeout_sec"`
	// Reason 只用于事件日志与现场解释，不参与任何判定。
	Reason string `json:"reason,omitempty"`
}

// timeout 返回本轮的 http.Client 超时（单片预算）。
func (p uploadPace) timeout() time.Duration {
	if p.PartTimeout <= 0 {
		return paceMinPartTimeout
	}
	return time.Duration(p.PartTimeout) * time.Second
}

// resolve 把 pace 归一化到合法区间。
//
// 必须做这一步，因为 pace 会落盘、会被现场手工 cat 甚至改坏、也会读到没有该字段的
// 旧版本作业文件（零值）。一个 Parts=0 的作业会让 upload 起 0 个 worker 然后死等，
// 比不做自适应更糟。
func (p uploadPace) resolve(ceiling int) uploadPace {
	if ceiling < 1 {
		ceiling = 1
	}
	parts := p.Parts
	if parts < 1 {
		parts = 1
	}
	if parts > ceiling {
		parts = ceiling
	}
	timeout := p.timeout()
	if timeout < paceMinPartTimeout {
		timeout = paceMinPartTimeout
	}
	if timeout > paceMaxPartTimeout {
		timeout = paceMaxPartTimeout
	}
	reason := p.Reason
	if reason == "" {
		reason = "default"
	}
	return uploadPace{Parts: parts, PartTimeout: int64(timeout / time.Second), Reason: reason}
}

// defaultUploadPace 是本轮未拿到任何实测证据时的起点：沿用资源决策的并发，
// 单片超时用既有常量，行为与改造前完全一致（不带 pace 的旧作业也一样）。
func defaultUploadPace(parts int, partTimeout time.Duration) uploadPace {
	if partTimeout <= 0 {
		partTimeout = logCollectPartTimeout
	}
	return uploadPace{
		Parts:       parts,
		PartTimeout: int64(partTimeout / time.Second),
		Reason:      "default",
	}
}

// uploadRoundSample 是一轮上传结束后能观测到的最小事实集。
//
// 只收这些，是因为它们是"物理上可测"的量：传上去多少字节、花了多少墙钟、
// 有几个分片是被超时掐断的。任何需要推测的量（比如"平台带宽配额"）都不能进来，
// 否则自适应会变成猜谜。
type uploadRoundSample struct {
	// UploadedParts / UploadedBytes 本轮真正成功的部分（不含历史已传分片）。
	UploadedParts int
	UploadedBytes int64
	// TimedOutParts 本轮因超时（而非 4xx/5xx）失败的次数。
	TimedOutParts int
	// Elapsed 本轮上传的墙钟总时长（从第一片开始到收口）。
	Elapsed time.Duration
}

// nextUploadPace 由上一轮节奏与本轮样本推导下一轮节奏。
//
// 这是整个修复的核心判据，做成纯函数是为了让下面每条边界都能被单测钉住。
func nextUploadPace(prev uploadPace, sample uploadRoundSample, chunkSize int64, ceiling int) uploadPace {
	prev = prev.resolve(ceiling)
	if chunkSize <= 0 {
		return prev
	}
	if sample.TimedOutParts <= 0 {
		// 没有超时证据：链路是够用的，不动节奏（哪怕很慢——慢不是失败）。
		return prev
	}

	measured := sample.UploadedParts > 0 && sample.UploadedBytes > 0 && sample.Elapsed > 0
	if !measured {
		// 一片都没过去：先降并发。总带宽不变时把 N 路合成 1 路，单路速率直接翻倍，
		// 这是唯一能在"带宽总量未知"时无条件变好的动作。
		next := prev
		if next.Parts > 1 {
			next.Parts /= 2
			if next.Parts < 1 {
				next.Parts = 1
			}
		}
		timeout := next.timeout() * 2
		if timeout > paceMaxPartTimeout {
			timeout = paceMaxPartTimeout
		}
		next.PartTimeout = int64(timeout / time.Second)
		next.Reason = "no_progress_escalation"
		return next
	}

	// 有实测吞吐：rate 是整轮总吞吐（B/s），按并发均分后得到单路速率。
	rate := float64(sample.UploadedBytes) / sample.Elapsed.Seconds()
	if rate <= 0 {
		return prev
	}
	parts := prev.Parts
	budget := prev.timeout().Seconds()
	// 并发降到"单路放得进预算"为止；parts==1 时无论如何都不再降（最小就是单路）。
	for parts > 1 {
		perStream := rate / float64(parts)
		need := float64(chunkSize) / perStream
		if need*paceSafetyFactor <= budget {
			break
		}
		parts /= 2
	}
	perStream := rate / float64(parts)
	need := float64(chunkSize) / perStream
	want := time.Duration(need * paceSafetyFactor * float64(time.Second))
	if want < prev.timeout() {
		// 只升不降：预算已经证明不够（本轮确实有片超时），此时把预算改小等于自欺。
		want = prev.timeout()
	}
	if want > paceMaxPartTimeout {
		want = paceMaxPartTimeout
	}
	if want < paceMinPartTimeout {
		want = paceMinPartTimeout
	}
	return uploadPace{
		Parts:       parts,
		PartTimeout: int64(want / time.Second),
		Reason:      fmt.Sprintf("measured:%.1fKB/s", rate/1024),
	}
}

// isPartTimeoutError 判断一个分片错误是不是"超时"。
//
// 为什么要单独判：只有超时才能推断"链路撑不住当前节奏"，5xx/401 与节奏无关，
// 拿它们去降并发会让平台发版期间无辜地退到单路慢传。
func isPartTimeoutError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) || os.IsTimeout(err) {
		return true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	// http.Client.Timeout 的原文是 "context deadline exceeded (Client.Timeout exceeded
	// while awaiting headers)"，被 transport 包装后不一定仍满足 errors.As(net.Error)。
	//
	// 这里额外认 "context deadline exceeded" 原文：中间层哪怕是字符串拼接（历史实现
	// wrapTransientTransport 就没有 Unwrap），上层也必须能把"超时"认出来——
	// 认不出的代价是每一轮都无证据地原地重试，正是 2026-11-06 弱网活锁的成因。
	text := err.Error()
	return strings.Contains(text, "Client.Timeout exceeded") ||
		strings.Contains(text, "context deadline exceeded") ||
		strings.Contains(text, "i/o timeout") ||
		strings.Contains(text, "operation timed out")
}
