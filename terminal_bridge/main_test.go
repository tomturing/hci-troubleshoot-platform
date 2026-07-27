package main

import "testing"

func TestLineMatchesOutputFilterAllAndExclude(t *testing.T) {
	filter := OutputFilter{
		Source:        "stdout",
		Include:       []string{"4359974862144", "qcow2"},
		Exclude:       []string{"grep"},
		IncludeMode:   "all",
		CaseSensitive: true,
	}
	if !lineMatchesOutputFilter("qemu 123 /images/4359974862144.vm/disk.qcow2\n", filter) {
		t.Fatal("expected the VM image line to match")
	}
	if lineMatchesOutputFilter("grep 4359974862144 qcow2\n", filter) {
		t.Fatal("exclude must win over include")
	}
}

func TestLineMatchesOutputFilterAnyCaseInsensitive(t *testing.T) {
	filter := OutputFilter{
		Source:        "stdout",
		Include:       []string{"server-img", "4359974862144"},
		IncludeMode:   "any",
		CaseSensitive: false,
	}
	if !lineMatchesOutputFilter("SERVER-IMG is busy\n", filter) {
		t.Fatal("case-insensitive any mode should match")
	}
	if lineMatchesOutputFilter("unrelated process\n", filter) {
		t.Fatal("unrelated line must not match")
	}
}

func TestFiltersForSourceIgnoresEmptyAndOtherStream(t *testing.T) {
	filters := []OutputFilter{
		{Source: "stdout", Include: []string{"VM"}},
		{Source: "stderr", Include: []string{"error"}},
		{Source: "stdout"},
	}
	selected := filtersForSource(filters, "stdout")
	if len(selected) != 1 || selected[0].Include[0] != "VM" {
		t.Fatalf("unexpected stdout filters: %#v", selected)
	}
}

func TestRelayOutputBudgetIsSharedAndFailClosed(t *testing.T) {
	budget := &relayOutputBudget{}
	if !budget.reserve(maxRelayedOutputBytes - 1) {
		t.Fatal("first reservation should fit")
	}
	if budget.reserve(2) {
		t.Fatal("stdout/stderr shared budget must reject overflow")
	}
	if budget.reserve(1) {
		t.Fatal("budget must remain fail-closed after overflow")
	}
}

func TestValidateOutputFilters(t *testing.T) {
	valid := []OutputFilter{{
		Source: "stdout", Include: []string{"4359974862144"}, IncludeMode: "all", CaseSensitive: true,
	}}
	if err := validateOutputFilters(valid); err != nil {
		t.Fatalf("valid literal filter rejected: %v", err)
	}
	invalid := []OutputFilter{{Source: "invalid", Include: []string{"VM"}, IncludeMode: "all"}}
	if err := validateOutputFilters(invalid); err == nil {
		t.Fatal("invalid source must be rejected")
	}
}
