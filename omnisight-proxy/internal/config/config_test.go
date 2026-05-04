package config

import "testing"

func TestForTestIsValid(t *testing.T) {
	cfg := ForTest()
	if err := cfg.Validate(); err != nil {
		t.Fatalf("ForTest() must be valid: %v", err)
	}
}

func TestValidateRejectsBlankAddr(t *testing.T) {
	cfg := ForTest()
	cfg.Addr = ""
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected blank Addr to be rejected")
	}
}

func TestValidateRejectsUnknownLogLevel(t *testing.T) {
	cfg := ForTest()
	cfg.LogLevel = "trace"
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected unknown LogLevel to be rejected")
	}
}
