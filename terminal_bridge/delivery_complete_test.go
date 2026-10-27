package main

// 收尾请求体的契约护栏。
//
// 现场回归：15 个分片全部传成功（1.4 GB），最后一步 complete 返回 HTTP 422，
// 任务是 failed、分片白传。根因是请求体里带了 `"sha256": ""`，而平台对该字段
// 有 `^[0-9a-f]{64}$` 格式约束——空串在**请求体校验层**就被拒，业务代码没被调用。
// 这类缺陷不会出现在任何业务日志里，只能靠断死请求体形状来防复发。

import (
	"encoding/json"
	"testing"
)

func TestBuildCompletePayloadOmitsEmptySha256(t *testing.T) {
	job := &deliveryJob{
		TaskID:        "task-1",
		UploadedParts: []int{1, 2, 3},
		SourceSize:    1048576,
	}

	payload := buildCompletePayload(job)
	if _, ok := payload["sha256"]; ok {
		t.Fatalf("请求体不得包含 sha256 键（空串会触发平台 422），实际: %v", payload)
	}

	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	if _, ok := decoded["sha256"]; ok {
		t.Fatalf("序列化后仍出现 sha256 键: %s", raw)
	}
	if got := decoded["size"]; got != float64(1048576) {
		t.Fatalf("size 应为 1048576，实际 %v", got)
	}
	parts, ok := decoded["parts"].([]any)
	if !ok || len(parts) != 3 {
		t.Fatalf("parts 应完整保留，实际 %v", decoded["parts"])
	}
}

func TestBuildCompletePayloadOmitsUnknownSize(t *testing.T) {
	// size=0 表示"还没量到"：缺省即不校验，避免刚恢复的作业被平台的 ge=1 拦下。
	payload := buildCompletePayload(&deliveryJob{UploadedParts: []int{1}})
	if _, ok := payload["size"]; ok {
		t.Fatalf("size 未知时不应发送该字段，实际: %v", payload)
	}
}
