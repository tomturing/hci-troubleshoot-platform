package main

import (
	"os/exec"
	"strings"
	"testing"
)

// 下载段的两个纯逻辑：远端命令拼接、续传起点判定。
//
// 为什么单独测这两处：它们是"从 HCI 取字节"唯一有分支的地方，
// 出错后表现都是**整包传完才失败**（摘要对不上 / 包打不开），
// 现场代价是几十分钟到一个小时的空跑，因此必须在进程内先把边界钉死。

// TestBuildPullCommandIsOneBased tail -c 的 N 是 1-based。
//
// 这是最容易错、且错了最难发现的一处：offset=0 传成 +0 会被 GNU tail 当成
// "最后 0 字节"直接返回空，于是本地包永远长不起来，却没有任何报错。
func TestBuildPullCommandIsOneBased(t *testing.T) {
	cases := []struct {
		name   string
		offset int64
		want   string
	}{
		{"从头读", 0, "tail -c +1 "},
		{"续传一字节", 1, "tail -c +2 "},
		{"续传整片", 1048576, "tail -c +1048577 "},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := buildPullCommand("/sf/data/local/acli/tmp/data/log_collect/t.zip", tc.offset)
			if !strings.HasPrefix(got, tc.want) {
				t.Fatalf("偏移计算错误：offset=%d 期望前缀 %q，实际 %q", tc.offset, tc.want, got)
			}
		})
	}
}

// TestBuildPullCommandQuotesPath 路径里的空格与元字符不能改变命令语义。
//
// 现场工单号来自用户输入，最终会拼进远端 shell；只要有一次没转义，
// 一个带分号或反引号的路径就能在客户节点上执行任意命令。
//
// 判据用**真 shell 回放**而不是字符串包含判断：后者容易写成
// `strings.Contains(cmd, ";rm")`，而单引号内的 `;rm` 是合法字面量，
// 断言会误报注入（第一次写这个用例时就踩到了）。让 /bin/sh 亲自解析，
// 拿到与原路径逐字节相同的输出，才是"不可注入"的物理证据。
func TestBuildPullCommandQuotesPath(t *testing.T) {
	evil := "/sf/data/a b;rm -rf /tmp/x`id`$(whoami).zip"
	command := buildPullCommand(evil, 0)

	// 只回放路径参数部分：buildPullCommand = "tail -c +N " + shellQuote(path)
	quoted := strings.TrimPrefix(command, "tail -c +1 ")
	if quoted == command {
		t.Fatalf("命令结构不符预期：%s", command)
	}

	// #nosec G204 -- 测试用固定字符串回放转义结果，非外部输入
	out, err := exec.Command("/bin/sh", "-c", "printf '%s' "+quoted).Output()
	if err != nil {
		t.Fatalf("shell 回放失败: %v", err)
	}
	if string(out) != evil {
		t.Fatalf("转义结果经 shell 解析后与原文不一致，存在注入风险\n期望: %q\n实际: %q", evil, string(out))
	}
}

// TestResolvePullStartBoundaries 续传起点判定的三种边界。
func TestResolvePullStartBoundaries(t *testing.T) {
	cases := []struct {
		name       string
		localSize  int64
		remoteSize int64
		wantOffset int64
		wantDiscar bool
		wantDone   bool
	}{
		{"本地无文件", 0, 1000, 0, false, false},
		{"本地半包续传", 400, 1000, 400, false, false},
		{"本地恰好完整", 1000, 1000, 1000, false, true},
		{"本地比源包大必须丢弃", 1200, 1000, 0, true, false},
		{"远端大小未知按续传", 400, 0, 400, false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := resolvePullStart(tc.localSize, tc.remoteSize)
			if got.Offset != tc.wantOffset || got.Discard != tc.wantDiscar || got.Complete != tc.wantDone {
				t.Fatalf("判定不符：local=%d remote=%d 期望 offset=%d discard=%v complete=%v，实际 %+v",
					tc.localSize, tc.remoteSize, tc.wantOffset, tc.wantDiscar, tc.wantDone, got)
			}
		})
	}
}

// TestResolvePullStartNeverResumesPastSource 任何情况下起点都不能越过源包大小。
//
// 越过即意味着 `tail -c +N` 从文件尾之后开始，节点会静默返回空流，
// bridge 却认为"读完了"，最终产出一个比源包小的本地包——摘要校验能发现，
// 但那时已经白跑了一整轮。
func TestResolvePullStartNeverResumesPastSource(t *testing.T) {
	for local := int64(0); local <= 2000; local += 137 {
		const remote = int64(1000)
		got := resolvePullStart(local, remote)
		if got.Offset > remote {
			t.Fatalf("起点越过源包：local=%d remote=%d → offset=%d", local, remote, got.Offset)
		}
	}
}
