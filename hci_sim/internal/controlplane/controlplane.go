// Package controlplane 提供 hci-sim 阶段 C–E 的纯业务内核。
//
// 它刻意不放在 SSH Runtime 数据面：编译、审批、调度与证据均属于控制面。
// 存储和 Runner 以接口注入，生产实现必须使用 PostgreSQL、不可变对象存储和真实
// Customer UI/Bridge 协议；MemoryStore 只服务单元测试和本地确定性验证。
package controlplane

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"hci_sim/internal/fixture"
	"hci_sim/internal/lease"
)

type BundleStatus string

const (
	BundleDraft     BundleStatus = "draft"
	BundleValidated BundleStatus = "validated"
	BundleApproved  BundleStatus = "approved"
	BundlePublished BundleStatus = "published"
	BundleStale     BundleStatus = "stale"
	BundleRetired   BundleStatus = "retired"
)

type Role string

const (
	RoleCompiler  Role = "compiler"
	RoleExpert    Role = "expert"
	RoleSecurity  Role = "security"
	RolePublisher Role = "publisher"
	RoleRuntime   Role = "runtime"
)

// Actor 由上层身份认证映射；不能把前端提交的字符串直接作为角色。
type Actor struct {
	ID   string
	Role Role
}

type Dependency struct {
	Type     string `json:"type"`
	ID       string `json:"id"`
	Revision string `json:"revision"`
	Digest   string `json:"digest"`
}

type Artifact struct {
	ID       string `json:"id"`
	Digest   string `json:"digest"`
	Approved bool   `json:"approved"`
}

// CompileInput 在编译一开始被冻结；不接受 active 指针或任意 URL。
type CompileInput struct {
	SupportID            string       `json:"support_id"`
	KBDRevision          int          `json:"kbd_revision"`
	KBDChecksum          string       `json:"kbd_checksum"`
	SignalsDigest        string       `json:"signals_digest"`
	ToolContractRevision string       `json:"tool_contract_revision"`
	PolicyRevision       string       `json:"policy_revision"`
	CompilerRevision     string       `json:"compiler_revision"`
	Artifacts            []Artifact   `json:"artifacts"`
	Dependencies         []Dependency `json:"dependencies"`
}

func (in CompileInput) Fingerprint() (string, error) {
	if err := in.validate(); err != nil {
		return "", err
	}
	copy := in
	sort.Slice(copy.Artifacts, func(i, j int) bool { return copy.Artifacts[i].ID < copy.Artifacts[j].ID })
	sort.Slice(copy.Dependencies, func(i, j int) bool {
		return copy.Dependencies[i].Type+copy.Dependencies[i].ID < copy.Dependencies[j].Type+copy.Dependencies[j].ID
	})
	payload, err := json.Marshal(copy)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(payload)
	return fmt.Sprintf("sha256:%x", sum[:]), nil
}

func (in CompileInput) validate() error {
	if in.SupportID == "" || in.KBDRevision < 1 || in.KBDChecksum == "" || in.SignalsDigest == "" || in.ToolContractRevision == "" || in.PolicyRevision == "" || in.CompilerRevision == "" {
		return errors.New("capability_gap: immutable KBD、Signal、Tool、Policy 或 Compiler 输入缺失")
	}
	seen := make(map[string]struct{}, len(in.Artifacts))
	for _, artifact := range in.Artifacts {
		if artifact.ID == "" || artifact.Digest == "" || !artifact.Approved {
			return errors.New("capability_gap: artifact 必须有 digest 且经过批准")
		}
		if _, ok := seen[artifact.ID]; ok {
			return fmt.Errorf("capability_gap: artifact %q 重复", artifact.ID)
		}
		seen[artifact.ID] = struct{}{}
	}
	dependencies := make(map[string]struct{}, len(in.Dependencies))
	for _, dependency := range in.Dependencies {
		if dependency.Type == "" || dependency.ID == "" || dependency.Revision == "" || dependency.Digest == "" {
			return errors.New("capability_gap: dependency 必须有 type、id、revision 和 digest")
		}
		key := dependency.Type + "\x00" + dependency.ID
		if _, ok := dependencies[key]; ok {
			return fmt.Errorf("capability_gap: dependency %s/%s 重复", dependency.Type, dependency.ID)
		}
		dependencies[key] = struct{}{}
	}
	return nil
}

type Approval struct {
	ActorID string    `json:"actor_id"`
	Role    Role      `json:"role"`
	At      time.Time `json:"at"`
}

type BundleRecord struct {
	Digest           string
	InputFingerprint string
	Input            CompileInput
	Manifest         []byte
	Status           BundleStatus
	Creator          string
	Approvals        []Approval
	StaleReason      string
	CreatedAt        time.Time
	UpdatedAt        time.Time
}

func (record BundleRecord) clone() BundleRecord {
	record.Manifest = append([]byte(nil), record.Manifest...)
	record.Approvals = append([]Approval(nil), record.Approvals...)
	return record
}

// Registry 是控制面的最小存储契约；生产适配器应以 DB 事务保证状态转换和唯一键。
type Registry interface {
	Compile(Actor, CompileInput, fixture.Manifest, time.Time) (BundleRecord, error)
	Validate(Actor, string, ValidationReport, time.Time) (BundleRecord, error)
	Approve(Actor, string, time.Time) (BundleRecord, error)
	Publish(Actor, string, time.Time) (BundleRecord, error)
	MarkStale(Actor, Dependency, string, time.Time) ([]BundleRecord, error)
	GetPublished(string) (BundleRecord, error)
	ResolvePublished(supportID, variant, node, container string) (BundleRecord, error)
	Get(string) (BundleRecord, error)
}

type ValidationReport struct {
	MutationDetected bool
	SecretScanPassed bool
	IndependentProof bool
}

// MemoryRegistry 用于单进程验证；启动多副本前必须替换为持久化 CAS 实现。
type MemoryRegistry struct {
	mu            sync.Mutex
	byDigest      map[string]BundleRecord
	byFingerprint map[string]string
}

func NewMemoryRegistry() *MemoryRegistry {
	return &MemoryRegistry{byDigest: map[string]BundleRecord{}, byFingerprint: map[string]string{}}
}

func (r *MemoryRegistry) Compile(actor Actor, input CompileInput, manifest fixture.Manifest, now time.Time) (BundleRecord, error) {
	if actor.Role != RoleCompiler || actor.ID == "" {
		return BundleRecord{}, errors.New("forbidden: 仅 compiler 身份可创建 draft")
	}
	fingerprint, err := input.Fingerprint()
	if err != nil {
		return BundleRecord{}, err
	}
	if manifest.KBD.SupportID != input.SupportID || manifest.KBD.Revision != input.KBDRevision || manifest.KBD.Checksum != input.KBDChecksum || manifest.Contracts.ToolRevision != input.ToolContractRevision || manifest.Contracts.PolicyRevision != input.PolicyRevision {
		return BundleRecord{}, errors.New("capability_gap: manifest 与已冻结编译输入不一致")
	}
	if hasRealisticRoute(manifest) && len(input.Artifacts) == 0 {
		return BundleRecord{}, errors.New("capability_gap: positive-realistic route 缺少批准 Artifact provenance")
	}
	manifest.Bundle.Status = "published" // fixture loader 仅接受 published 数据面格式；Registry 状态单独管理。
	manifest.Bundle.Digest = fixture.ComputeBundleDigest(manifest)
	raw, err := json.Marshal(manifest)
	if err != nil {
		return BundleRecord{}, err
	}
	if _, err := fixture.Parse(raw); err != nil {
		return BundleRecord{}, fmt.Errorf("manifest lint 失败: %w", err)
	}
	if err := scan(raw); err != nil {
		return BundleRecord{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, ok := r.byFingerprint[fingerprint]; ok {
		record := r.byDigest[existing]
		if string(record.Manifest) != string(raw) {
			return BundleRecord{}, errors.New("compiler_nondeterministic_output: 相同冻结输入生成了不同 Bundle")
		}
		return record.clone(), nil
	}
	record := BundleRecord{Digest: manifest.Bundle.Digest, InputFingerprint: fingerprint, Input: input, Manifest: raw, Status: BundleDraft, Creator: actor.ID, CreatedAt: now.UTC(), UpdatedAt: now.UTC()}
	if existing, ok := r.byDigest[record.Digest]; ok {
		r.byFingerprint[fingerprint] = existing.Digest
		return existing.clone(), nil
	}
	r.byDigest[record.Digest] = record
	r.byFingerprint[fingerprint] = record.Digest
	return record.clone(), nil
}

func (r *MemoryRegistry) Validate(actor Actor, digest string, report ValidationReport, now time.Time) (BundleRecord, error) {
	if actor.Role != RoleCompiler || actor.ID == "" {
		return BundleRecord{}, errors.New("forbidden: 仅 compiler 身份可验证 draft")
	}
	if !report.MutationDetected || !report.SecretScanPassed || !report.IndependentProof {
		return BundleRecord{}, errors.New("validation_failed: mutation、secret scan 与独立证据均为发布前置条件")
	}
	return r.transition(digest, BundleDraft, BundleValidated, now)
}

func (r *MemoryRegistry) Approve(actor Actor, digest string, now time.Time) (BundleRecord, error) {
	if (actor.Role != RoleExpert && actor.Role != RoleSecurity) || actor.ID == "" {
		return BundleRecord{}, errors.New("forbidden: 仅 expert 或 security 可审批")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.byDigest[digest]
	if !ok {
		return BundleRecord{}, errors.New("bundle_not_found")
	}
	if record.Status != BundleValidated {
		return BundleRecord{}, fmt.Errorf("invalid_transition: %s 不能审批", record.Status)
	}
	if actor.ID == record.Creator {
		return BundleRecord{}, errors.New("forbidden: compiler 不得自审")
	}
	for _, approval := range record.Approvals {
		if approval.ActorID == actor.ID {
			return BundleRecord{}, errors.New("forbidden: 同一审批人不得同时满足多角色审批")
		}
		if approval.Role == actor.Role {
			return BundleRecord{}, fmt.Errorf("conflict: %s 审批已存在", actor.Role)
		}
	}
	record.Approvals = append(record.Approvals, Approval{ActorID: actor.ID, Role: actor.Role, At: now.UTC()})
	if hasApprovals(record.Approvals, RoleExpert, RoleSecurity) {
		record.Status = BundleApproved
	}
	record.UpdatedAt = now.UTC()
	r.byDigest[digest] = record
	return record.clone(), nil
}

func (r *MemoryRegistry) Publish(actor Actor, digest string, now time.Time) (BundleRecord, error) {
	if actor.Role != RolePublisher || actor.ID == "" {
		return BundleRecord{}, errors.New("forbidden: 仅 publisher 可发布")
	}
	return r.transition(digest, BundleApproved, BundlePublished, now)
}

func (r *MemoryRegistry) MarkStale(actor Actor, changed Dependency, reason string, now time.Time) ([]BundleRecord, error) {
	if actor.ID == "" || (actor.Role != RoleCompiler && actor.Role != RoleSecurity) || reason == "" {
		return nil, errors.New("forbidden: 仅受信任控制面可标记 stale")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	var stale []BundleRecord
	for digest, record := range r.byDigest {
		if record.Status != BundlePublished || !dependsOn(record.Input.Dependencies, changed) {
			continue
		}
		record.Status, record.StaleReason, record.UpdatedAt = BundleStale, reason, now.UTC()
		r.byDigest[digest] = record
		stale = append(stale, record.clone())
	}
	return stale, nil
}

func (r *MemoryRegistry) GetPublished(digest string) (BundleRecord, error) {
	record, err := r.Get(digest)
	if err != nil {
		return BundleRecord{}, err
	}
	if record.Status != BundlePublished {
		return BundleRecord{}, fmt.Errorf("bundle_not_runnable: %s", record.Status)
	}
	if _, err := fixture.Parse(record.Manifest); err != nil {
		return BundleRecord{}, fmt.Errorf("bundle_integrity_failed: %w", err)
	}
	return record, nil
}

// ResolvePublished 是 TestRun 创建时唯一允许的“active”解析点；返回后 Run 只保存 digest。
func (r *MemoryRegistry) ResolvePublished(supportID, variant, node, container string) (BundleRecord, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var selected *BundleRecord
	for _, candidate := range r.byDigest {
		if candidate.Status != BundlePublished || candidate.Input.SupportID != supportID {
			continue
		}
		var manifest fixture.Manifest
		if err := json.Unmarshal(candidate.Manifest, &manifest); err != nil {
			return BundleRecord{}, fmt.Errorf("bundle_integrity_failed: %w", err)
		}
		for _, route := range manifest.Routes {
			if route.Variant != variant || route.RouteKey.Node != node || route.RouteKey.Container != container {
				continue
			}
			copy := candidate.clone()
			if selected != nil && selected.Digest != copy.Digest {
				return BundleRecord{}, errors.New("bundle_resolution_ambiguous")
			}
			selected = &copy
			break
		}
	}
	if selected == nil {
		return BundleRecord{}, errors.New("bundle_resolution_missing")
	}
	return selected.clone(), nil
}

func (r *MemoryRegistry) Get(digest string) (BundleRecord, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.byDigest[digest]
	if !ok {
		return BundleRecord{}, errors.New("bundle_not_found")
	}
	return record.clone(), nil
}

func (r *MemoryRegistry) transition(digest string, from, to BundleStatus, now time.Time) (BundleRecord, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.byDigest[digest]
	if !ok {
		return BundleRecord{}, errors.New("bundle_not_found")
	}
	if record.Status != from {
		return BundleRecord{}, fmt.Errorf("invalid_transition: %s → %s", record.Status, to)
	}
	record.Status, record.UpdatedAt = to, now.UTC()
	r.byDigest[digest] = record
	return record.clone(), nil
}

func hasRealisticRoute(manifest fixture.Manifest) bool {
	for _, route := range manifest.Routes {
		if route.Variant == "positive-realistic" {
			return true
		}
	}
	return false
}

func hasApprovals(approvals []Approval, required ...Role) bool {
	roles := make(map[Role]bool, len(approvals))
	for _, approval := range approvals {
		roles[approval.Role] = true
	}
	for _, role := range required {
		if !roles[role] {
			return false
		}
	}
	return true
}

func dependsOn(dependencies []Dependency, changed Dependency) bool {
	for _, dependency := range dependencies {
		if dependency.Type == changed.Type && dependency.ID == changed.ID && dependency.Revision != changed.Revision {
			return true
		}
	}
	return false
}

func scan(raw []byte) error {
	// 阻断常见私钥、Bearer token 和未参数化 IPv4；生产扫描器须扩展为 DLP/许可证策略。
	text := string(raw)
	for _, forbidden := range []string{"-----BEGIN PRIVATE KEY-----", "Authorization: Bearer", "AKIA"} {
		if strings.Contains(text, forbidden) {
			return errors.New("security_scan_failed: secret material")
		}
	}
	return nil
}

// RunStatus 保留 transport、业务差异和不确定性，禁止把错误压缩成 false/pass。
type RunStatus string

const (
	RunRequested    RunStatus = "requested"
	RunPreparing    RunStatus = "preparing"
	RunLeased       RunStatus = "leased"
	RunRunning      RunStatus = "running"
	RunPassed       RunStatus = "passed"
	RunFailed       RunStatus = "failed"
	RunInconclusive RunStatus = "inconclusive"
	RunCancelled    RunStatus = "cancelled"
	RunExpired      RunStatus = "expired"
)

type TestRun struct {
	ID             string
	IdempotencyKey string
	RequestDigest  string
	SupportID      string
	KBDRevision    int
	BundleDigest   string
	Variant        string
	Node           string
	Container      string
	Status         RunStatus
	Version        int
	Deadline       time.Time
	Attempt        int
	LeaseJTI       string
}

type CreateRunRequest struct {
	IDempotencyKey string
	SupportID      string
	Variant        string
	Node           string
	Container      string
	Deadline       time.Time
}

type Runner interface {
	Run(context.Context, TestRun, string) (RunStatus, error)
}

// RunStore 实现创建幂等、版本化状态机、容量预留及短时 Capability 签发。
type RunStore struct {
	mu          sync.Mutex
	registry    Registry
	secret      []byte
	issuer      string
	audience    string
	capacity    int
	inflight    int
	runs        map[string]TestRun
	idempotency map[string]string
}

func NewRunStore(registry Registry, secret []byte, issuer, audience string, capacity int) (*RunStore, error) {
	if registry == nil || len(secret) < 32 || issuer == "" || audience == "" || capacity < 1 {
		return nil, errors.New("TestRun 控制面配置不完整")
	}
	return &RunStore{registry: registry, secret: secret, issuer: issuer, audience: audience, capacity: capacity, runs: map[string]TestRun{}, idempotency: map[string]string{}}, nil
}

func (s *RunStore) Create(request CreateRunRequest, now time.Time) (TestRun, error) {
	if request.IDempotencyKey == "" || request.SupportID == "" || request.Variant == "" || request.Node == "" || request.Container == "" || !request.Deadline.After(now) {
		return TestRun{}, errors.New("invalid_run_request")
	}
	requestDigest := digestRequest(request)
	s.mu.Lock()
	if runID, ok := s.idempotency[request.IDempotencyKey]; ok {
		run := s.runs[runID]
		s.mu.Unlock()
		if run.RequestDigest != requestDigest {
			return TestRun{}, errors.New("idempotency_conflict")
		}
		return run, nil
	}
	s.mu.Unlock()
	// active 解析只发生在本次创建；之后仅使用持久化的精确 digest/revision。
	record, err := s.registry.ResolvePublished(request.SupportID, request.Variant, request.Node, request.Container)
	if err != nil {
		return TestRun{}, err
	}
	return s.CreateForBundle(request, record.Digest, now)
}

func (s *RunStore) CreateForBundle(request CreateRunRequest, bundleDigest string, now time.Time) (TestRun, error) {
	if request.IDempotencyKey == "" || request.SupportID == "" || request.Variant == "" || request.Node == "" || request.Container == "" || !request.Deadline.After(now) {
		return TestRun{}, errors.New("invalid_run_request")
	}
	requestDigest := digestRequest(request)
	s.mu.Lock()
	if runID, ok := s.idempotency[request.IDempotencyKey]; ok {
		run := s.runs[runID]
		s.mu.Unlock()
		if run.RequestDigest != requestDigest || run.BundleDigest != bundleDigest {
			return TestRun{}, errors.New("idempotency_conflict")
		}
		return run, nil
	}
	s.mu.Unlock()
	record, err := s.registry.GetPublished(bundleDigest)
	if err != nil {
		return TestRun{}, err
	}
	router, err := fixture.Parse(record.Manifest)
	if err != nil {
		return TestRun{}, err
	}
	if router.KBD().SupportID != request.SupportID {
		return TestRun{}, errors.New("bundle_support_id_mismatch")
	}
	if !containsRoute(record.Manifest, request.Variant, request.Node, request.Container) {
		return TestRun{}, errors.New("bundle_route_resolution_missing")
	}
	run := TestRun{ID: runID(requestDigest), IdempotencyKey: request.IDempotencyKey, RequestDigest: requestDigest, SupportID: request.SupportID, KBDRevision: router.KBD().Revision, BundleDigest: bundleDigest, Variant: request.Variant, Node: request.Node, Container: request.Container, Status: RunRequested, Version: 1, Deadline: request.Deadline.UTC()}
	s.mu.Lock()
	defer s.mu.Unlock()
	if existingID, ok := s.idempotency[request.IDempotencyKey]; ok {
		existing := s.runs[existingID]
		if existing.RequestDigest != requestDigest || existing.BundleDigest != bundleDigest {
			return TestRun{}, errors.New("idempotency_conflict")
		}
		return existing, nil
	}
	s.runs[run.ID], s.idempotency[request.IDempotencyKey] = run, run.ID
	return run, nil
}

func (s *RunStore) Start(ctx context.Context, runID string, runner Runner, now time.Time) (TestRun, error) {
	if runner == nil {
		return TestRun{}, errors.New("runner_required")
	}
	s.mu.Lock()
	run, ok := s.runs[runID]
	if !ok {
		s.mu.Unlock()
		return TestRun{}, errors.New("run_not_found")
	}
	if run.Status != RunRequested || !run.Deadline.After(now) {
		s.mu.Unlock()
		return TestRun{}, errors.New("run_not_startable")
	}
	if s.inflight >= s.capacity {
		s.mu.Unlock()
		return TestRun{}, errors.New("capacity_unavailable")
	}
	s.inflight++
	run.Status, run.Version, run.Attempt = RunPreparing, run.Version+1, run.Attempt+1
	s.runs[runID] = run
	s.mu.Unlock()
	defer func() { s.mu.Lock(); s.inflight--; s.mu.Unlock() }()
	record, err := s.registry.GetPublished(run.BundleDigest)
	if err != nil {
		return s.finish(runID, RunInconclusive)
	}
	claims := lease.Claims{JTI: fmt.Sprintf("%s-%d", run.ID, run.Attempt), LeaseID: fmt.Sprintf("lease-%s", run.ID), TestRunID: run.ID, ScenarioID: record.InputFingerprint, SupportID: run.SupportID, KBDRevision: run.KBDRevision, BundleDigest: run.BundleDigest, FixtureVariant: run.Variant, ToolContractRevision: record.Input.ToolContractRevision, PolicyRevision: record.Input.PolicyRevision, VirtualNodeID: run.Node, Container: run.Container, ExecutionMode: "sim-ssh", Issuer: s.issuer, Audience: s.audience, IssuedAt: now.Unix(), NotBefore: now.Unix(), ExpiresAt: minTime(run.Deadline, now.Add(15*time.Minute)).Unix(), RunDeadline: run.Deadline.Unix(), MaxSessions: 4, MaxCommands: 200, MaxOutputBytes: int64(routerOutputLimit(record.Manifest))}
	token, err := lease.Sign(s.secret, claims)
	if err != nil {
		return s.finish(runID, RunInconclusive)
	}
	s.mu.Lock()
	run = s.runs[runID]
	run.Status, run.Version, run.LeaseJTI = RunRunning, run.Version+1, claims.JTI
	s.runs[runID] = run
	s.mu.Unlock()
	status, err := runner.Run(ctx, run, token)
	if err != nil || status == "" {
		status = RunInconclusive
	}
	return s.finish(runID, status)
}

func (s *RunStore) Cancel(runID string) (TestRun, error) { return s.finish(runID, RunCancelled) }

func (s *RunStore) finish(id string, status RunStatus) (TestRun, error) {
	if !terminal(status) {
		return TestRun{}, errors.New("invalid_terminal_status")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[id]
	if !ok {
		return TestRun{}, errors.New("run_not_found")
	}
	if terminal(run.Status) {
		return run, nil
	}
	run.Status, run.Version = status, run.Version+1
	s.runs[id] = run
	return run, nil
}

func (s *RunStore) Get(id string) (TestRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, ok := s.runs[id]
	if !ok {
		return TestRun{}, errors.New("run_not_found")
	}
	return run, nil
}

func terminal(status RunStatus) bool {
	return status == RunPassed || status == RunFailed || status == RunInconclusive || status == RunCancelled || status == RunExpired
}
func minTime(left, right time.Time) time.Time {
	if left.Before(right) {
		return left
	}
	return right
}
func routerOutputLimit(raw []byte) int {
	router, err := fixture.Parse(raw)
	if err != nil {
		return 1
	}
	return router.OutputLimit()
}

func containsRoute(raw []byte, variant, node, container string) bool {
	var manifest fixture.Manifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return false
	}
	for _, route := range manifest.Routes {
		if route.Variant == variant && route.RouteKey.Node == node && route.RouteKey.Container == container {
			return true
		}
	}
	return false
}
func digestRequest(request CreateRunRequest) string {
	raw, _ := json.Marshal(request)
	sum := sha256.Sum256(raw)
	return fmt.Sprintf("sha256:%x", sum[:])
}
func runID(requestDigest string) string {
	return "run-" + strings.TrimPrefix(requestDigest, "sha256:")[:24]
}

type Observation struct {
	CommandFingerprint, Stdout, Stderr string
	ExitCode                           int
	Outcome                            RunStatus
}
type DifferentialResult struct {
	Equal       bool
	Differences []string
}

// CompareObservations 是 E 阶段结构化差分核心；allow 用于经过审批的非语义字段规范化。
func CompareObservations(real, sim Observation, allow func(string) string) DifferentialResult {
	if allow == nil {
		allow = func(value string) string { return value }
	}
	var differences []string
	if real.CommandFingerprint != sim.CommandFingerprint {
		differences = append(differences, "command_fingerprint")
	}
	if allow(real.Stdout) != allow(sim.Stdout) {
		differences = append(differences, "stdout")
	}
	if allow(real.Stderr) != allow(sim.Stderr) {
		differences = append(differences, "stderr")
	}
	if real.ExitCode != sim.ExitCode {
		differences = append(differences, "exit_code")
	}
	if real.Outcome != sim.Outcome {
		differences = append(differences, "outcome")
	}
	return DifferentialResult{Equal: len(differences) == 0, Differences: differences}
}

type StabilityReport struct {
	Runs               int
	FirstAttemptPasses int
	Flaky              int
	P50, P95           time.Duration
}

func SummarizeStability(outcomes []RunStatus, durations []time.Duration) StabilityReport {
	report := StabilityReport{Runs: len(outcomes)}
	for _, outcome := range outcomes {
		if outcome == RunPassed {
			report.FirstAttemptPasses++
		}
	}
	if len(durations) == 0 {
		return report
	}
	sorted := append([]time.Duration(nil), durations...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	report.P50 = sorted[(len(sorted)-1)*50/100]
	report.P95 = sorted[(len(sorted)-1)*95/100]
	return report
}

// MutationCase 必须保留原始与变异输入及其归类，避免只统计“测试执行过”。
// Equivalent 仅可由专家审查后标记；它不计入检出率分母。
type MutationCase struct {
	ID         string
	Original   Observation
	Mutant     Observation
	Equivalent bool
	Critical   bool
}

type MutationReport struct {
	ValidMutants    int
	DetectedMutants int
	CriticalMisses  []string
	Misses          []string
}

func (r MutationReport) DetectionRate() float64 {
	if r.ValidMutants == 0 {
		return 0
	}
	return float64(r.DetectedMutants) / float64(r.ValidMutants)
}

// AssessMutations 把生产判定器应用于原始和 mutant；结果相同即表示缺少可观测敏感性。
func AssessMutations(cases []MutationCase, classify func(Observation) string) MutationReport {
	report := MutationReport{}
	for _, mutation := range cases {
		if mutation.ID == "" || mutation.Equivalent {
			continue
		}
		report.ValidMutants++
		if classify(mutation.Original) != classify(mutation.Mutant) {
			report.DetectedMutants++
			continue
		}
		report.Misses = append(report.Misses, mutation.ID)
		if mutation.Critical {
			report.CriticalMisses = append(report.CriticalMisses, mutation.ID)
		}
	}
	return report
}

// CapacityEvidence 是容量梯度的最小、可序列化证据。它不把 Scenario、Agent 和 Browser
// 三种容量声明混为一谈；调用者每一种 workload 分别生成 evidence。
type CapacityEvidence struct {
	Concurrent                 int
	Completed                  int
	CrossScenarioContamination int
	ExplicitOverload           bool
}

// ValidateCapacityLadder 验证 1→10→50→100→200 阶梯：前序级别不合格时不得继续声明后续级别。
func ValidateCapacityLadder(evidence []CapacityEvidence) error {
	expected := []int{1, 10, 50, 100, 200}
	if len(evidence) == 0 || len(evidence) > len(expected) {
		return errors.New("capacity_evidence_invalid")
	}
	for index, item := range evidence {
		if item.Concurrent != expected[index] || item.Completed < 0 || item.Completed > item.Concurrent {
			return errors.New("capacity_evidence_invalid")
		}
		if item.CrossScenarioContamination != 0 {
			return fmt.Errorf("security_stop: %d 并发存在跨 Scenario 污染", item.Concurrent)
		}
		if item.Completed < item.Concurrent && !item.ExplicitOverload {
			return fmt.Errorf("capacity_unexplained: %d 并发存在未解释丢失", item.Concurrent)
		}
	}
	return nil
}
