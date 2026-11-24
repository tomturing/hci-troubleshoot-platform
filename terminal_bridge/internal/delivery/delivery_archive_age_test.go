package delivery

// 包归属判定的回归测试。
//
// 守护的不变量：
//  1. 节点时钟静态偏移（慢/快数小时）不参与判定；
//  2. 交付排队导致包“看起来老了”，但只要它是本次作业开始后产出的就放行；
//  3. 真旧包仍被拦截，并带结构化 LC-TRANSFER-011；
//  4. 时钟回拨（mtime 在未来）放行，避免用户侧时间异常把功能堵死；
//  5. 探测失败跳过判定，不把“读不到时间”当成旧包。

import (
	"context"
	"strconv"
	"strings"
	"testing"
	"time"

	"terminal_bridge/internal/shared/logcontract"
)

func TestClassifyArchiveAge(t *testing.T) {
	base := time.Date(2026, 11, 24, 4, 0, 0, 0, time.UTC)
	jobStartedAt := base.Add(-time.Minute)
	cases := []struct {
		name      string
		nodeNow   time.Time
		mtime     time.Time
		bridgeNow time.Time
		jobStart  time.Time
		want      archiveAgeVerdict
	}{
		{"刚打包完成", base, base.Add(-5 * time.Second), base, jobStartedAt, archiveAgeFresh},
		// 现场 Q2026112400585：节点慢 6 分钟，旧实现拿节点 mtime 与 bridge 墙钟比较。
		{"现场慢 6 分钟的新包", base.Add(-6 * time.Minute), base.Add(-6*time.Minute - 5*time.Second), base, jobStartedAt, archiveAgeFresh},
		{"节点慢 6 小时但包是新的", base.Add(-6 * time.Hour), base.Add(-6*time.Hour - time.Minute), base, jobStartedAt, archiveAgeFresh},
		{"节点快 6 小时但包是新的", base.Add(6 * time.Hour), base.Add(6*time.Hour - time.Minute), base, jobStartedAt, archiveAgeFresh},
		// 交付排队 45 分钟：包在作业开始后 5 分钟才产出，不能被“节点内年龄 40 分钟”误杀。
		{"排队 40 分钟的新包", base, base.Add(-40 * time.Minute), base, base.Add(-45 * time.Minute), archiveAgeFresh},
		{"刚好 30 分钟仍放行", base, base.Add(-30 * time.Minute), base, jobStartedAt, archiveAgeFresh},
		{"超过 30 分钟且早于作业", base, base.Add(-31 * time.Minute), base, jobStartedAt, archiveAgeStale},
		{"上一轮旧包", base, base.Add(-2 * time.Hour), base, jobStartedAt, archiveAgeStale},
		{"mtime 在容许范围内的未来", base, base.Add(time.Minute), base, jobStartedAt, archiveAgeFresh},
		{"时钟回拨导致 mtime 在未来", base, base.Add(5 * time.Minute), base, jobStartedAt, archiveAgeClockRollback},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, age := classifyArchiveAge(tc.nodeNow, tc.mtime, tc.bridgeNow, tc.jobStart)
			if got != tc.want {
				t.Fatalf("verdict=%s, want=%s (age=%s)", got, tc.want, age)
			}
		})
	}
}

// fakeNodeSession 只实现本测试用到的 RunQuickCommand；
// 其余方法用嵌入的 nil 接口占位，调用即 panic，能防止测试悄悄走错路径。
type fakeNodeSession struct {
	NodeSession
	quickFn func(context.Context, string) (string, int, error)
}

func (f *fakeNodeSession) RunQuickCommand(ctx context.Context, command string) (string, int, error) {
	return f.quickFn(ctx, command)
}

func TestInspectArchiveAgeUsesSingleNodeClock(t *testing.T) {
	path := "/sf/data/local/acli/tmp/data/log_collect/Q-test.zip"
	nodeNow := int64(1795492550)
	mtime := nodeNow - 300 // 节点本地 5 分钟前
	var gotCommand string
	session := &fakeNodeSession{quickFn: func(_ context.Context, command string) (string, int, error) {
		gotCommand = command
		return "1795492550 1795492250\n", 0, nil
	}}

	sample, err := inspectArchiveAge(context.Background(), session, path)
	if err != nil {
		t.Fatalf("探测失败: %v", err)
	}
	if sample.NodeNow.Unix() != nodeNow || sample.MTime.Unix() != mtime {
		t.Fatalf("时间解析错误: now=%d mtime=%d", sample.NodeNow.Unix(), sample.MTime.Unix())
	}
	for _, fragment := range []string{"date +%s", "stat -c %Y", path} {
		if !strings.Contains(gotCommand, fragment) {
			t.Fatalf("命令必须同一次取节点当前时间与 mtime，缺少 %q: %s", fragment, gotCommand)
		}
	}
}

func TestEnsureArchiveNotStaleAcceptsFreshPackage(t *testing.T) {
	bridgeNow := time.Now()
	nodeNow := bridgeNow.Add(-6 * time.Minute).Unix() // 节点慢 6 分钟
	session := &fakeNodeSession{quickFn: func(context.Context, string) (string, int, error) {
		return formatTwoEpochs(nodeNow, nodeNow-5), 0, nil
	}}
	job := newArchiveAgeTestJob("dj-fresh", bridgeNow.Add(-time.Minute))
	if err := ensureArchiveNotStale(context.Background(), session, job, "/tmp/Q.zip"); err != nil {
		t.Fatalf("新包不应被拦: %v", err)
	}
}

func TestEnsureArchiveNotStaleAcceptsQueuedFreshPackage(t *testing.T) {
	bridgeNow := time.Now()
	nodeNow := bridgeNow.Unix()
	session := &fakeNodeSession{quickFn: func(context.Context, string) (string, int, error) {
		// 包在作业开始后产出，但排了 40 分钟才被检查。
		return formatTwoEpochs(nodeNow, nodeNow-int64((40*time.Minute)/time.Second)), 0, nil
	}}
	job := newArchiveAgeTestJob("dj-queued", bridgeNow.Add(-45*time.Minute))
	if err := ensureArchiveNotStale(context.Background(), session, job, "/tmp/Q.zip"); err != nil {
		t.Fatalf("排队不应把本轮新包误判为旧包: %v", err)
	}
}

func TestEnsureArchiveNotStaleRejectsOldPackage(t *testing.T) {
	bridgeNow := time.Now()
	nodeNow := bridgeNow.Unix()
	session := &fakeNodeSession{quickFn: func(context.Context, string) (string, int, error) {
		return formatTwoEpochs(nodeNow, nodeNow-int64((2*time.Hour)/time.Second)), 0, nil
	}}
	job := newArchiveAgeTestJob("dj-old", bridgeNow.Add(-time.Minute))
	err := ensureArchiveNotStale(context.Background(), session, job, "/tmp/Q.zip")
	if err == nil {
		t.Fatal("超过时效的包必须被拦")
	}
	if !isDeliveryFatalError(err) {
		t.Fatalf("旧包必须 fatal，避免 5 次无意义重试: %T", err)
	}
	if code := deliveryErrorCode(err); code != logcontract.ErrTransferSourceStale {
		t.Fatalf("错误码=%q, want=%q", code, logcontract.ErrTransferSourceStale)
	}
	message := err.Error()
	for _, fragment := range []string{"2 小时", "节点本地时间", "校正节点时钟偏差"} {
		if !strings.Contains(message, fragment) {
			t.Fatalf("文案必须给出节点本地时间、校正说明与实测年龄，缺少 %q: %s", fragment, message)
		}
	}
	if strings.Contains(message, "本次采集开始") {
		t.Fatalf("不得再把 bridge 时间写成“本次采集开始”: %s", message)
	}
}

func TestEnsureArchiveNotStaleSkipsProbeFailure(t *testing.T) {
	session := &fakeNodeSession{quickFn: func(context.Context, string) (string, int, error) {
		return "", 1, context.DeadlineExceeded
	}}
	job := newArchiveAgeTestJob("dj-probe", time.Now().Add(-time.Minute))
	if err := ensureArchiveNotStale(context.Background(), session, job, "/tmp/Q.zip"); err != nil {
		t.Fatalf("探测失败应跳过判定而不是误杀: %v", err)
	}
}

func TestEnsureArchiveNotStaleSkipsMissingJobStart(t *testing.T) {
	session := &fakeNodeSession{quickFn: func(context.Context, string) (string, int, error) {
		return formatTwoEpochs(time.Now().Unix(), time.Now().Add(-2*time.Hour).Unix()), 0, nil
	}}
	if err := ensureArchiveNotStale(context.Background(), session, &deliveryJob{JobID: "dj-no-start"}, "/tmp/Q.zip"); err != nil {
		t.Fatalf("作业缺少创建时间时应跳过判定而不是误杀: %v", err)
	}
}

func newArchiveAgeTestJob(id string, startedAt time.Time) *deliveryJob {
	return &deliveryJob{JobID: id, CreatedAt: startedAt.UTC().Format(time.RFC3339Nano)}
}

func formatTwoEpochs(nodeNow, mtime int64) string {
	return strconv.FormatInt(nodeNow, 10) + " " + strconv.FormatInt(mtime, 10) + "\n"
}
