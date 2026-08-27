// Package fixtureasset 定义 Bundle Factory 可复用 stdout 资产的最小持久化契约。
package fixtureasset

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const (
	TypeTemplate    = "template"
	TypeInstance    = "instance"
	StatusDraft     = "draft"
	StatusPublished = "published"
	StatusRetired   = "retired"
)

// Asset 是一个不可变修订。编辑必须创建 revision+1，而不是覆盖历史事实。
type Asset struct {
	ID               string          `json:"id"`
	AssetKey         string          `json:"asset_key"`
	AssetType        string          `json:"asset_type"`
	SignalType       string          `json:"signal_type"`
	Revision         int             `json:"revision"`
	Status           string          `json:"status"`
	Content          json.RawMessage `json:"content"`
	TemplateAssetKey *string         `json:"template_asset_key,omitempty"`
	TemplateRevision *int            `json:"template_revision,omitempty"`
	CategoryBaseline json.RawMessage `json:"category_baseline"`
	CatalogBaseline  json.RawMessage `json:"catalog_baseline"`
	ContentDigest    string          `json:"content_digest"`
	CreatedBy        string          `json:"created_by"`
	TraceID          string          `json:"trace_id"`
	CreatedAt        time.Time       `json:"created_at"`
	UpdatedAt        time.Time       `json:"updated_at"`
}

type CreateRequest struct {
	AssetKey         string          `json:"asset_key"`
	AssetType        string          `json:"asset_type"`
	SignalType       string          `json:"signal_type"`
	Content          json.RawMessage `json:"content"`
	TemplateAssetKey string          `json:"template_asset_key,omitempty"`
	TemplateRevision *int            `json:"template_revision,omitempty"`
	CategoryBaseline json.RawMessage `json:"category_baseline"`
	CatalogBaseline  json.RawMessage `json:"catalog_baseline"`
}

// Store 同时服务管理 API 和编译器。生产实现必须由数据库提供事务语义。
type Store interface {
	List(context.Context, string, string, string) ([]Asset, error)
	Get(context.Context, string) ([]Asset, error)
	CreateRevision(context.Context, CreateRequest, string, string) (Asset, error)
	Publish(context.Context, string, int, string, string) (Asset, error)
	Retire(context.Context, string, int, string, string) (Asset, error)
	ResolvePublishedInstance(context.Context, string, string) (Asset, Asset, error)
}

func ValidateCreate(request CreateRequest) error {
	if strings.TrimSpace(request.AssetKey) == "" || len(request.AssetKey) > 128 {
		return errors.New("asset_key 必须为 1-128 个字符")
	}
	if request.AssetType != TypeTemplate && request.AssetType != TypeInstance {
		return errors.New("asset_type 必须为 template 或 instance")
	}
	if !strings.HasPrefix(strings.TrimSpace(request.SignalType), "qkv_") {
		return errors.New("signal_type 必须为 qkv_* 信号")
	}
	if !json.Valid(request.Content) || !json.Valid(request.CategoryBaseline) || !json.Valid(request.CatalogBaseline) {
		return errors.New("content、category_baseline 与 catalog_baseline 必须是有效 JSON")
	}
	if request.AssetType == TypeInstance && (strings.TrimSpace(request.TemplateAssetKey) == "" || request.TemplateRevision == nil || *request.TemplateRevision < 1) {
		return errors.New("instance 必须引用 template_asset_key 和 template_revision")
	}
	if request.AssetType == TypeTemplate && (request.TemplateAssetKey != "" || request.TemplateRevision != nil) {
		return errors.New("template 不得引用其他模板")
	}
	return nil
}

// Keyword 从已冻结的 qkv argv 中解析检索关键字。无法解析时仅使用默认实例。
func Keyword(argv []string) string {
	for i := 0; i+1 < len(argv); i++ {
		if argv[i] == "-k" || argv[i] == "--keyword" {
			return strings.TrimSpace(argv[i+1])
		}
	}
	return ""
}

// Render 将实例 bindings 和本次检索关键字注入模板。模板只能引用显式 bindings，
// 避免把未冻结的运行时环境或请求头意外写进 Draft。
func Render(template Asset, instance Asset, keyword string) (string, error) {
	var templateContent struct {
		StdoutTemplate string `json:"stdout_template"`
	}
	if err := json.Unmarshal(template.Content, &templateContent); err != nil {
		return "", fmt.Errorf("解析模板内容: %w", err)
	}
	if strings.TrimSpace(templateContent.StdoutTemplate) == "" {
		return "", errors.New("模板缺少 stdout_template")
	}
	var instanceContent struct {
		Bindings map[string]string `json:"bindings"`
	}
	if err := json.Unmarshal(instance.Content, &instanceContent); err != nil {
		return "", fmt.Errorf("解析实例内容: %w", err)
	}
	bindings := instanceContent.Bindings
	if bindings == nil {
		bindings = map[string]string{}
	}
	if keyword != "" {
		bindings["KEYWORD"] = keyword
	}
	result := templateContent.StdoutTemplate
	for key, value := range bindings {
		result = strings.ReplaceAll(result, "{{"+key+"}}", value)
	}
	if strings.Contains(result, "{{") || strings.Contains(result, "}}") {
		return "", errors.New("模板存在未绑定变量")
	}
	return result, nil
}
