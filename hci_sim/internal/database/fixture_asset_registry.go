package database

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"hci_sim/internal/fixtureasset"
)

// FixtureAssetRegistry 是 stdout 模板/实例的 PostgreSQL 存储实现。
type FixtureAssetRegistry struct{ pool *pgxpool.Pool }

func NewFixtureAssetRegistry(pool *pgxpool.Pool) (*FixtureAssetRegistry, error) {
	if pool == nil {
		return nil, errors.New("fixture asset database pool is required")
	}
	return &FixtureAssetRegistry{pool: pool}, nil
}

func (r *FixtureAssetRegistry) List(ctx context.Context, signalType, assetType, status string) ([]fixtureasset.Asset, error) {
	rows, err := r.pool.Query(ctx, `SELECT id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id, created_at, updated_at FROM fixture.asset_revision WHERE ($1 = '' OR signal_type = $1) AND ($2 = '' OR asset_type = $2) AND ($3 = '' OR status = $3) ORDER BY signal_type, asset_type, asset_key, revision DESC`, signalType, assetType, status)
	if err != nil {
		return nil, fmt.Errorf("查询资产: %w", err)
	}
	defer rows.Close()
	return scanAssets(rows)
}

func (r *FixtureAssetRegistry) Get(ctx context.Context, assetKey string) ([]fixtureasset.Asset, error) {
	rows, err := r.pool.Query(ctx, `SELECT id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id, created_at, updated_at FROM fixture.asset_revision WHERE asset_key = $1 ORDER BY revision DESC`, assetKey)
	if err != nil {
		return nil, fmt.Errorf("查询资产修订: %w", err)
	}
	defer rows.Close()
	return scanAssets(rows)
}

func (r *FixtureAssetRegistry) CreateRevision(ctx context.Context, request fixtureasset.CreateRequest, actor, traceID string) (fixtureasset.Asset, error) {
	if err := fixtureasset.ValidateCreate(request); err != nil {
		return fixtureasset.Asset{}, err
	}
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fixtureasset.Asset{}, err
	}
	defer tx.Rollback(ctx)
	if request.AssetType == fixtureasset.TypeInstance {
		var typeName, signalType string
		err = tx.QueryRow(ctx, `SELECT asset_type, signal_type FROM fixture.asset_revision WHERE asset_key = $1 AND revision = $2`, request.TemplateAssetKey, *request.TemplateRevision).Scan(&typeName, &signalType)
		if err != nil {
			return fixtureasset.Asset{}, fmt.Errorf("引用模板不存在: %w", err)
		}
		if typeName != fixtureasset.TypeTemplate || signalType != request.SignalType {
			return fixtureasset.Asset{}, errors.New("实例必须引用同信号类型的模板修订")
		}
	}
	// 每个业务键使用事务级顾问锁，避免并发编辑生成相同 revision。
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, request.AssetKey); err != nil {
		return fixtureasset.Asset{}, err
	}
	var revision int
	if err = tx.QueryRow(ctx, `SELECT COALESCE(MAX(revision), 0) + 1 FROM fixture.asset_revision WHERE asset_key = $1`, request.AssetKey).Scan(&revision); err != nil {
		return fixtureasset.Asset{}, err
	}
	digest := digestAsset(request)
	asset, err := insertAsset(ctx, tx, fixtureasset.Asset{ID: uuid.NewString(), AssetKey: request.AssetKey, AssetType: request.AssetType, SignalType: request.SignalType, Revision: revision, Status: fixtureasset.StatusDraft, Content: request.Content, TemplateAssetKey: request.TemplateAssetKey, TemplateRevision: request.TemplateRevision, CategoryBaseline: request.CategoryBaseline, CatalogBaseline: request.CatalogBaseline, ContentDigest: digest, CreatedBy: actor, TraceID: traceID})
	if err != nil {
		return fixtureasset.Asset{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return fixtureasset.Asset{}, err
	}
	return asset, nil
}

func (r *FixtureAssetRegistry) Publish(ctx context.Context, assetKey string, revision int, actor, traceID string) (fixtureasset.Asset, error) {
	return r.transition(ctx, assetKey, revision, fixtureasset.StatusPublished, actor, traceID)
}
func (r *FixtureAssetRegistry) Retire(ctx context.Context, assetKey string, revision int, actor, traceID string) (fixtureasset.Asset, error) {
	return r.transition(ctx, assetKey, revision, fixtureasset.StatusRetired, actor, traceID)
}

func (r *FixtureAssetRegistry) transition(ctx context.Context, assetKey string, revision int, status, actor, traceID string) (fixtureasset.Asset, error) {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return fixtureasset.Asset{}, err
	}
	defer tx.Rollback(ctx)
	if status == fixtureasset.StatusPublished {
		var assetType, templateKey string
		var templateRevision *int
		if err = tx.QueryRow(ctx, `SELECT asset_type, template_asset_key, template_revision FROM fixture.asset_revision WHERE asset_key = $1 AND revision = $2 FOR UPDATE`, assetKey, revision).Scan(&assetType, &templateKey, &templateRevision); err != nil {
			return fixtureasset.Asset{}, err
		}
		if assetType == fixtureasset.TypeInstance {
			var templateStatus string
			if templateRevision == nil {
				return fixtureasset.Asset{}, errors.New("实例缺少模板修订")
			}
			if err = tx.QueryRow(ctx, `SELECT status FROM fixture.asset_revision WHERE asset_key = $1 AND revision = $2`, templateKey, *templateRevision).Scan(&templateStatus); err != nil {
				return fixtureasset.Asset{}, fmt.Errorf("实例引用模板不存在: %w", err)
			}
			if templateStatus != fixtureasset.StatusPublished {
				return fixtureasset.Asset{}, errors.New("发布实例前必须先发布其模板修订")
			}
		}
		if _, err = tx.Exec(ctx, `UPDATE fixture.asset_revision SET status = 'retired', updated_at = now() WHERE asset_key = $1 AND status = 'published' AND revision <> $2`, assetKey, revision); err != nil {
			return fixtureasset.Asset{}, err
		}
	}
	row := tx.QueryRow(ctx, `UPDATE fixture.asset_revision SET status = $3, updated_at = now(), trace_id = $4 WHERE asset_key = $1 AND revision = $2 RETURNING id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id, created_at, updated_at`, assetKey, revision, status, traceID)
	asset, err := scanAsset(row)
	if err != nil {
		return fixtureasset.Asset{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return fixtureasset.Asset{}, err
	}
	return asset, nil
}

func (r *FixtureAssetRegistry) ResolvePublishedInstance(ctx context.Context, signalType, keyword string) (fixtureasset.Asset, fixtureasset.Asset, error) {
	rows, err := r.pool.Query(ctx, `SELECT i.id, i.asset_key, i.asset_type, i.signal_type, i.revision, i.status, i.content_json, i.template_asset_key, i.template_revision, i.category_baseline_json, i.catalog_baseline_json, i.content_digest, i.created_by, i.trace_id, i.created_at, i.updated_at, t.id, t.asset_key, t.asset_type, t.signal_type, t.revision, t.status, t.content_json, t.template_asset_key, t.template_revision, t.category_baseline_json, t.catalog_baseline_json, t.content_digest, t.created_by, t.trace_id, t.created_at, t.updated_at FROM fixture.asset_revision i JOIN fixture.asset_revision t ON t.asset_key = i.template_asset_key AND t.revision = i.template_revision WHERE i.signal_type = $1 AND i.asset_type = 'instance' AND i.status = 'published' AND t.asset_type = 'template' AND t.status = 'published' ORDER BY i.revision DESC`, signalType)
	if err != nil {
		return fixtureasset.Asset{}, fixtureasset.Asset{}, err
	}
	defer rows.Close()
	var fallback *fixtureasset.Asset
	var fallbackTemplate *fixtureasset.Asset
	for rows.Next() {
		var instance, template fixtureasset.Asset
		if err := scanAssetPair(rows, &instance, &template); err != nil {
			return fixtureasset.Asset{}, fixtureasset.Asset{}, err
		}
		if selectedForKeyword(instance, keyword) {
			return instance, template, nil
		}
		if fallback == nil && defaultInstance(instance) {
			fallback = &instance
			fallbackTemplate = &template
		}
	}
	if err := rows.Err(); err != nil {
		return fixtureasset.Asset{}, fixtureasset.Asset{}, err
	}
	if fallback != nil {
		return *fallback, *fallbackTemplate, nil
	}
	return fixtureasset.Asset{}, fixtureasset.Asset{}, pgx.ErrNoRows
}

func selectedForKeyword(asset fixtureasset.Asset, keyword string) bool {
	var content struct {
		Selection struct {
			Keyword string `json:"keyword"`
			Default bool   `json:"default"`
		} `json:"selection"`
	}
	if json.Unmarshal(asset.Content, &content) != nil {
		return false
	}
	return content.Selection.Keyword != "" && keyword != "" && strings.Contains(keyword, content.Selection.Keyword)
}

func defaultInstance(asset fixtureasset.Asset) bool {
	var content struct {
		Selection struct {
			Default bool `json:"default"`
		} `json:"selection"`
	}
	return json.Unmarshal(asset.Content, &content) == nil && content.Selection.Default
}
func digestAsset(request fixtureasset.CreateRequest) string {
	payload, _ := json.Marshal(request)
	sum := sha256.Sum256(payload)
	return "sha256:" + hex.EncodeToString(sum[:])
}

type rowScanner interface{ Scan(...any) error }

func scanAsset(row rowScanner) (fixtureasset.Asset, error) {
	var asset fixtureasset.Asset
	err := row.Scan(&asset.ID, &asset.AssetKey, &asset.AssetType, &asset.SignalType, &asset.Revision, &asset.Status, &asset.Content, &asset.TemplateAssetKey, &asset.TemplateRevision, &asset.CategoryBaseline, &asset.CatalogBaseline, &asset.ContentDigest, &asset.CreatedBy, &asset.TraceID, &asset.CreatedAt, &asset.UpdatedAt)
	return asset, err
}
func scanAssets(rows pgx.Rows) ([]fixtureasset.Asset, error) {
	var assets []fixtureasset.Asset
	for rows.Next() {
		asset, err := scanAsset(rows)
		if err != nil {
			return nil, err
		}
		assets = append(assets, asset)
	}
	return assets, rows.Err()
}
func scanAssetPair(row rowScanner, instance, template *fixtureasset.Asset) error {
	values := []any{&instance.ID, &instance.AssetKey, &instance.AssetType, &instance.SignalType, &instance.Revision, &instance.Status, &instance.Content, &instance.TemplateAssetKey, &instance.TemplateRevision, &instance.CategoryBaseline, &instance.CatalogBaseline, &instance.ContentDigest, &instance.CreatedBy, &instance.TraceID, &instance.CreatedAt, &instance.UpdatedAt, &template.ID, &template.AssetKey, &template.AssetType, &template.SignalType, &template.Revision, &template.Status, &template.Content, &template.TemplateAssetKey, &template.TemplateRevision, &template.CategoryBaseline, &template.CatalogBaseline, &template.ContentDigest, &template.CreatedBy, &template.TraceID, &template.CreatedAt, &template.UpdatedAt}
	return row.Scan(values...)
}
func insertAsset(ctx context.Context, tx pgx.Tx, asset fixtureasset.Asset) (fixtureasset.Asset, error) {
	row := tx.QueryRow(ctx, `INSERT INTO fixture.asset_revision (id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id, created_at, updated_at`, asset.ID, asset.AssetKey, asset.AssetType, asset.SignalType, asset.Revision, asset.Status, asset.Content, asset.TemplateAssetKey, asset.TemplateRevision, asset.CategoryBaseline, asset.CatalogBaseline, asset.ContentDigest, asset.CreatedBy, asset.TraceID)
	return scanAsset(row)
}
