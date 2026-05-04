package config

import (
	"os"
	"path/filepath"
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

func TestValidateRejectsInvalidHeartbeatInterval(t *testing.T) {
	cfg := ForTest()
	cfg.HeartbeatIntervalSeconds = 0
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected non-positive HeartbeatIntervalSeconds to be rejected")
	}
}

func TestValidateRequiresProxyIDWhenHeartbeatConfigured(t *testing.T) {
	cfg := ForTest()
	cfg.SaaSHeartbeatURL = "https://app.example.test/api/v1/byog/proxies/proxy-a/heartbeat"
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected missing ProxyID to be rejected")
	}

	cfg.ProxyID = "proxy-a"
	if err := cfg.Validate(); err != nil {
		t.Fatalf("expected heartbeat config with proxy id to validate: %v", err)
	}
}

func TestValidateRequiresProxyIDWhenSaaSAuditConfigured(t *testing.T) {
	cfg := ForTest()
	cfg.SaaSAuditURL = "https://app.example.test/api/v1/byog/proxies/proxy-a/audit"
	if err := cfg.Validate(); err == nil {
		t.Fatal("expected missing ProxyID to be rejected")
	}

	cfg.ProxyID = "proxy-a"
	if err := cfg.Validate(); err != nil {
		t.Fatalf("expected SaaS audit config with proxy id to validate: %v", err)
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

func TestLoadDefaultsHeartbeatIntervalToThirtySeconds(t *testing.T) {
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() failed: %v", err)
	}
	if cfg.HeartbeatIntervalSeconds != 30 {
		t.Fatalf("HeartbeatIntervalSeconds = %d, want 30", cfg.HeartbeatIntervalSeconds)
	}
}

func TestLoadRejectsInvalidHeartbeatInterval(t *testing.T) {
	t.Setenv("OMNISIGHT_PROXY_HEARTBEAT_INTERVAL_SECONDS", "soon")
	if _, err := Load(); err == nil {
		t.Fatal("expected invalid heartbeat interval to be rejected")
	}
}

func TestValidateAcceptsMultiProviderCatalog(t *testing.T) {
	catalog := &ProviderCatalog{
		Providers: []ProviderConfig{
			{
				Name:    "anthropic",
				BaseURL: "https://api.anthropic.com",
				Models:  []string{"claude-3-5-sonnet-latest"},
				KeySource: ProviderKeySource{
					Type: KeySourceLocalFile,
					Path: "/run/secrets/anthropic_api_key",
				},
			},
			{
				Name:    "openai",
				BaseURL: "https://api.openai.com",
				KeySource: ProviderKeySource{
					Type:              KeySourceKMS,
					KMSProvider:       "aws",
					KMSKeyID:          "arn:aws:kms:us-east-1:111122223333:key/abcd",
					KMSCiphertextFile: "/run/secrets/openai_api_key.kms",
				},
			},
			{
				Name: "gemini",
				KeySource: ProviderKeySource{
					Type:           KeySourceVault,
					VaultAddress:   "https://vault.internal:8200",
					VaultTokenFile: "/run/secrets/vault_token",
					VaultMount:     "secret",
					VaultPath:      "llm/gemini",
					VaultField:     "api_key",
				},
			},
		},
	}

	if err := catalog.Validate(); err != nil {
		t.Fatalf("expected multi-provider catalog to validate: %v", err)
	}
}

func TestValidateRejectsDuplicateProviders(t *testing.T) {
	catalog := &ProviderCatalog{
		Providers: []ProviderConfig{
			{
				Name: "OpenAI",
				KeySource: ProviderKeySource{
					Type: KeySourceLocalFile,
					Path: "/run/secrets/openai_api_key",
				},
			},
			{
				Name: "openai",
				KeySource: ProviderKeySource{
					Type: KeySourceLocalFile,
					Path: "/run/secrets/openai_backup_key",
				},
			},
		},
	}

	if err := catalog.Validate(); err == nil {
		t.Fatal("expected duplicate providers to be rejected")
	}
}

func TestValidateRejectsUnknownKeySource(t *testing.T) {
	catalog := &ProviderCatalog{
		Providers: []ProviderConfig{
			{
				Name: "anthropic",
				KeySource: ProviderKeySource{
					Type: "env",
					Path: "ANTHROPIC_API_KEY",
				},
			},
		},
	}

	if err := catalog.Validate(); err == nil {
		t.Fatal("expected unknown key source to be rejected")
	}
}

func TestReadLocalFileKeyTrimsWhitespace(t *testing.T) {
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "provider.key")
	if err := os.WriteFile(keyFile, []byte("  sk-test\n"), 0o600); err != nil {
		t.Fatalf("write key file: %v", err)
	}

	key, err := (ProviderKeySource{
		Type: KeySourceLocalFile,
		Path: keyFile,
	}).ReadLocalFileKey()
	if err != nil {
		t.Fatalf("read local file key: %v", err)
	}
	if key != "sk-test" {
		t.Fatalf("key = %q, want sk-test", key)
	}
}

func TestLoadReadsProviderCatalogFile(t *testing.T) {
	dir := t.TempDir()
	configFile := filepath.Join(dir, "providers.json")
	body := `{
		"providers": [
			{
				"name": "anthropic",
				"base_url": "https://api.anthropic.com",
				"key_source": {
					"type": "local_file",
					"path": "/run/secrets/anthropic_api_key"
				}
			},
			{
				"name": "openai",
				"key_source": {
					"type": "kms",
					"kms_provider": "gcp",
					"kms_key_id": "projects/p/locations/global/keyRings/r/cryptoKeys/k",
					"kms_ciphertext_file": "/run/secrets/openai_api_key.kms"
				}
			}
		]
	}`
	if err := os.WriteFile(configFile, []byte(body), 0o600); err != nil {
		t.Fatalf("write provider config: %v", err)
	}
	t.Setenv("OMNISIGHT_PROXY_PROVIDER_CONFIG_FILE", configFile)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() failed: %v", err)
	}
	if cfg.ProviderCatalog == nil {
		t.Fatal("expected provider catalog to load")
	}
	if got := len(cfg.ProviderCatalog.Providers); got != 2 {
		t.Fatalf("provider count = %d, want 2", got)
	}
}

func TestLoadRejectsUnknownProviderConfigFields(t *testing.T) {
	dir := t.TempDir()
	configFile := filepath.Join(dir, "providers.json")
	body := `{
		"providers": [
			{
				"name": "anthropic",
				"unknown": true,
				"key_source": {
					"type": "local_file",
					"path": "/run/secrets/anthropic_api_key"
				}
			}
		]
	}`
	if err := os.WriteFile(configFile, []byte(body), 0o600); err != nil {
		t.Fatalf("write provider config: %v", err)
	}
	t.Setenv("OMNISIGHT_PROXY_PROVIDER_CONFIG_FILE", configFile)

	if _, err := Load(); err == nil {
		t.Fatal("expected unknown provider config field to be rejected")
	}
}

func TestExampleProviderConfigMatchesSchema(t *testing.T) {
	catalog, err := LoadProviderCatalogFile(filepath.Join("..", "..", "config.example.json"))
	if err != nil {
		t.Fatalf("example provider config must match schema: %v", err)
	}
	if got := len(catalog.Providers); got != 3 {
		t.Fatalf("example provider count = %d, want 3", got)
	}
}
