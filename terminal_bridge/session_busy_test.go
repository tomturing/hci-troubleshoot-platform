package main

// 业务活跃保护（lastBusyAt）的行为契约。
//
// 现实约束：HCI 交互式 shell 有 TMOUT=600，10 分钟无输入必然退出；
// 而一次日志采集跑十几分钟且期间零键盘输入。历史实现把 shell EOF 当成
// 整条 SSH 连接失效并 destroySession，于是采集到一半会话被摘表：
// 后续 progress 全部 session_missing，前端干等 60s 报"progress 超时（60s）"
// （2026-10-28 现场）。
//
// 这里钉住三件事：
//  1. 业务动作会打戳，窗口内会话受保护；
//  2. 窗口到期保护自动失效（不会像漏减的计数那样永久保护）；
//  3. 重连覆盖不再腰斩进行中的长任务（摘表但保留连接，作业结束自愈关闭）。

import (
	"testing"
	"time"
)

func newTestSession() *SSHSession {
	return &SSHSession{done: make(chan struct{}), createdAt: time.Now()}
}

// 业务动作必须留下时间戳：它是 shell EOF 时"连接还在被用"的唯一证据。
func TestMarkBusyRecordsTimestamp(t *testing.T) {
	session := newTestSession()
	if session.isBusy() {
		t.Fatal("从未被业务使用过的会话不应处于活跃窗口")
	}
	if age := session.busyAgeSeconds(); age != -1 {
		t.Fatalf("未使用过的会话 busyAgeSeconds 应为 -1（区分没用过与刚用过），实际 %v", age)
	}
	session.markBusy()
	if !session.isBusyWithin(time.Minute) {
		t.Fatal("刚打过戳的会话必须处于活跃窗口内")
	}
	if age := session.busyAgeSeconds(); age < 0 || age > 5 {
		t.Fatalf("刚打戳的 busyAgeSeconds 应在 0~5s，实际 %v", age)
	}
}

// 窗口到期保护必须自动失效：这是选 lease 而非计数器的唯一理由。
func TestBusyWindowExpires(t *testing.T) {
	session := newTestSession()
	session.markBusy()
	// 直接把戳推到 1 小时前，等价于"用户早已离开"。
	session.lastBusyAt.Store(time.Now().Add(-time.Hour).UnixMilli())
	if session.isBusyWithin(defaultSessionBusyGrace) {
		t.Fatal("超过窗口后仍判定为活跃，保护将永久生效（等价于泄漏）")
	}
}

// 采集中会话不得被空闲租约回收：浏览器关闭（refs=0）是长时采集的常态。
func TestReaperKeepsBusySession(t *testing.T) {
	bridge := newBridge()
	key := "Q2026102801381@172.30.99.131"
	session := newTestSession()
	bridge.set(key, session)
	bridge.releaseSession(key, "conn-1")

	session.markBusy()
	if reaped := bridge.reapIdleSessions(time.Nanosecond); reaped != 0 {
		t.Fatalf("业务活跃期内的会话不应被空闲租约回收，实际回收 %d 条", reaped)
	}
	if bridge.get(key) == nil {
		t.Fatal("会话被误回收，后续 progress 将全部 session_missing")
	}

	// 窗口过期后必须恢复回收，否则孤儿会话永久占用远端 shell。
	session.lastBusyAt.Store(time.Now().Add(-time.Hour).UnixMilli())
	if reaped := bridge.reapIdleSessions(time.Nanosecond); reaped != 1 {
		t.Fatalf("业务窗口过期后应恢复常规回收，实际回收 %d 条", reaped)
	}
}

// 重连覆盖不得腰斩进行中的长任务：只摘表，连接留给作业。
func TestDetachSessionKeepsConnectionForLongTask(t *testing.T) {
	bridge := newBridge()
	key := "Q2026102801381@172.30.99.131"
	session := newTestSession()
	bridge.set(key, session)
	session.beginLongTask()

	if !bridge.destroyOrDetachSession(key, sessionReasonReconnect, "c2-2") {
		t.Fatal("重连覆盖应命中已有会话")
	}
	if bridge.get(key) != nil {
		t.Fatal("摘表后路由表里不应再有该键（新会话必须能入表）")
	}
	if session.isClosed() {
		t.Fatal("长任务进行中不得关闭连接：关闭等于把 12.8 GiB 拉取腰斩")
	}

	// 作业结束必须自愈关闭，否则这条连接会永远挂在远端 shell 上。
	session.endLongTask()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if session.isClosed() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("长任务结束后孤儿会话未自行关闭")
}

// 没有长任务时重连覆盖仍是常规销毁：摘表 + 关闭，不留连接。
func TestDestroyOrDetachDestroysIdleSession(t *testing.T) {
	bridge := newBridge()
	key := "Q2026102801381@172.30.99.131"
	session := newTestSession()
	bridge.set(key, session)

	if !bridge.destroyOrDetachSession(key, sessionReasonReconnect, "c2-2") {
		t.Fatal("重连覆盖应命中已有会话")
	}
	if !session.isClosed() {
		t.Fatal("无长任务的会话必须正常销毁（不能退化成只摘表，否则连接泄漏）")
	}
}

// 保护期内必须"不计空闲"：保护解除后要重新用满一个 TTL，而不是按历史空闲时长
// 立刻回收。2026-10-28 现场：终端 10:18:11 断开进入空闲租约，拉取 10:28:34 完成，
// 10:28:48 会话就被 idle_ttl 销毁，交付卡在 uploading 并被判 needs_reauth。
func TestReaperRestartsIdleClockWhileProtected(t *testing.T) {
	bridge := newBridge()
	key := "Q2026102801381@172.30.99.131"
	session := newTestSession()
	bridge.set(key, session)
	bridge.releaseSession(key, "conn-1")

	// 终端早已断开，空闲租约按旧起点算已经过期很久，但长任务仍在跑。
	bridge.mu.Lock()
	bridge.sessionIdleAt[key] = time.Now().Add(-time.Hour)
	bridge.mu.Unlock()
	session.beginLongTask()

	if reaped := bridge.reapIdleSessions(time.Minute); reaped != 0 {
		t.Fatalf("保护期内不应回收，实际回收 %d 条", reaped)
	}
	session.endLongTask()

	// 保护刚解除：若 reaper 没有刷新 idleAt，这里会立刻回收（正是现场事故）。
	if reaped := bridge.reapIdleSessions(time.Minute); reaped != 0 {
		t.Fatalf("保护解除后应按新起点重新计时，实际立刻回收 %d 条", reaped)
	}

	// 确认回收能力没丢：真正过期后仍能回收。
	bridge.mu.Lock()
	bridge.sessionIdleAt[key] = time.Now().Add(-2 * time.Minute)
	bridge.mu.Unlock()
	if reaped := bridge.reapIdleSessions(time.Minute); reaped != 1 {
		t.Fatalf("租约真正过期后应回收，实际回收 %d 条", reaped)
	}
}

// 已拉满的作业在 retrying 时必须归成 uploading：归成 pulling 会让一次纯上传失败
// 的重试去要求 SSH 会话，会话被回收后就成了 needs_reauth 终态，而本地副本完好。
func TestNormalizeRetryingWithFullPullGoesUploading(t *testing.T) {
	job := &deliveryJob{
		Phase:       phaseRetrying,
		SourceSize:  2122094416,
		PulledBytes: 2122094416,
	}
	phase, err := normalizeDeliveryPhase(job)
	if err != nil {
		t.Fatalf("retrying 归一化不应报错: %v", err)
	}
	if phase != phaseUploading {
		t.Fatalf("已拉满的作业必须归成 uploading，实际 %s", phase)
	}
	if deliveryPhaseNeedsNode(phase) {
		t.Fatal("uploading 不需要节点，不应因会话缺失被判 needs_reauth")
	}
}

// 未拉满时必须回 pulling：否则会拿半截包去上传。
func TestNormalizeRetryingWithPartialPullGoesPulling(t *testing.T) {
	job := &deliveryJob{Phase: phaseRetrying, SourceSize: 1000, PulledBytes: 400}
	if phase, _ := normalizeDeliveryPhase(job); phase != phasePulling {
		t.Fatalf("未拉满的作业必须归成 pulling，实际 %s", phase)
	}
}
