package remote

import (
	"strings"
	"testing"
)

func TestValidateArchiveOwnership(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name    string
		path    string
		caseID  string
		wantErr bool
	}{
		{"同工单包放行", "/sf/data/local/acli/tmp/data/log_collect/Q2026112500790.zip", "Q2026112500790", false},
		{"别的工单包拒绝", "/sf/data/local/acli/tmp/data/log_collect/Q2026080200163.zip", "Q2026112500790", true},
		{"大小写不一致拒绝", "/sf/data/local/acli/tmp/data/log_collect/q2026112500790.zip", "Q2026112500790", true},
		{"工单号为空不猜测", "/sf/data/local/acli/tmp/data/log_collect/Q2026080200163.zip", "  ", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateArchiveOwnership(tc.path, tc.caseID)
			if tc.wantErr && err == nil {
				t.Fatalf("期望拒绝 %s / %s，实际放行", tc.path, tc.caseID)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("期望放行 %s / %s，实际报错：%v", tc.path, tc.caseID, err)
			}
			if err != nil && !strings.Contains(err.Error(), tc.caseID) {
				t.Fatalf("错误信息应包含期望工单号，实际：%v", err)
			}
		})
	}
}
