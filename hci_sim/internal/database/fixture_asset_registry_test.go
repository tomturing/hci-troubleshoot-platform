package database

import (
	"testing"

	"hci_sim/internal/fixtureasset"
)

type fixtureAssetRowScanner func(...any) error

func (scan fixtureAssetRowScanner) Scan(values ...any) error {
	return scan(values...)
}

func TestScanAssetAllowsNullTemplateReference(t *testing.T) {
	asset, err := scanAsset(fixtureAssetRowScanner(func(values ...any) error {
		if len(values) != 16 {
			t.Fatalf("scan destination count = %d, want 16", len(values))
		}
		templateKey, ok := values[7].(**string)
		if !ok {
			t.Fatalf("template_asset_key destination type = %T, want **string", values[7])
		}
		*templateKey = nil
		templateRevision, ok := values[8].(**int)
		if !ok {
			t.Fatalf("template_revision destination type = %T, want **int", values[8])
		}
		*templateRevision = nil
		return nil
	}))
	if err != nil {
		t.Fatalf("scanAsset() error = %v", err)
	}
	if asset.TemplateAssetKey != nil || asset.TemplateRevision != nil {
		t.Fatalf("template reference = (%v, %v), want (nil, nil)", asset.TemplateAssetKey, asset.TemplateRevision)
	}
}

func TestScanAssetPairAllowsNullTemplateReference(t *testing.T) {
	var instance, template fixtureasset.Asset
	err := scanAssetPair(fixtureAssetRowScanner(func(values ...any) error {
		if len(values) != 32 {
			t.Fatalf("scan destination count = %d, want 32", len(values))
		}
		templateKey, ok := values[23].(**string)
		if !ok {
			t.Fatalf("template_asset_key destination type = %T, want **string", values[23])
		}
		*templateKey = nil
		templateRevision, ok := values[24].(**int)
		if !ok {
			t.Fatalf("template_revision destination type = %T, want **int", values[24])
		}
		*templateRevision = nil
		return nil
	}), &instance, &template)
	if err != nil {
		t.Fatalf("scanAssetPair() error = %v", err)
	}
	if template.TemplateAssetKey != nil || template.TemplateRevision != nil {
		t.Fatalf("template reference = (%v, %v), want (nil, nil)", template.TemplateAssetKey, template.TemplateRevision)
	}
}
