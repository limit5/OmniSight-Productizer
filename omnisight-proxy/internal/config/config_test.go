package config

import (
	"strings"
	"testing"
)

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

func TestValidateRejectsInvalidNonceTTL(t *testing.T) {
	cfg := ForTest()
	cfg.NonceTTLSeconds = 0
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected non-positive NonceTTLSeconds to be rejected")
	}
}

func TestValidateRequiresAuthFieldsWhenEnabled(t *testing.T) {
	cfg := ForTest()
	cfg.AuthEnabled = true
	cfg.TenantID = "tenant-a"
	cfg.ServerCertFile = "server.crt"
	cfg.ServerKeyFile = "server.key"
	cfg.ClientCAFile = "ca.crt"
	cfg.PinnedClientCertSHA256 = "sha256:" + strings.Repeat("a", 64)

	if err := cfg.Validate(); err == nil {
		t.Fatal("expected missing NonceHMACKeyFile to be rejected")
	}

	cfg.NonceHMACKeyFile = "nonce.key"
	if err := cfg.Validate(); err != nil {
		t.Fatalf("expected complete auth config to validate: %v", err)
	}
}

func TestLoadRejectsInvalidAuthEnabled(t *testing.T) {
	t.Setenv("OMNISIGHT_PROXY_AUTH_ENABLED", "maybe")
	if _, err := Load(); err == nil {
		t.Fatal("expected invalid auth bool to be rejected")
	}
}

func TestLoadRejectsInvalidNonceTTL(t *testing.T) {
	t.Setenv("OMNISIGHT_PROXY_NONCE_TTL_SECONDS", "five")
	if _, err := Load(); err == nil {
		t.Fatal("expected invalid nonce TTL to be rejected")
	}
}
