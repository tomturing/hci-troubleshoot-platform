package delivery

// 交付作业执行器：进程内唯一的串行调度者。
//
// 为什么必须"唯一"：交付是 20GB 级的长任务，两路并发会在客户机上互相拖到双双超时
// （2026-10-27 现场内存 95%、CPU 83%）。跨任务并发默认 1，只有高配机才允许 2；
// 任务内的分片并发由资源探测决定，见 delivery_concurrency.go。
//
// 与浏览器彻底解耦：submit 之后所有阶段都在这里推进，页面关闭不影响；
// 前端只通过 job_status 轮询进度，重连后继续轮询即可。

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/shared/evidence"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/remote"
	"terminal_bridge/internal/shared/uplink"
	"terminal_bridge/internal/sshx/session"
	"time"
)

const (
	// 等待 acli 采集完成的上限：单次采集超 4 小时基本可以判死，避免 job 永久占位。
	deliveryCollectTimeout = 4 * time.Hour
	// 单个 job 的拉取+上传整体预算：20GiB @ 100Mbps 约 27 分钟，给足重试余量。
	//
	// 从 6 小时抬到 48 小时，是因为 6 小时在弱网上小到没有意义：1.94 GiB 的包在
	// 实测 34 KB/s 的上行下需要约 16 小时（Q2026110600112），作业会在传完之前
	// 被墙砍断，而每一轮都已经"有进展"——这不是占位，是真在传。
	// 真正防占位的判据换成了 deliveryStallTimeout（无进展即终止），
	// 这个绝对上限只作为最后一道保险，不需要贴着业务时长设。
	deliveryJobMaxTimeout = 48 * time.Hour
	// 停摆判定：拉取/上传阶段连续这么久没有任何字节或分片增量，就认定链路已死。
	//
	// 阈值取 30 分钟 = 单片预算上限（20 分钟）+ 10 分钟余量：只要单片还能在预算内
	// 完成，就一定会刷出一次进度；刷不出来说明连最保守的节奏也走不动了，
	// 再等只是让用户盯着"上传中"看一晚上。等采集与平台合并这两个阶段不参与判定
	// （它们各自有 deliveryCollectTimeout / deliveryCompleteTimeout）。
	deliveryStallTimeout = 30 * time.Minute
	// 停摆巡检间隔：判定粒度，取 30 秒足够细，且不产生任何 I/O。
	deliveryStallPollInterval = 30 * time.Second
	// 每传多少片落一次盘：太频繁会把磁盘打满（5120 片 × 写盘），太稀疏会丢进度。
	deliverySaveEveryParts = 10
	// 节点与客户机的时钟偏差容忍。判断"包是否属于本次采集"靠 mtime 与任务开始时间比较，
	// 两边时钟不可能严格对齐；5 分钟既足以吸收常见 NTP 漂移，
	// 又远小于一次真实采集的时长（十几分钟起），不会放过串味的旧包。
	deliveryClockSkewTolerance = 5 * time.Minute
	// 平台收尾（合并）的超时。合并 12.8 GiB 要在服务端读一遍、写一遍、算一遍
	// SHA-256，慢盘上远超分片的 5 分钟；原来复用分片超时会让桥先断开，
	// 服务端随后才写完——桥侧只看到 ClientDisconnect，分片全白传。
	deliveryCompleteTimeout = 30 * time.Minute
	// 非凭据类失败的最大尝试次数（含首次）。超过即终态失败，等人介入。
	// 抽成常量是因为它同时出现在"判定"和"重排"两处，写死两遍必然漂移。
	maxDeliveryAttempts = 5
	// 平台"暂时不可用"（5xx / 连接失败）时的最大尝试次数。
	//
	// 为什么要单独一个更大的预算：一次 K8s 滚动更新或 ArgoCD 同步的空窗通常
	// 30~90 秒，慢的时候更久；而普通预算只有 5 次 × 1..16 秒（累计约 31 秒），
	// 比空窗还短。2026-10-27 现场就是这样：5 次收尾请求全部落在
	// 18:29:20–18:29:46，而新 Pod 18:29:21 才创建、18:29:41 才启动完成，
	// 于是"平台发版"把用户的一次上传整成了终态失败。
	maxDeliveryTransientAttempts = 8
	// DNS「域名不存在」（NXDOMAIN / no such host）的尝试次数（含首次）。
	//
	// 为什么必须比 5xx 的 8 次小得多：8 次 × 最长 60s 退避约 5 分钟，
	// 而 hosts/DNS/代理配错不会在 5 分钟内自愈——2026-11-09 Q2026110900185
	// 就是让用户对着「平台暂时不可用」白等了 8 轮，真实原因却是本机解析不了域名。
	// 但也不能一次都不重试：VPN/代理切换存在秒级空窗，给 2 次机会足够区分
	// 「抖动」与「配错」。
	maxDeliveryDNSAttempts = 3
)

// NodeSession 是 delivery 需要的节点能力：remote 执行原语 + 会话生命周期保护。
//
// 为什么不再直接用 NodeSession：端口化的价值在于编排逻辑可以离线验证
// （Fake 实现即可），而 shared/remote 早就用 fakeRunner 做到了这一点，
// delivery 却一直持具体类型、只能靠集成测试。这里把执行与生命周期两组能力
// 收成一个端口，具体类型只在装配层出现。
type NodeSession interface {
	remote.Runner
	remote.StreamsRunner
	remote.StreamRunner
	BeginLongTask()
	EndLongTask()
	StartShellHeartbeat(caseID string) func()
	MarkBusy()
	// RunCommandPty 在独立通道上分配伪终端执行（acli 命令需要 tty）。
	RunCommandPty(ctx context.Context, command string, timeout time.Duration) (string, int, error)
	// Alive 报告会话是否仍可执行命令；交付拒绝把命令发到已关闭的僵尸会话。
	Alive() bool
}

// Reconnector 是 delivery 需要的装配层能力：
//   - Wrap：把注册表里的会话适配成节点端口（只有装配层知道具体类型）；
//   - ReconnectDeliveryNode：会话掉线后用已保存的认证在后台重建。
//
// 为什么用端口而不是直接收 *app.Bridge：域反向依赖装配层是阶段 2 要清的债。
type Reconnector interface {
	Wrap(key string) NodeSession
	ReconnectDeliveryNode(caseID, nodeIP, connID string) (NodeSession, error)
}

// deliveryRunner 是进程级单例。
type deliveryRunner struct {
	hub         *session.Hub
	reconnect   Reconnector
	store       *deliveryStore
	concurrency deliveryConcurrency

	mu       sync.Mutex
	jobs     map[string]*deliveryJob
	cancels  map[string]context.CancelFunc
	queue    chan *deliveryJob
	started  bool
	inflight int
}

var DeliveryRunnerInstance *deliveryRunner

// NewDeliveryRunner 构造执行器；store 目录来自日志目录（与 job 文件同级，便于现场打包取证）。
func NewDeliveryRunner(hub *session.Hub, r Reconnector) *deliveryRunner {
	runner := &deliveryRunner{
		hub:         hub,
		reconnect:   r,
		store:       newDeliveryStore(evidence.LogDir()),
		concurrency: decideDeliveryConcurrency(),
		jobs:        make(map[string]*deliveryJob),
		cancels:     make(map[string]context.CancelFunc),
		queue:       make(chan *deliveryJob, 16),
	}
	return runner
}

// start 启动调度器并恢复未完成的 job。
//
// 恢复策略：平台任务状态优先（已完成直接归档），其余按落盘阶段继续；
// 读不出来的 job 文件跳过并留痕，绝不阻塞其它 job。
func (r *deliveryRunner) Start() {
	r.mu.Lock()
	if r.started {
		r.mu.Unlock()
		return
	}
	r.started = true
	r.mu.Unlock()

	evidence.Emit("INFO", "delivery.runner_started", "交付执行器已启动", "", "", "", "",
		describeDeliveryConcurrency(r.concurrency))

	go r.schedule()

	recovered := r.store.list()
	for _, job := range recovered {
		r.mu.Lock()
		r.jobs[job.JobID] = job
		r.mu.Unlock()
		evidence.Emit("INFO", "delivery.recovered", "恢复未完成的交付作业", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "task_id": job.TaskID, "phase": string(job.Phase),
			"pulled_bytes": job.PulledBytes, "uploaded_parts": len(job.UploadedParts),
			"needs_reauth": job.NeedsReauth,
		})
		// needs_reauth 的 job 不自动入队：没有凭据跑起来只会立刻失败，等前端重连后再唤醒。
		if !job.NeedsReauth {
			r.enqueue(job)
		}
	}
	// 终态 job 文件保留 7 天供取证，之后清理。
	if removed := r.store.purgeOlderThan(7 * 24 * time.Hour); removed > 0 {
		evidence.Emit("INFO", "delivery.purged", "清理过期交付作业记录", "", "", "", "", map[string]any{"removed": removed})
	}
	// 本地中转副本的回收与 job 记录回收是两条独立生命周期：
	// 记录留 7 天是为了取证，副本留 3 天是为了给重试机会——GB 级副本不能跟着记录一起攒。
	r.startLocalCopyCleanup()
}

// requeueEligible 判断作业是否仍有资格重排（fencing + membership）。
//
// 新会话 submit 时会把旧作业从 r.jobs 删除并放入新指针；指针不相等说明
// "这已经不是当前作业"，即使它还停在 retrying 也不得重新入队。
// 少了这两个判据，旧作业会在 ctx 取消后靠退避重排复活，并重建一个全新的
// context 继续拉包/上传（2026-11-06 Q2026110601054）。
func (r *deliveryRunner) requeueEligible(job *deliveryJob) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return job.Phase == phaseRetrying && !job.NeedsReauth &&
		!job.Superseded && r.jobs[job.JobID] == job
}

// errDeliverySuperseded 表示"本作业已不是该任务的合法交付执行者"。
//
// 它必须是独立哨兵而不是 context.Canceled：取消是**通知**（可能没送到、可能
// 被别的 cancel 掩盖），而顶替是**资格丧失**，两者的正确处理都是落终态，
// 但现场需要能分清"用户取消/看门狗掐断"和"被新会话顶替"。
var errDeliverySuperseded = errors.New("superseded_by_new_session")

// ownerIntact 判定作业此刻是否仍占据该任务的执行位（fencing 的唯一判据）。
//
// 第一性原理：一个作业能不能产生对外副作用，与它"收没收到取消信号"无关，
// 只与它"还是不是当前执行者"有关。后者是三个可观测事实的合取：
//  1. 没被新会话顶替（Superseded）；
//  2. r.jobs 里占着同一个键的位置仍是指向自己——submit 顶替时会在锁内换掉指针；
//  3. 键上没有任何作业（独立构造的作业/单测直调），视为未受管，保持放行。
//
// 第 3 条是刻意保留的宽松分支：把"未注册"当失败会让所有直接调用
// upload/complete 的既有路径（含单测）无法工作，而它们本来就不存在竞争。
func (r *deliveryRunner) ownerIntact(job *deliveryJob) bool {
	if job == nil {
		return false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if job.Superseded {
		return false
	}
	return r.jobs[job.JobID] == nil || r.jobs[job.JobID] == job
}

// fence 是每个对外副作用之前的必过关口：返回非 nil 即不得继续。
//
// 调用点必须在不持有 r.mu 的位置调用（ownerIntact 内部会取锁）。
func (r *deliveryRunner) fence(ctx context.Context, job *deliveryJob) error {
	if !r.ownerIntact(job) {
		return errDeliverySuperseded
	}
	if ctx != nil {
		return ctx.Err()
	}
	return nil
}

// saveJob 是落盘的统一入口：只有"仍是当前执行者"的作业才能写 journal。
//
// 为什么必须拦：journal 文件名就是 task_id（dj-<task>），旧作业收尾时若无条件
// save，会把新作业刚写好的记录**覆盖回旧会话的字节数/阶段**——平台上明明是
// 19/233，本地文件却是 0/233，下一轮续传又从第一片开始。
func (r *deliveryRunner) saveJob(job *deliveryJob) error {
	// 这里不打日志：拉取进度回调会高频调用，僵尸作业被跳过时逐次打点会变成日志洪泛。
	// "被顶替"本身已经由 submit 的 delivery.superseded 事件留痕。
	if !r.ownerIntact(job) {
		return nil
	}
	return r.store.save(job)
}

// saveJobLocked 是同语义的变体，要求调用方已持有 r.mu。
func (r *deliveryRunner) saveJobLocked(job *deliveryJob) error {
	if job == nil || job.Superseded || (r.jobs[job.JobID] != nil && r.jobs[job.JobID] != job) {
		return nil
	}
	return r.store.save(job)
}

// enqueue 非阻塞入队；队列满时保持 job 在 store 里等下一轮启动，不丢。
func (r *deliveryRunner) enqueue(job *deliveryJob) {
	select {
	case r.queue <- job:
	default:
		evidence.Emit("WARN", "delivery.queue_full", "交付队列已满，作业留待下一轮", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "phase": string(job.Phase),
		})
	}
}

// schedule 按「跨任务并发」限制串行消费队列。
func (r *deliveryRunner) schedule() {
	semaphore := make(chan struct{}, r.concurrency.Tasks)
	for job := range r.queue {
		semaphore <- struct{}{}
		go func(job *deliveryJob) {
			defer func() { <-semaphore }()
			r.runJob(job)
		}(job)
	}
}

// submit 接收前端下发的交付请求：落盘 + 入队，立即返回 job_id（不再让前端等 2 小时）。
func (r *deliveryRunner) submit(job *deliveryJob) *deliveryJob {
	if job.JobID == "" {
		job.JobID = "dj-" + job.TaskID
	}
	job.CreatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	job.Phase = phaseWaitingCollect
	var stale *deliveryJob
	var staleCancel context.CancelFunc
	r.mu.Lock()
	if existing, ok := r.jobs[job.JobID]; ok && !existing.Phase.terminal() {
		// 同一个平台会话的重放（前端刷新/重复点击）直接复用，避免重复占位。
		if sameDeliverySession(existing, job) {
			r.mu.Unlock()
			return existing
		}
		// 会话参数变了：平台已签发新令牌 / 新 total_size / 新分片方案，
		// 旧作业的 SubmittedSize 与 PartCount 已经作废。若继续复用，
		// 新会话会按上一轮的基线校验，每一片都必然失败且重试不会自愈。
		existing.Phase = phaseFailed
		existing.LastError = "superseded_by_new_session"
		existing.Superseded = true
		stale, staleCancel = existing, r.cancels[job.JobID]
		delete(r.cancels, job.JobID)
		delete(r.jobs, job.JobID)
	}
	r.jobs[job.JobID] = job
	r.mu.Unlock()

	if stale != nil {
		// 旧作业的 goroutine 必须在锁外终止。
		//
		// 这里**不改 stale.JobID**：JobID 既是 r.cancels 的键，也是 ownerIntact
		// 判据里的身份。在锁外改写它会与旧 goroutine 的读写形成 data race，还会让
		// runJob 收尾时按新键 delete，把取消函数留在 map 里成孤儿。
		// "独立的落盘文件名"只改副本（见下），共享结构从此刻起只读。
		oldID := stale.JobID
		if staleCancel != nil {
			staleCancel()
		}
		// 浅拷贝足够：save 只 marshal，不追加 UploadedParts。
		snapshot := *stale
		snapshot.JobID = oldID + "-superseded"
		_ = r.store.save(&snapshot)
		evidence.Emit("WARN", "delivery.superseded", "同一任务的新上传会话覆盖了旧作业", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "old_job_id": oldID,
			"old_submitted_size": stale.SubmittedSize, "new_submitted_size": job.SubmittedSize,
			"old_part_count": stale.PartCount, "new_part_count": job.PartCount,
		})
	}

	if err := r.store.save(job); err != nil {
		evidence.Emit("WARN", "delivery.save_failed", "交付作业落盘失败（本次仅内存）", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "error": err.Error(),
		})
	}
	evidence.Emit("INFO", "delivery.submitted", "交付作业已提交", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "task_id": job.TaskID, "source_path": job.SourcePath,
		"source_size": job.SourceSize, "part_count": job.PartCount, "chunk_size": job.ChunkSize,
	})
	r.enqueue(job)
	return job
}

// sameDeliverySession 判断两次提交是否指向同一个平台上传会话。
//
// 不能只看 task_id：同一任务失败重试时，平台会签发新令牌、新 total_size
// 和新分片方案，而 bridge 里的旧作业可能仍处于 retrying（非终态）。
// 复用旧作业会让 SubmittedSize/PartCount 停在上一轮，新会话每片都校验不过。
func sameDeliverySession(a, b *deliveryJob) bool {
	return a.UploadToken == b.UploadToken &&
		a.SubmittedSize == b.SubmittedSize &&
		a.PartCount == b.PartCount &&
		a.ChunkSize == b.ChunkSize &&
		a.Direct == b.Direct
}

// status 返回 job 快照（深拷贝，避免调用方读到并发修改中的结构）。
func (r *deliveryRunner) status(jobID string) *deliveryJob {
	r.mu.Lock()
	job, ok := r.jobs[jobID]
	r.mu.Unlock()
	if !ok {
		// 内存里没有就回查磁盘：进程重启后前端仍会按 job_id 轮询。
		job = r.store.load(jobID)
		if job == nil {
			return nil
		}
	}
	snapshot := *job
	snapshot.UploadedParts = append([]int(nil), job.UploadedParts...)
	return &snapshot
}

// resume 在用户重连后唤醒 needs_reauth 的 job。
func (r *deliveryRunner) resume(jobID string) bool {
	r.mu.Lock()
	job, ok := r.jobs[jobID]
	if !ok {
		r.mu.Unlock()
		return false
	}
	job.NeedsReauth = false
	job.LastError = ""
	// 唤醒时必须把阶段从终态里拉出来：needs_reauth 时 phase 是 failed（终态），
	// 只清标志位会让 advance 立刻以"终态拒绝推进"再次失败——用户点"继续"等于没点。
	// 归一化成 retrying，由 advance 按已拉字节/已传分片决定从哪续。
	job.Phase = phaseRetrying
	r.mu.Unlock()
	_ = r.saveJob(job)
	r.enqueue(job)
	return true
}

// cancel 取消正在跑的 job（用户显式取消，或平台任务已被取消）。
func (r *deliveryRunner) cancel(jobID string) bool {
	r.mu.Lock()
	cancel, running := r.cancels[jobID]
	job := r.jobs[jobID]
	r.mu.Unlock()
	if running {
		cancel()
	}
	if job == nil {
		return false
	}
	job.Phase = phaseFailed
	job.LastError = "cancelled_by_user"
	_ = r.saveJob(job)
	return true
}

// runJob 推进单个 job 到终态。
func (r *deliveryRunner) runJob(job *deliveryJob) {
	r.mu.Lock()
	abandoned := job.Superseded
	r.mu.Unlock()
	if abandoned {
		evidence.Emit("INFO", "delivery.superseded_skipped", "被顶替的作业不再推进任何阶段", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "task_id": job.TaskID,
		})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), deliveryJobMaxTimeout)
	// "检查是否已被顶替"与"注册取消函数"必须在同一把锁内完成。
	//
	// 分开做会留下一个窗口：读到 abandoned=false 之后、注册 cancels 之前被 submit
	// 顶替，此时 submit 从 cancels 里取不到 cancel（还没注册），旧作业于是永远收不到
	// 取消信号，会一路拉包、传分片、甚至收尾——这就是"僵尸作业重写已存在数据"。
	r.mu.Lock()
	if job.Superseded || (r.jobs[job.JobID] != nil && r.jobs[job.JobID] != job) {
		r.mu.Unlock()
		cancel()
		evidence.Emit("INFO", "delivery.superseded_skipped", "注册时已被顶替，作业不再推进任何阶段", "", job.CaseID, job.NodeIP, "", map[string]any{
			"job_id": job.JobID, "task_id": job.TaskID, "stage": "register",
		})
		return
	}
	r.cancels[job.JobID] = cancel
	partsBefore := len(job.UploadedParts)
	pulledBefore := job.PulledBytes
	r.mu.Unlock()
	// 停摆看门狗与绝对上限并存：绝对上限只防"永久占位"，是否还在干活由字节增量判定。
	// stalled 标志用于把"看门狗掐断"与"用户取消/superseded"区分开：三者都表现为
	// context.Canceled，但前者的正确处理是重排下一轮，后两者必须落终态。
	var stalled atomic.Bool
	watchDone := make(chan struct{})
	go r.watchStall(ctx, cancel, job, watchDone, func() { stalled.Store(true) })
	// 证据数据面：每 2 秒上报一次阶段速率/ETA。无票据（旧前端）时直接空转退出，
	// 不产生任何 HTTP 请求，也不影响交付状态机。
	go r.streamEvidenceMetrics(ctx, job)
	defer func() {
		close(watchDone)
		cancel()
		r.mu.Lock()
		delete(r.cancels, job.JobID)
		r.mu.Unlock()
	}()

	err := r.advance(ctx, job)
	// 预算按错误性质分流：平台暂时不可用（5xx/连接失败）值得等，
	// 业务性失败（分片长度不符、本地缺文件）等再久也不会变好。
	budget := deliveryAttemptBudget(err)
	r.mu.Lock()
	if err != nil {
		// 本轮只要真的推进过分片或字节，就不该消耗重试预算。
		//
		// 第一性原理：attempt 预算要防的是"无意义重试"，而"每轮都多传上去几片"
		// 有确定的意义——只是慢。按旧逻辑（transient 8 次）计算，弱网下每轮 15 分钟、
		// 8 轮约 2 小时就把一个仍在稳定推进的 20 GiB 交付判死，平台侧只剩 19/233 片
		// 和一个再也不会有人接的 failed。进度型重试的收敛由停摆看门狗负责。
		progressed := len(job.UploadedParts) > partsBefore || job.PulledBytes > pulledBefore
		if progressed {
			job.Attempt = 0
			evidence.Emit("INFO", "delivery.progress_renewed", "本轮有新增进度，重试预算复位", "", job.CaseID, job.NodeIP, "", map[string]any{
				"job_id": job.JobID, "task_id": job.TaskID,
				"uploaded_parts_before": partsBefore, "uploaded_parts_after": len(job.UploadedParts),
				"pulled_bytes_before": pulledBefore, "pulled_bytes_after": job.PulledBytes,
			})
		} else {
			job.Attempt++
		}
		job.LastError = err.Error()
		// 结构化错误码优先于文案匹配：文案会改，码是平台落库、前端分支与统计的锚点。
		// 传空表示本轮错误没有专属码，同时清掉上一轮可能残留的旧码。
		job.LastErrorCode = deliveryErrorCode(err)
		// 停摆优先于"取消"判定：看门狗也是靠 cancel 收工的，但它的语义是
		// "这一轮链路已经死了，换下一轮"，不是"这条作业废了"。
		if stalled.Load() {
			job.Attempt = 0
			job.Phase = phaseRetrying
			job.LastError = fmt.Sprintf("交付停摆：%s 内没有任何新增进度，换下一轮继续", deliveryStallTimeout)
			job.LastErrorCode = "delivery_stalled"
		} else if job.Superseded || errors.Is(err, errDeliverySuperseded) {
			// 最高优先级：作业已被新会话顶替（fencing）。
			// 这类错误必须直接落终态，绝不能进 retrying——否则退避重排会给它
			// 创建一个全新的 ctx，旧作业就此"复活"并把过期失败写回平台。
			job.Phase = phaseFailed
			job.LastError = "superseded_by_new_session"
			job.LastErrorCode = ""
		} else if errors.Is(err, context.Canceled) {
			// 用户取消 / 绝对上限：同样是终态，但**不得冒充"被顶替"**。
			// cancel() 已经把 LastError 写成 cancelled_by_user，这里覆盖成
			// superseded_by_new_session 会让现场误以为"又被新会话顶掉了"。
			job.Phase = phaseFailed
		} else if isDeliveryAuthError(err) {
			// 凭据类失败单独标记：它不是"重试能好"的错误，必须等人。
			job.NeedsReauth = true
			job.Phase = phaseFailed
		} else if isDeliveryFatalError(err) {
			// 完整性类失败同理：源包与基线不一致，重试只会再白拉一遍 20 GiB。
			job.Phase = phaseFailed
		} else if job.Attempt < budget {
			// 可恢复失败：后台会退避重排，因此对外只能表现为 retrying。
			// 写成 phaseFailed 会让前端先于 bridge 把任务判死（并回写平台 failed），
			// 而字节其实还在继续传——这正是"界面说失败、平台记 failed、包却传完了"的来源。
			job.Phase = phaseRetrying
		} else {
			job.Phase = phaseFailed
		}
	} else {
		job.Attempt = 0
		job.LastError = ""
		job.LastErrorCode = ""
	}
	finalPhase := job.Phase
	finalAttempt := job.Attempt
	needsReauth := job.NeedsReauth
	r.mu.Unlock()

	// 已不是当前执行者的作业不得写 journal：dj-<task>.json 是所有同任务作业共用的
	// 文件名，僵尸收尾会把新作业的记录覆盖回旧会话的阶段与字节数。
	_ = r.saveJob(job)
	evidence.Emit("INFO", "delivery.job_finished", "交付作业状态更新", "", job.CaseID, job.NodeIP, "", map[string]any{
		"job_id": job.JobID, "task_id": job.TaskID, "phase": string(finalPhase),
		"attempt": finalAttempt, "needs_reauth": needsReauth, "error": job.LastError,
		"pulled_bytes": job.PulledBytes, "uploaded_parts": len(job.UploadedParts),
	})

	// 可恢复的失败按退避重排，避免弱网下一抖就判死。
	if err != nil && !needsReauth && finalAttempt < budget {
		delay := deliveryBackoff(finalAttempt)
		go func() {
			time.Sleep(delay)
			// 阶段归一化交给 advance（按已拉字节/已传分片决定从哪续）。
			// 这里只认 retrying：若期间用户重新提交或作业已被取消/判终态，
			// 旧协程不得把作业再拽回执行队列（否则同一 job 会被两条流程同时推进）。
			// 抽成方法是为了让"旧作业不得复活"这条约束能被单测直接锁住。
			if r.requeueEligible(job) {
				r.enqueue(job)
			} else {
				evidence.Emit("INFO", "delivery.requeue_rejected", "被顶替或已取消的作业不再重排", "", job.CaseID, job.NodeIP, "", map[string]any{
					"job_id": job.JobID, "superseded": job.Superseded, "phase": string(job.Phase),
				})
			}
		}()
	}
}

// watchStall 在拉取/上传阶段监视"是否还在动"，连续 deliveryStallTimeout 无增量即终止本轮。
//
// 为什么不是继续用固定墙钟：墙钟既会砍掉"只是慢但在传"的作业（弱网 16 小时 > 旧 6 小时），
// 又会放过"卡死但没到点"的作业（用户盯着"上传中"熬一晚上）。字节增量是唯一既排除死锁
// 又不排斥慢网的可测事实，所以判据换成它。
func (r *deliveryRunner) watchStall(ctx context.Context, cancel context.CancelFunc, job *deliveryJob, done <-chan struct{}, markStalled func()) {
	ticker := time.NewTicker(deliveryStallPollInterval)
	defer ticker.Stop()
	tracker := newStallTracker(deliveryStallTimeout)
	for {
		select {
		case <-ctx.Done():
			return
		case <-done:
			return
		case now := <-ticker.C:
			signature, watched := r.progressSignature(job)
			if !tracker.observe(now, signature, watched) {
				continue
			}
			evidence.Emit("WARN", "delivery.stalled", "交付长时间无进展，终止本轮等下一轮重排", "", job.CaseID, job.NodeIP, "", map[string]any{
				"job_id": job.JobID, "task_id": job.TaskID,
				"stalled_sec": int64(deliveryStallTimeout / time.Second), "signature": signature,
			})
			markStalled()
			cancel()
			return
		}
	}
}

// progressSignature 返回"这个作业是否在动"的可比签名。
//
// 第二返回值表示当前阶段是否适合用字节增量判定。签名只取单调量（已传分片数、
// 已拉字节、当前分片序号），不取时间戳，避免"签名自己会变"造成假进展。
func (r *deliveryRunner) progressSignature(job *deliveryJob) (string, bool) {
	r.mu.Lock()
	phase := job.Phase
	parts := len(job.UploadedParts)
	pulled := job.PulledBytes
	r.mu.Unlock()
	switch phase {
	case phasePulling, phaseUploading:
		return fmt.Sprintf("%s|parts=%d|pulled=%d", phase, parts, pulled), true
	default:
		return fmt.Sprintf("%s", phase), false
	}
}

// advance 按阶段推进；每个阶段都可重入。
func (r *deliveryRunner) advance(ctx context.Context, job *deliveryJob) error {
	// 协作式取消：每个阶段入口都必须先问一次。旧实现只在个别子步骤里检查，
	// 被 cancel 的作业仍会继续 waitCollect / 拉取 / 上传，正是"取消不生效"的根因。
	if err := ctx.Err(); err != nil {
		return err
	}
	// 先做与外部资源无关的守卫：把中间态归一化，并拒绝推进终态/未知阶段。
	// 顺序很关键——放在 sessionFor 之前，既省掉一次无意义的会话解析，
	// 也让"作业不会被误标成功"这条约束可以被单测直接验证。
	r.mu.Lock()
	phase, phaseErr := normalizeDeliveryPhase(job)
	if phaseErr == nil {
		job.Phase = phase
	}
	r.mu.Unlock()
	if phaseErr != nil {
		return phaseErr
	}

	session := r.sessionFor(job)
	if session != nil {
		// 交付的每个需要节点的步骤都是"业务使用"：与日志采集一样没有显式包裹，
		// 靠时间窗保护会话不被空闲租约回收（见 sshx.SSHSession 的 MarkBusy 语义）。
		session.MarkBusy()
	}
	if err := requireSessionForPhase(job.Phase, session); err != nil {
		return err
	}

	switch job.Phase {
	case phaseWaitingCollect, "":
		// 交付只有两条物理前提：节点上有完整包、平台有会话。
		// "采集是否仍在跑"与"能不能传这个已有的包"没有因果关系，
		// 所以补交付（Direct）必须跳过 waitCollect，否则会被节点全局的
		// progress 态拖住甚至误判——那正是 2026-10-27 现场 4 秒"采集完成"的来源。
		if job.Direct {
			if err := r.prepareDirect(ctx, session, job); err != nil {
				return err
			}
		} else if err := r.waitCollect(ctx, session, job); err != nil {
			return err
		}
		fallthrough
	case phasePulling:
		if err := r.pull(ctx, session, job); err != nil {
			return err
		}
		fallthrough
	case phaseUploading:
		if err := r.upload(ctx, job); err != nil {
			return err
		}
		fallthrough
	case phaseCompleting:
		if err := r.complete(ctx, job); err != nil {
			return err
		}
	}

	// 平台已确认收到完整包（complete 返回 2xx），本地副本再无用途。
	// 留着只会挤占下一次交付的空间：P2 中转盘就是客户机 C 盘（2026-11-03 Q2026110300988）。
	//
	// 但删除之前必须重新确认执行者资格：本地副本路径按 task_id 命名，新旧两代作业
	// 指向**同一个文件**。被顶替的作业若在此处收尾，删掉的是新作业正在上传的那个包，
	// 新作业下一片就会以"本地副本不存在"失败——比它自己的失败更糟。
	// 这里只判"还是不是执行者"，**不判 ctx**：走到这一步时 complete 已经 2xx，
	// 包已经落到平台。此时若因为看门狗/绝对上限刚巧 cancel 就返回错误，
	// 会把一次成功的交付写成 failed——比不删副本严重得多。
	if !r.ownerIntact(job) {
		return errDeliverySuperseded
	}
	// 删除本地副本之前读一次 zip 中央目录：包还在盘上时才有内容级证据，
	// 删完就只剩体积推断，608 字节空壳包会被误判成功（Q2026111200624）。
	// 证据回传走端口：交付域只说"为这个采集任务上报本地包清单"，
	// 票据/上传模式/TLS 放宽由采集侧实现决定。
	if reporter := evidence.Transfer(); reporter.Enabled(job.TaskID) {
		evidenceCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		reporter.ReportArchive(evidenceCtx, job.TaskID, job.LocalPath)
		cancel()
	}
	r.discardDeliveredCopy(job)

	r.mu.Lock()
	if job.Superseded {
		// 极窄窗口：fence 通过之后到置终态之前被顶替。此时不得把自己的结论写成 done。
		r.mu.Unlock()
		return errDeliverySuperseded
	}
	job.Phase = phaseDone
	r.mu.Unlock()
	return nil
}

// sessionFor 按 case+node 解析会话；会话掉线但桥持有该工单的认证信息时，
// 后台自动重连并续传，把"需要人工回面板重连"变成自愈（见 ReconnectDeliveryNode）。
// 仅当本阶段确实需要节点时才尝试重连：上传/收尾只读本地副本与平台，不该为它们
// 去敲一条 SSH 连接（否则会在不需要时开连接、甚至因节点不可达而误报 needs_reauth）。
// 两个分支都落空才返回 nil（由调用方转成 needs_reauth）。
func (r *deliveryRunner) sessionFor(job *deliveryJob) NodeSession {
	sess, key, reason := r.hub.ResolveForNode(models.InMessage{CaseID: job.CaseID, NodeIP: job.NodeIP})
	// 交付必须落到作业指定的那台节点。only_session_for_case 是"本工单唯一会话"
	// 的兜底，多节点场景下它可能是另一台机器的会话，拿它执行 acli 命令会落错机器：
	// 2026-11-25 Q2026112500592 的 .35 会话被销毁后，兜底返回 .38 的僵尸会话，
	// 连续 5 轮 acli log_collect progress 全部 no-session 直至失败。
	// 因此兜底命中一律转自动重连，用节点身份重新建链。
	if sess != nil && deliverySessionResolvable(reason) {
		// 具体类型的适配只有装配层知道，这里通过端口取回可执行对象。
		wrapped := r.reconnect.Wrap(key)
		// 只接受"连接仍活着"的会话：Close() 会置空 Client，但对象可能还留在
		// 注册表里。钉住一个僵尸会话等于把本轮直接判死。
		if wrapped != nil && wrapped.Alive() {
			// 钉住会话：交付拉取十几分钟且期间常常零键盘输入，不能让 reaper 按空闲收走。
			// 必须在确认存活之后再 Pin，避免把僵尸会话的租约也钉住。
			r.hub.Pin(key, session.SessionPinTTL())
			return wrapped
		}
	}
	if !deliveryPhaseNeedsNode(job.Phase) {
		return nil
	}
	if reconnected, err := r.reconnect.ReconnectDeliveryNode(job.CaseID, job.NodeIP, "delivery-auto"); err == nil && reconnected != nil && reconnected.Alive() {
		return reconnected
	}
	return nil
}

// deliverySessionResolvable 判断 ResolveForNode 的命中原因能否安全用于交付。
//
// 交付是"对某台指定节点执行命令"，与采集/取消不同：多一次重连可以接受，
// 把命令发到另一台机器绝不可接受。only_session_for_case 只凭工单号找到唯一
// 会话，不携带节点身份，因此在交付域必须拒绝，改走按节点重连。
func deliverySessionResolvable(reason string) bool {
	return reason != "only_session_for_case"
}

// deliveryPhaseNeedsNode 判断该阶段是否必须访问节点（因此必须有 SSH 会话）。
//
// 物理事实：上传与收尾只读**本地副本**、只访问平台 HTTP，全程不碰 HCI。
// 把它们一并绑在 SSH 会话上，等于让"节点连接掉了"去否决一件根本不需要节点的事。
// 现场 Q2026102800720（3.6 GiB）就是这条错误绑定收尾的：平台收尾连续 500 的
// 几分钟里 SSH 会话被回收，第 5 次重试直接按"凭据失效"判死，而当时 110/110 片
// 已在平台、本地副本也完整——差一步就归档成功。
func deliveryPhaseNeedsNode(phase deliveryPhase) bool {
	switch phase {
	case phaseWaitingCollect, phasePulling:
		return true
	default:
		return false
	}
}

// requireSessionForPhase 只在真的需要节点时，把"没有会话"判定为需要重新认证。
func requireSessionForPhase(phase deliveryPhase, session NodeSession) error {
	if session == nil && deliveryPhaseNeedsNode(phase) {
		return &deliveryAuthError{reason: "SSH 会话不存在（需重新连接节点）"}
	}
	return nil
}

// deliveryAuthError 表示"重试无用，必须人工/前端介入"的失败。
type deliveryAuthError struct{ reason string }

func (e *deliveryAuthError) Error() string { return e.reason }

// deliveryFatalError 表示"重试必然同样失败"的完整性/数据错误。
//
// 与 authError 的区别：它不需要人做任何额外动作，只是继续重试没有意义。
// 典型场景：节点源包与拉取结果摘要不一致（包已被同名覆盖），重试会再白拉
// 一次 20 GiB，一天之内就能把客户机带宽和磁盘打满。
type deliveryFatalError struct {
	reason string
	// code 是可选的规范错误码。前端与平台要靠它分支处置，
	// 只匹配中文文案会在文案一改就静默失效（磁盘满 008 的教训）。
	code string
}

func (e *deliveryFatalError) Error() string { return e.reason }

// ErrorCode 让 deliveryErrorCode 能统一取出可落库的码。
func (e *deliveryFatalError) ErrorCode() string { return e.code }

// newFatal 构造带规范错误码的 fatal 失败。
func newFatal(code, format string, args ...any) *deliveryFatalError {
	return &deliveryFatalError{code: code, reason: fmt.Sprintf(format, args...)}
}

// isDeliveryFatalError 用 errors.As 穿透包装。
func isDeliveryFatalError(err error) bool {
	var target *deliveryFatalError
	return errors.As(err, &target)
}

// sourceSizeGuard 判断交付期间源包是否被重新生成（重新采集/重新打包覆盖同名文件）。
//
// 为什么必须是 fatal 而不是 transient：平台上传会话的 total_size 是**提交时快照**，
// 包的大小一变，末片长度乃至整个切片方案都不再与平台匹配。继续按新大小切片只会
// 把同一个 500 重复 N 轮 —— 2026-10-30 Q2026103000994 就是这样：提交 9 片 ×
// 8 MiB（末片 1,901,494 字节），实际文件变成 172 MB，末片按新大小切出 8 MiB，
// 平台按旧会话只允许 1.9 MB，8 轮 attempt 全 500，界面还显示"平台暂时不可用"。
//
// submitted/current 任一为 0 表示信息不足（老前端未上报、stat 失败），此时不做判断，
// 保持原有行为。
func sourceSizeGuard(submitted, current int64) error {
	if submitted <= 0 || current <= 0 || submitted == current {
		return nil
	}
	return &deliveryFatalError{reason: fmt.Sprintf(
		"源包已变化（提交上传时 %d 字节，当前 %d 字节），本次上传已作废："+
			"平台上传会话仍按提交时的大小校验，继续上传只会每片都失败。"+
			"请重新点击「上传到平台」；若要交付最新内容，请先重新采集",
		submitted, current,
	)}
}

// deliveryTransientError 表示"平台暂时不可用，等一会儿重试就能成"。
//
// 与 deliveryAuthError 的本质差别：这个不需要人，只需要时间。
// 典型场景是平台正在滚动更新——空窗几十秒，期间所有请求都会拿到 503。
// deliveryTransientError 标记"暂时不可用，退避重试"。
//
// 它必须保留底层错误链（Unwrap）：类型是控制流的一部分，"这片到底是超时还是 5xx"
// 决定了自适应节奏要不要降并发。2026-11-06 的弱网用例就是踩在这里——包装时只拼了
// 字符串，导致 uplink.IsPartTimeoutError 在上层拿到 false，于是每一轮都"没证据"地原地重试。
type deliveryTransientError struct {
	reason string
	err    error
}

func (e *deliveryTransientError) Error() string { return e.reason }

func (e *deliveryTransientError) Unwrap() error { return e.err }

// deliveryDNSError 表示「本机无法解析平台域名」（NXDOMAIN / no such host）。
//
// 为什么必须与 transient 分开：transient 的语义是「平台暂时不可用，等一会儿就好」，
// 而域名不存在是本机的 hosts / DNS / 代理 / VPN 配错——等再久也不会自己变好。
// 混在一起会让用户按「等平台恢复」去处理，方向完全反了（2026-11-09 现场）。
//
// 只覆盖 IsNotFound：DNS 超时（IsTimeout）仍属链路抖动，保留 transient 语义。
type deliveryDNSError struct {
	reason string
	err    error
}

func (e *deliveryDNSError) Error() string { return e.reason }

func (e *deliveryDNSError) Unwrap() error { return e.err }

// isDeliveryAuthError 用 errors.As 而不是类型断言：错误会被上层用 %w 包装
// （例如 upload() 返回 "上传中断（已传 3/15 片）: <原始错误>"），
// 类型断言遇到包装就失效，令牌失效会被当成普通失败盲重试 5 次，
// 用户永远等不到"请重新认证"。
func isDeliveryAuthError(err error) bool {
	var target *deliveryAuthError
	return errors.As(err, &target)
}

// isDeliveryTransientError 同上，穿透包装。
func isDeliveryTransientError(err error) bool {
	var target *deliveryTransientError
	return errors.As(err, &target)
}

// isDeliveryDNSError 同样必须穿透包装：upload() 会把底层错误包成
// 「上传中断（已传 N/M 片）: ...」，类型断言在那一层已经失效。
func isDeliveryDNSError(err error) bool {
	var target *deliveryDNSError
	return errors.As(err, &target)
}

// isDNENotFound 判断是否为「域名不存在」（NXDOMAIN / no such host）。
//
// 必须先于通用 net.Error 判定：*net.DNSError 同时实现 net.Error，
// 不提前摘出就会被当成可重试的瞬时故障（2026-11-09 现场）。
// 只认 IsNotFound：DNS 超时/临时失败仍属链路抖动，保留 transient 语义，
// 否则会把「DNS 服务器一时不通」也误报成「域名配错」。
func isDNENotFound(err error) bool {
	var dnsErr *net.DNSError
	return errors.As(err, &dnsErr) && dnsErr.IsNotFound
}

// deliveryAttemptBudget 按错误性质给出重试预算（含首次）。
//
// 顺序即优先级，不能随意调换：
//   - 磁盘满 / 准入不足：重试只会再失败一次，1 次即终态（清盘要人做）；
//   - DNS 域名不存在：本机配置问题，只给 VPN/代理切换留 2 次窗口；
//   - 平台 5xx / 连接失败：发版空窗可达数十秒，给足 8 次；
//   - 其余（业务性失败）：默认 5 次。
func deliveryAttemptBudget(err error) int {
	switch {
	case isDeliveryDiskFull(err), isDeliveryAdmission(err):
		return 1
	case isDeliveryDNSError(err):
		return maxDeliveryDNSAttempts
	case isDeliveryTransientError(err):
		return maxDeliveryTransientAttempts
	default:
		return maxDeliveryAttempts
	}
}

// deliveryBackoff 返回第 attempt 次失败后的退避时长：2s→4s→8s→16s→32s→60s。
//
// 起点刻意不是 1 秒：从 1 秒起步会把预算全部耗在一次发版空窗内部
// （2026-10-27 现场：5 次重试挤在 26 秒里，而新 Pod 那一刻刚被创建）。
func deliveryBackoff(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	if attempt > 6 {
		attempt = 6
	}
	delay := time.Duration(1<<uint(attempt)) * time.Second
	if delay > 60*time.Second {
		delay = 60 * time.Second
	}
	return delay
}

// platformStatusError 把非 2xx 的平台响应分类。
//
// 5xx/429 = 平台暂时不可用（发版中、过载），值得等；
// 其余 4xx = 请求本身不合法，重试没有意义，必须原样抛出。
func platformStatusError(action string, status int) error {
	message := fmt.Sprintf("%s HTTP %d", action, status)
	if status == http.StatusTooManyRequests || status >= 500 {
		return &deliveryTransientError{reason: message + "（平台暂时不可用，将退避重试）"}
	}
	return errors.New(message)
}

// isTransientTransportError 判断是否为"网络层暂时不可用"。
//
// 必须先把证书类错误摘出去：url.Error 同时实现了 net.Error，
// 不排除的话 x509 会被当成可重试，用户白等 3 分钟才看到同样的失败。
func isTransientTransportError(err error) bool {
	if err == nil {
		return false
	}
	if uplink.IsUntrustedCertificateError(err) {
		return false
	}
	// 域名不存在是本机解析配置问题，不是链路抖动；混进 transient 会让用户
	// 对着「平台暂时不可用」等到重试预算耗尽（2026-11-09 Q2026110900185）。
	if isDNENotFound(err) {
		return false
	}
	var netErr net.Error
	if errors.As(err, &netErr) {
		return true
	}
	return errors.Is(err, io.EOF)
}

// wrapTransientTransport 把连接类失败标成"暂时不可用"，其余原样返回。
func wrapTransientTransport(err error) error {
	if err == nil {
		return err
	}
	// DNS「域名不存在」必须有自己的文案：原文案把责任指向平台，用户会去等平台恢复，
	// 而真正要做的是检查本机 hosts / DNS / 代理 / VPN。
	if isDNENotFound(err) {
		return &deliveryDNSError{
			reason: "无法解析平台域名（本机 hosts / DNS / 代理 / VPN 异常，请检查后重试）: " + err.Error(),
			err:    err,
		}
	}
	if !isTransientTransportError(err) {
		return err
	}
	return &deliveryTransientError{
		reason: "平台连接失败（暂时不可用，将退避重试）: " + err.Error(),
		err:    err,
	}
}
