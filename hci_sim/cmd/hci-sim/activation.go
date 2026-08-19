package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	"hci_sim/internal/controlplane"
	"hci_sim/internal/database"
	"hci_sim/internal/fixture"
	"hci_sim/internal/metrics"
)

// activationResult 是 Publish -> Runtime 的明确 ACK，不把“对象已发布”冒充“Runtime 已生效”。
type activationResult struct {
	Status         string `json:"status"`
	SupportID      string `json:"support_id"`
	Digest         string `json:"bundle_digest"`
	PreviousDigest string `json:"previous_bundle_digest,omitempty"`
	Generation     int64  `json:"generation,omitempty"`
	RuntimeID      string `json:"runtime_id,omitempty"`
	TraceID        string `json:"trace_id"`
	FailureCode    string `json:"failure_code,omitempty"`
	FailureMessage string `json:"failure_message,omitempty"`
}

// runtimeBundleActivator 负责 durable pointer、对象完整性校验和内存原子切换。
// GitOps 基线只在启动时进入 BundlePool；已发布 Bundle 通过该控制器热激活。
type runtimeBundleActivator struct {
	pool       *fixture.BundlePool
	registry   controlplane.Registry
	repository *database.RunRepository
	runtimeID  string
	metrics    *metrics.Metrics
}

func newRuntimeBundleActivator(pool *fixture.BundlePool, registry controlplane.Registry, repository *database.RunRepository, runtimeID string, observability ...*metrics.Metrics) *runtimeBundleActivator {
	var prom *metrics.Metrics
	if len(observability) > 0 {
		prom = observability[0]
	}
	return &runtimeBundleActivator{pool: pool, registry: registry, repository: repository, runtimeID: strings.TrimSpace(runtimeID), metrics: prom}
}

func (a *runtimeBundleActivator) ActivateBundle(ctx context.Context, supportID, digest, traceID string) (activationResult, error) {
	if a == nil || a.pool == nil || a.registry == nil {
		return activationResult{}, errors.New("runtime bundle activator unavailable")
	}
	if a.metrics != nil {
		a.metrics.BundleActivationsTotal.Add(1)
	}
	supportID, digest, traceID = strings.TrimSpace(supportID), strings.TrimSpace(digest), strings.TrimSpace(traceID)
	if supportID == "" || digest == "" || traceID == "" {
		return activationResult{}, errors.New("runtime bundle activation requires support_id, digest and trace_id")
	}
	record, err := a.registry.GetPublished(digest)
	if err != nil {
		if a.metrics != nil {
			a.metrics.BundleActivationFailuresTotal.Add(1)
		}
		return activationResult{}, fmt.Errorf("读取已发布 Bundle 失败: %w", err)
	}
	if record.Input.SupportID != supportID {
		return activationResult{}, errors.New("runtime bundle activation support_id 不匹配")
	}
	router, err := fixture.Parse(record.Manifest)
	if err != nil {
		return activationResult{}, fmt.Errorf("Runtime 激活前 manifest 校验失败: %w", err)
	}
	if router.KBD().SupportID != supportID || router.BundleDigest() != digest {
		return activationResult{}, errors.New("runtime bundle activation manifest identity mismatch")
	}

	previous := a.pool.Get(supportID)
	previousDigest := ""
	if previous != nil {
		previousDigest = previous.BundleDigest()
	}
	if a.repository != nil {
		if _, err := a.repository.RequestBundleActivation(ctx, supportID, digest, "bundle-publisher", a.runtimeID, traceID); err != nil {
			return activationResult{}, err
		}
	}
	if _, err := a.pool.Activate(router); err != nil {
		if a.metrics != nil {
			a.metrics.BundleActivationFailuresTotal.Add(1)
		}
		if a.repository != nil {
			_ = a.repository.FailBundleActivation(ctx, supportID, digest, a.runtimeID, traceID, "memory_swap_failed", err.Error())
		}
		return activationResult{}, err
	}
	if a.repository != nil {
		ack, ackErr := a.repository.AckBundleActivation(ctx, supportID, digest, a.runtimeID, traceID)
		if ackErr != nil {
			// 内存切换已经成功，保留新 Router；durable pointer 仍为 pending/failed，
			// 下一次 reconcile 会重试 ACK，绝不回退到未经验证的旧对象。
			_ = a.repository.FailBundleActivation(ctx, supportID, digest, a.runtimeID, traceID, "ack_persist_failed", ackErr.Error())
			return activationResult{Status: "pending", SupportID: supportID, Digest: digest, PreviousDigest: previousDigest, RuntimeID: a.runtimeID, TraceID: traceID, FailureCode: "ack_persist_failed", FailureMessage: ackErr.Error()}, ackErr
		} else {
			return activationResult{Status: ack.Status, SupportID: supportID, Digest: digest, PreviousDigest: previousDigest, Generation: ack.Generation, RuntimeID: a.runtimeID, TraceID: traceID}, nil
		}
	}
	return activationResult{Status: "active", SupportID: supportID, Digest: digest, PreviousDigest: previousDigest, RuntimeID: a.runtimeID, TraceID: traceID}, nil
}

// RestoreActive 从 durable pointer 恢复热激活版本。任何 pointer/object/digest 不一致
// 都阻止 Runtime Ready，避免静默退回 GitOps legacy Bundle。
func (a *runtimeBundleActivator) RestoreActive(ctx context.Context) error {
	if a == nil || a.repository == nil {
		return nil
	}
	records, err := a.repository.ListActiveBundleActivations(ctx)
	if err != nil {
		return fmt.Errorf("读取 Runtime active pointer 失败: %w", err)
	}
	for _, activation := range records {
		if activation.ActiveDigest == "" || activation.DesiredDigest != activation.ActiveDigest {
			return fmt.Errorf("Runtime active pointer 不一致: support_id=%s", activation.SupportID)
		}
		record, getErr := a.registry.GetPublished(activation.ActiveDigest)
		if getErr != nil {
			return fmt.Errorf("恢复 Runtime Bundle %s 失败: %w", activation.ActiveDigest, getErr)
		}
		router, parseErr := fixture.Parse(record.Manifest)
		if parseErr != nil || router.KBD().SupportID != activation.SupportID || router.BundleDigest() != activation.ActiveDigest {
			return fmt.Errorf("恢复 Runtime Bundle %s 完整性失败: %v", activation.ActiveDigest, parseErr)
		}
		if _, activateErr := a.pool.Activate(router); activateErr != nil {
			return activateErr
		}
		log.Printf("hci-sim active bundle restored support_id=%s digest=%s generation=%d trace_id=%s", activation.SupportID, activation.ActiveDigest, activation.Generation, activation.TraceID)
	}
	return nil
}

// ReconcilePending 重试控制面已写入但尚未 ACK 的激活，避免短暂数据库/对象存储故障
// 把发布永久卡在 pending。每次重试都复用同一 Bundle digest，并产生新的调用链。
func (a *runtimeBundleActivator) ReconcilePending(ctx context.Context) error {
	if a == nil || a.repository == nil {
		return nil
	}
	records, err := a.repository.ListPendingBundleActivations(ctx)
	if err != nil {
		return err
	}
	for _, record := range records {
		traceID, traceErr := newTraceID()
		if traceErr != nil {
			traceID = fmt.Sprintf("activation-retry-%d", time.Now().UnixNano())
		}
		if _, activateErr := a.ActivateBundle(ctx, record.SupportID, record.DesiredDigest, traceID); activateErr != nil {
			log.Printf("hci-sim bundle activation retry failed support_id=%s digest=%s trace_id=%s error=%v", record.SupportID, record.DesiredDigest, traceID, activateErr)
		}
	}
	return nil
}

func (a *runtimeBundleActivator) RollbackBundle(ctx context.Context, supportID, traceID string) (activationResult, error) {
	if a == nil || a.repository == nil {
		return activationResult{}, errors.New("runtime bundle rollback unavailable")
	}
	activation, err := a.repository.GetBundleActivation(ctx, strings.TrimSpace(supportID))
	if err != nil {
		return activationResult{}, err
	}
	if activation.PreviousDigest == "" {
		return activationResult{}, errors.New("bundle_activation_previous_missing")
	}
	return a.ActivateBundle(ctx, activation.SupportID, activation.PreviousDigest, traceID)
}

func activationJSON(result activationResult) map[string]any {
	raw, _ := json.Marshal(result)
	var value map[string]any
	_ = json.Unmarshal(raw, &value)
	return value
}
