package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRunLocalVTPSHWritesControlledScreendump(t *testing.T) {
	t.Setenv("HCI_SIM_VIRTUAL_NODE_ID", "SIM-HCI-NODE-01")
	dir, err := os.MkdirTemp("/tmp", "hci-vm-console-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	target := filepath.Join(dir, "123e4567-e89b-12d3-a456-426614174000.ppm")
	code := runLocalVTPSH([]string{
		"create", "/nodes/SIM-HCI-NODE-01/qemu/90010001/monitor", "--command", "screendump " + target,
	})
	if code != 0 {
		t.Fatalf("runLocalVTPSH code=%d", code)
	}
	raw, err := os.ReadFile(target)
	if err != nil || len(raw) == 0 {
		t.Fatalf("capture err=%v size=%d", err, len(raw))
	}
}

func TestRunLocalVTPSHRejectsArbitraryOperationAndWrongNode(t *testing.T) {
	t.Setenv("HCI_SIM_VIRTUAL_NODE_ID", "SIM-HCI-NODE-01")
	if code := runLocalVTPSH([]string{
		"create", "/nodes/SIM-HCI-NODE-01/qemu/90010001/monitor", "--command", "sendkey ctrl-alt-delete",
	}); code == 0 {
		t.Fatal("arbitrary operation must fail")
	}
	if code := runLocalVTPSH([]string{
		"create", "/nodes/OTHER/qemu/90010001/monitor", "--command", "sendkey down",
	}); code == 0 {
		t.Fatal("wrong node must fail")
	}
}
