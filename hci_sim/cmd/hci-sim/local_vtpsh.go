package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var (
	localVTPSHMonitorPattern = regexp.MustCompile(
		`^/nodes/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/qemu/[0-9]{1,20}/monitor$`,
	)
	localVTPSHScreendumpPattern = regexp.MustCompile(
		`^screendump (/tmp/hci-vm-console-[A-Za-z0-9._-]+/[0-9a-fA-F-]{36}\.ppm)$`,
	)
)

// runLocalVTPSH 是离线实验室镜像的受控多调用入口。它只接受生产采集器
// 固定生成的 screendump/sendkey down 形态；不实现任意 vtpsh 命令，也不经 shell。
func runLocalVTPSH(args []string) int {
	if len(args) != 4 || args[0] != "create" || args[2] != "--command" {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim vtpsh adapter: unsupported argv")
		return 127
	}
	monitor := localVTPSHMonitorPattern.FindStringSubmatch(args[1])
	if len(monitor) != 2 || monitor[1] != env("HCI_SIM_VIRTUAL_NODE_ID", "SIM-HCI-NODE-01") {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim vtpsh adapter: target node mismatch")
		return 126
	}
	if args[3] == "sendkey down" {
		return 0
	}
	match := localVTPSHScreendumpPattern.FindStringSubmatch(args[3])
	if len(match) != 2 {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim vtpsh adapter: unsupported monitor operation")
		return 127
	}
	target := filepath.Clean(match[1])
	if !strings.HasPrefix(target, "/tmp/hci-vm-console-") || filepath.Ext(target) != ".ppm" {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim vtpsh adapter: unsafe capture path")
		return 126
	}
	nearBlack := strings.Contains(strings.ToLower(env("HCI_SIM_FIXTURE_VARIANT", "positive")), "near-black")
	if err := os.WriteFile(target, localVMConsolePPM(nearBlack), 0o600); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "hci-sim vtpsh adapter:", err)
		return 1
	}
	return 0
}

func localVMConsolePPM(nearBlack bool) []byte {
	const width, height = 8, 6
	header := fmt.Sprintf("P6\n%d %d\n255\n", width, height)
	pixels := make([]byte, 0, width*height*3)
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			if nearBlack {
				pixels = append(pixels, byte((x+y)%3), byte((x+y)%2), byte((x*y)%3))
				continue
			}
			pixels = append(pixels, byte(64+x*24), byte(96+y*24), byte(200))
		}
	}
	return append([]byte(header), pixels...)
}
