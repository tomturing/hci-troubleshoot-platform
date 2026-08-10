package database

import "testing"

func TestParseOnlyAcceptsHciSimDatabase(t *testing.T) {
	valid, err := Parse("postgres://sim:secret@postgres:5432/hci_sim?sslmode=disable")
	if err != nil || !valid.Configured || valid.Database != "hci_sim" {
		t.Fatalf("valid hci_sim URL rejected: %#v %v", valid, err)
	}
	for _, raw := range []string{
		"postgres://sim:secret@postgres:5432/hci_troubleshoot",
		"mysql://sim:secret@postgres:3306/hci_sim",
		"postgres://sim:secret@/hci_sim",
	} {
		if _, err := Parse(raw); err == nil {
			t.Fatalf("unsafe URL accepted: %q", raw)
		}
	}
}

func TestFromEnvironmentFailsClosedWhenRequired(t *testing.T) {
	t.Setenv("HCI_SIM_DATABASE_REQUIRED", "true")
	t.Setenv("HCI_SIM_DATABASE_URL", "")
	if _, err := FromEnvironment(); err == nil {
		t.Fatal("missing required database URL was accepted")
	}
	t.Setenv("HCI_SIM_DATABASE_URL", "postgres://sim:secret@postgres:5432/hci_troubleshoot")
	if _, err := FromEnvironment(); err == nil {
		t.Fatal("platform database URL was accepted")
	}
}
