package reconcile

// guard_fanout_test.go —— S3 验收：主机键互斥（B3）与集合性写入的 written-set。
//
// 这两条不变量都属于"看起来能跑、出事才发现"的类型：
// 并发交错要跑几十次才撞上一次，回滚多碰一台不可达节点只在现场才暴露。
// 因此用确定性构造（预先占锁、故意让一台失败）把它们钉成必现用例。

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestGuardAcquireRejectsConcurrentSameHost(t *testing.T) {
	guard := NewGuard("")
	release, err := guard.Acquire("spc_slot@172.28.24.1")
	if err != nil {
		t.Fatalf("首次占锁应成功：%v", err)
	}
	if _, err := guard.Acquire("spc_slot@172.28.24.1"); !errors.Is(err, ErrBusy) {
		t.Fatalf("同主机第二次占锁必须回 ErrBusy（不阻塞），实际 %v", err)
	}
	release()
	release2, err := guard.Acquire("spc_slot@172.28.24.1")
	if err != nil {
		t.Fatalf("释放后应能重新占锁：%v", err)
	}
	release2()
}

// TestGuardConcurrentAcquireExactlyOneWins 竞态用例：50 个执行者同时抢同一主机，
// 必须恰好一个成功。用 map+mutex 而不是 channel 时最容易写出"两个都拿到锁"。
func TestGuardConcurrentAcquireExactlyOneWins(t *testing.T) {
	guard := NewGuard("")
	var wins, busy int32
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			release, err := guard.Acquire("acli_binary@172.28.25.1")
			if err == nil {
				atomic.AddInt32(&wins, 1)
				time.Sleep(5 * time.Millisecond)
				release()
				return
			}
			if errors.Is(err, ErrBusy) {
				atomic.AddInt32(&busy, 1)
			}
		}()
	}
	close(start)
	wg.Wait()
	if wins != 1 {
		t.Fatalf("同一时刻只能有一个执行者拿到主机锁，实际 %d 个", wins)
	}
	if busy != 49 {
		t.Fatalf("其余执行者必须收到 ErrBusy 而不是阻塞或别的错误，实际 %d", busy)
	}
	if guard.Held("acli_binary@172.28.25.1") {
		t.Fatal("全部释放后不应仍持锁")
	}
}

// TestGuardReleaseIsIdempotent 重复释放不能把别人刚拿到的锁放掉。
func TestGuardReleaseIsIdempotent(t *testing.T) {
	guard := NewGuard("")
	release, err := guard.Acquire("host-a")
	if err != nil {
		t.Fatal(err)
	}
	release()
	release() // 幂等：第二次是空操作
	other, err := guard.Acquire("host-a")
	if err != nil {
		t.Fatalf("第二个执行者应拿到锁：%v", err)
	}
	release() // 第一个执行者的陈旧 release 不得影响第二个
	if !guard.Held("host-a") {
		t.Fatal("陈旧 release 把别人正在持有的锁放掉了")
	}
	other()
}

func TestGuardDifferentHostsAndKindsDoNotBlock(t *testing.T) {
	guard := NewGuard("")
	releaseA, err := guard.Acquire(HostKey(KindSpcSlot, "172.28.24.1"))
	if err != nil {
		t.Fatal(err)
	}
	defer releaseA()
	if _, err := guard.Acquire(HostKey(KindSpcSlot, "172.28.25.1")); err != nil {
		t.Fatalf("不同主机不应互斥：%v", err)
	}
	if _, err := guard.Acquire(HostKey(KindAcliBinary, "172.28.24.1")); err != nil {
		t.Fatalf("同主机不同 Kind 动的是不同文件，不应互斥：%v", err)
	}
}

func TestGuardNilAndEmptyKey(t *testing.T) {
	var nilGuard *Guard
	release, err := nilGuard.Acquire("anything")
	if err != nil {
		t.Fatalf("nil Guard 应是空操作（单节点链路不需要互斥）：%v", err)
	}
	release()
	if nilGuard.Held("anything") {
		t.Fatal("nil Guard 不该报告持锁")
	}
	guard := NewGuard("")
	if _, err := guard.Acquire("   "); err == nil {
		t.Fatal("空主机键必须拒绝：无键互斥等于没互斥")
	}
}

func TestGuardLeasePathFencesByExecID(t *testing.T) {
	guard := NewGuard("/var/lib/terminal-bridge/shards")
	first, err := guard.LeasePath("172.28.24.1", "exec-1", "part-0")
	if err != nil {
		t.Fatal(err)
	}
	second, err := guard.LeasePath("172.28.24.1", "exec-2", "part-0")
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("不同代际必须落在不同分片路径，否则并发执行者会互写")
	}
	if _, err := guard.LeasePath("172.28.24.1", "", "part-0"); err == nil {
		t.Fatal("缺代际标识必须 fail-closed，不能退化成共享路径")
	}
	// 含分隔符与 .. 的键不得逃出基线目录。
	escaped, err := guard.LeasePath("../../etc", "a/b", "c")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(escaped, "..") || !strings.HasPrefix(escaped, "/var/lib/terminal-bridge/shards/") {
		t.Fatalf("fencing 路径被穿越：%s", escaped)
	}
	if _, err := NewGuard("").LeasePath("h", "e", "n"); err == nil {
		t.Fatal("未配置基线目录时应拒绝生成 fencing 路径")
	}
}

// fanoutFixture 造 4 台主机，其中第 4 台故意失败（模拟节点环境坏）。
func fanoutFixture(t *testing.T) (Fanout, []string, *sync.Map) {
	t.Helper()
	touched := &sync.Map{}
	fanout := DefaultFanout()
	hosts := []string{"172.28.24.1", "172.28.24.2", "172.28.25.1", "172.28.25.2"}
	return fanout, hosts, touched
}

func TestFanoutWriteAllTracksWrittenSet(t *testing.T) {
	fanout, hosts, touched := fanoutFixture(t)
	results, ws := fanout.WriteAll(context.Background(), hosts, func(ctx context.Context, host string) HostResult {
		touched.Store(host, true)
		if host == "172.28.25.2" {
			return HostResult{Host: host, ErrorCode: "write_failed", ErrorDetail: "read-only file system"}
		}
		return HostResult{Host: host, Written: true}
	})
	if len(results) != 4 {
		t.Fatalf("逐台结果必须齐全：%d", len(results))
	}
	if ws.Len() != 3 {
		t.Fatalf("写入集应只含 3 台成功主机，实际 %d", ws.Len())
	}
	if ws.Has("172.28.25.2") {
		t.Fatal("失败主机不得进入写入集")
	}
	for i, host := range hosts {
		if results[i].Host != host {
			t.Fatalf("结果顺序必须与入参一致：%d %s != %s", i, results[i].Host, host)
		}
	}
}

// TestFanoutRollbackWrittenOnlyTouchesWritten 是 #1546 / Q2026110302166 的回归：
// 3 台写成功、1 台失败，回滚只能操作那 3 台。把不可达节点纳入回滚会制造
// "可能残留平台清单"的假告警，把排障引向完全错误的方向。
func TestFanoutRollbackWrittenOnlyTouchesWritten(t *testing.T) {
	fanout, hosts, _ := fanoutFixture(t)
	_, ws := fanout.WriteAll(context.Background(), hosts, func(ctx context.Context, host string) HostResult {
		if host == "172.28.25.2" {
			return HostResult{Host: host, ErrorCode: "host_unreachable"}
		}
		return HostResult{Host: host, Written: true}
	})
	var rolled []string
	var mu sync.Mutex
	results := fanout.RollbackWritten(context.Background(), ws, func(ctx context.Context, host string) HostResult {
		mu.Lock()
		rolled = append(rolled, host)
		mu.Unlock()
		return HostResult{Host: host, Written: true}
	})
	if len(rolled) != 3 || len(results) != 3 {
		t.Fatalf("回滚只应操作 3 台已写入主机，实际 %v", rolled)
	}
	for _, host := range rolled {
		if host == "172.28.25.2" {
			t.Fatal("从未写入的节点被回滚了")
		}
	}
}

func TestFanoutRollbackWrittenEmptyDoesNothing(t *testing.T) {
	fanout := DefaultFanout()
	called := false
	results := fanout.RollbackWritten(context.Background(), WrittenSet{}, func(ctx context.Context, host string) HostResult {
		called = true
		return HostResult{Host: host}
	})
	if called || results != nil {
		t.Fatal("写入集为空时一条命令都不该发")
	}
}

func TestFanoutRespectsConcurrencyLimit(t *testing.T) {
	fanout := DefaultFanout()
	if fanout.Concurrency != 4 || fanout.HostTimeout != 30*time.Second || !fanout.BatchMode {
		t.Fatalf("默认参数必须与现有 spc 扇出一致：%+v", fanout)
	}
	var current, peak int32
	hosts := make([]string, 0, 12)
	for i := 0; i < 12; i++ {
		hosts = append(hosts, string(rune('a'+i)))
	}
	fanout.WriteAll(context.Background(), hosts, func(ctx context.Context, host string) HostResult {
		now := atomic.AddInt32(&current, 1)
		for {
			old := atomic.LoadInt32(&peak)
			if now <= old || atomic.CompareAndSwapInt32(&peak, old, now) {
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
		atomic.AddInt32(&current, -1)
		return HostResult{Host: host, Written: true}
	})
	if peak > 4 {
		t.Fatalf("并发上限被突破（会打满被采节点 sshd）：peak=%d", peak)
	}
	if peak < 2 {
		t.Fatalf("并发根本没生效：peak=%d", peak)
	}
}

func TestFanoutHostTimeoutCancelsAction(t *testing.T) {
	fanout := Fanout{Concurrency: 2, HostTimeout: 40 * time.Millisecond}
	results, ws := fanout.WriteAll(context.Background(), []string{"slow"}, func(ctx context.Context, host string) HostResult {
		<-ctx.Done() // 动作必须能收到取消，否则"超时了"只是骨架不再等
		return HostResult{Host: host, Written: true}
	})
	if len(results) != 1 || results[0].ErrorCode == "" {
		t.Fatalf("单台超时必须留痕而不是当成成功：%+v", results)
	}
	if ws.Len() != 0 {
		t.Fatal("超时主机不得进入写入集")
	}
}

// TestFanoutNegativeHostTimeoutDisablesDeadline 负值禁用主机级超时（spc 多槽位写入场景）：
// 逐台动作含多条命令，每条的预算已在消费者内部强制，再套一层主机级
// 会把多槽位写入误杀成"单台超时"。零值仍取默认 30s（有 deadline）。
func TestFanoutNegativeHostTimeoutDisablesDeadline(t *testing.T) {
	fanout := Fanout{Concurrency: 1, HostTimeout: -1}
	results, ws := fanout.WriteAll(context.Background(), []string{"172.28.24.1"}, func(ctx context.Context, host string) HostResult {
		if _, ok := ctx.Deadline(); ok {
			return HostResult{ErrorCode: "unexpected_deadline", ErrorDetail: "负值不应套 deadline"}
		}
		time.Sleep(60 * time.Millisecond) // 毫秒级慢动作也必须完整跑完，不被默认超时误杀
		return HostResult{Written: true}
	})
	if len(results) != 1 || !results[0].OK() || ws.Len() != 1 {
		t.Fatalf("负超时下慢动作必须完整跑完：%+v", results)
	}
	// 零值对照：默认 30s 仍生效（动作 ctx 必须带 deadline）。
	zero := Fanout{Concurrency: 1}
	zero.WriteAll(context.Background(), []string{"172.28.24.1"}, func(ctx context.Context, host string) HostResult {
		if _, ok := ctx.Deadline(); !ok {
			t.Error("零值必须落到默认 30s 主机级超时")
		}
		return HostResult{Written: true}
	})
}

func TestFanoutDedupesAndSkipsEmptyHosts(t *testing.T) {
	fanout := DefaultFanout()
	var count int32
	results, ws := fanout.WriteAll(context.Background(),
		[]string{"172.28.24.1", " 172.28.24.1 ", "", "   ", "172.28.25.1"},
		func(ctx context.Context, host string) HostResult {
			atomic.AddInt32(&count, 1)
			return HostResult{Host: host, Written: true}
		})
	if count != 2 || len(results) != 2 || ws.Len() != 2 {
		t.Fatalf("重复主机必须去重、空主机必须跳过（空 IP 会让 ssh 跳到本机）：count=%d", count)
	}
}

// TestFanoutGuardRejectsBusyHost 两个 reconcile 并发操作同一主机：
// 先占锁的那个正常跑，后来的那台立刻拿到 busy，不阻塞、不交错。
func TestFanoutGuardRejectsBusyHost(t *testing.T) {
	guard := NewGuard("")
	release, err := guard.Acquire("172.28.24.1")
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	fanout := DefaultFanout()
	fanout.Guard = guard
	results, ws := fanout.WriteAll(context.Background(), []string{"172.28.24.1", "172.28.25.1"},
		func(ctx context.Context, host string) HostResult {
			return HostResult{Host: host, Written: true}
		})
	if results[0].ErrorCode != CodeBusy {
		t.Fatalf("被占用的主机应回 busy：%+v", results[0])
	}
	if results[1].ErrorCode != "" || !ws.Has("172.28.25.1") {
		t.Fatalf("其余主机不该被牵连：%+v", results[1])
	}
	if ws.Has("172.28.24.1") {
		t.Fatal("busy 主机不得进入写入集")
	}
}

func TestFanoutEachDoesNotTrackWrittenSet(t *testing.T) {
	fanout := DefaultFanout()
	results := fanout.Each(context.Background(), []string{"a", "b"}, func(ctx context.Context, host string) HostResult {
		return HostResult{Host: host, Written: true, Value: host + "-fact"}
	})
	if len(results) != 2 || results[0].Value != "a-fact" {
		t.Fatalf("集合性探测必须保留消费者逐台证据：%+v", results)
	}
	if fanout.Each(context.Background(), nil, nil) != nil {
		t.Fatal("空主机列表应直接返回")
	}
}

func TestSortedHostsIsStableAndDeduped(t *testing.T) {
	got := SortedHosts([]string{"b", "a", "b", "", "c"})
	if strings.Join(got, ",") != "a,b,c" {
		t.Fatalf("排序去重结果不对：%v", got)
	}
}
