package fixture

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func poolManifest(t *testing.T, supportID string, revision int) []byte {
	t.Helper()
	manifest := Manifest{
		SchemaVersion: SchemaVersion,
		Bundle:        BundleRef{Status: "published"},
		KBD:           KBDRef{SupportID: supportID, Revision: revision, Checksum: "sha256:kbd"},
		Contracts:     Contracts{ToolRevision: "tool-v1", PolicyRevision: "policy-v1"},
		Limits:        Limits{MaxRoutes: 1, MaxOutputBytesPerCommand: 4096, MaxBundleBytes: 65536},
		Routes:        []Route{},
	}
	manifest.Bundle.Digest = ComputeBundleDigest(manifest)
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func writePoolManifest(t *testing.T, dir, name, supportID string, revision int) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), poolManifest(t, supportID, revision), 0600); err != nil {
		t.Fatal(err)
	}
}

func TestBundlePoolLoadsDeterministicPublishedSet(t *testing.T) {
	dir := t.TempDir()
	writePoolManifest(t, dir, "kbd-27123.json", "27123", 25)
	writePoolManifest(t, dir, "kbd-23821.json", "23821", 25)
	pool, err := LoadBundlePoolDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(pool.SupportIDs(), ","); got != "23821,27123" {
		t.Fatalf("support_id 顺序不稳定: %s", got)
	}
	if pool.Get("23821") == nil || pool.Get("unknown") != nil || pool.MaxOutputLimit() != 4096 {
		t.Fatal("BundlePool 路由集合不符合预期")
	}
	required := map[string]string{}
	for _, bundle := range pool.Bundles() {
		required[bundle.SupportID] = bundle.BundleDigest
	}
	raw, _ := json.Marshal(required)
	if err := pool.ValidateRequired(string(raw)); err != nil {
		t.Fatalf("正确发布声明被拒绝: %v", err)
	}
}

func TestBundlePoolFailsClosedOnDuplicateOrDigestDrift(t *testing.T) {
	dir := t.TempDir()
	writePoolManifest(t, dir, "one.json", "23821", 25)
	writePoolManifest(t, dir, "two.json", "23821", 26)
	if _, err := LoadBundlePoolDir(dir); err == nil || !strings.Contains(err.Error(), "重复") {
		t.Fatalf("重复 support_id 未被拒绝: %v", err)
	}

	dir = t.TempDir()
	writePoolManifest(t, dir, "one.json", "23821", 25)
	pool, err := LoadBundlePoolDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := pool.ValidateRequired(`{"23821":"sha256:wrong"}`); err == nil || !strings.Contains(err.Error(), "漂移") {
		t.Fatalf("错误 digest 未被拒绝: %v", err)
	}
	if err := pool.ValidateRequired(`{"23821":"` + pool.Get("23821").BundleDigest() + `","99999":"sha256:x"}`); err == nil {
		t.Fatal("夹带未加载 Bundle 的发布声明未被拒绝")
	}
}
