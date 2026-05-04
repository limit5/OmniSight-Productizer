// Package config centralises omnisight-proxy runtime settings.

package config

import (
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
)

// Settings aggregates every runtime knob owned by the BYOG proxy. Later
// KS.3 rows keep extending this struct instead of scattering env reads
// through request-handling code.
type Settings struct {
	Addr                     string
	LogLevel                 string
	AuthEnabled              bool
	ProxyID                  string
	TenantID                 string
	ServerCertFile           string
	ServerKeyFile            string
	ClientCAFile             string
	PinnedClientCertSHA256   string
	NonceHMACKeyFile         string
	NonceTTLSeconds          int
	ProviderConfigFile       string
	ProviderCatalog          *ProviderCatalog
	SaaSHeartbeatURL         string
	HeartbeatIntervalSeconds int
}

// Load parses environment-backed settings and validates their shape.
func Load() (*Settings, error) {
	authEnabled, err := envBoolDefault("OMNISIGHT_PROXY_AUTH_ENABLED", false)
	if err != nil {
		return nil, err
	}
	nonceTTLSeconds, err := envIntDefault("OMNISIGHT_PROXY_NONCE_TTL_SECONDS", 300)
	if err != nil {
		return nil, err
	}
	heartbeatIntervalSeconds, err := envIntDefault("OMNISIGHT_PROXY_HEARTBEAT_INTERVAL_SECONDS", 30)
	if err != nil {
		return nil, err
	}
	s := &Settings{
		Addr:                     envDefault("OMNISIGHT_PROXY_ADDR", ":8080"),
		LogLevel:                 strings.ToLower(envDefault("OMNISIGHT_PROXY_LOG_LEVEL", "info")),
		AuthEnabled:              authEnabled,
		ProxyID:                  envDefault("OMNISIGHT_PROXY_ID", ""),
		TenantID:                 envDefault("OMNISIGHT_PROXY_TENANT_ID", ""),
		ServerCertFile:           envDefault("OMNISIGHT_PROXY_TLS_CERT_FILE", ""),
		ServerKeyFile:            envDefault("OMNISIGHT_PROXY_TLS_KEY_FILE", ""),
		ClientCAFile:             envDefault("OMNISIGHT_PROXY_CLIENT_CA_FILE", ""),
		PinnedClientCertSHA256:   envDefault("OMNISIGHT_PROXY_PINNED_CLIENT_CERT_SHA256", ""),
		NonceHMACKeyFile:         envDefault("OMNISIGHT_PROXY_NONCE_HMAC_KEY_FILE", ""),
		NonceTTLSeconds:          nonceTTLSeconds,
		ProviderConfigFile:       envDefault("OMNISIGHT_PROXY_PROVIDER_CONFIG_FILE", ""),
		SaaSHeartbeatURL:         envDefault("OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL", ""),
		HeartbeatIntervalSeconds: heartbeatIntervalSeconds,
	}
	if s.ProviderConfigFile != "" {
		catalog, err := LoadProviderCatalogFile(s.ProviderConfigFile)
		if err != nil {
			return nil, err
		}
		s.ProviderCatalog = catalog
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	return s, nil
}

// ForTest returns deterministic settings without reading process env.
func ForTest() *Settings {
	return &Settings{
		Addr:                     "127.0.0.1:0",
		LogLevel:                 "error",
		NonceTTLSeconds:          300,
		HeartbeatIntervalSeconds: 30,
	}
}

func (s *Settings) Validate() error {
	if s.Addr == "" {
		return fmt.Errorf("OMNISIGHT_PROXY_ADDR must be non-empty")
	}
	switch s.LogLevel {
	case "debug", "info", "warn", "error":
	default:
		return fmt.Errorf("OMNISIGHT_PROXY_LOG_LEVEL must be one of debug|info|warn|error, got %q", s.LogLevel)
	}
	if s.NonceTTLSeconds <= 0 {
		return fmt.Errorf("OMNISIGHT_PROXY_NONCE_TTL_SECONDS must be positive")
	}
	if s.HeartbeatIntervalSeconds <= 0 {
		return fmt.Errorf("OMNISIGHT_PROXY_HEARTBEAT_INTERVAL_SECONDS must be positive")
	}
	if s.SaaSHeartbeatURL != "" && s.ProxyID == "" {
		return fmt.Errorf("OMNISIGHT_PROXY_ID must be non-empty when OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL is set")
	}
	if s.AuthEnabled {
		required := map[string]string{
			"OMNISIGHT_PROXY_TENANT_ID":                 s.TenantID,
			"OMNISIGHT_PROXY_TLS_CERT_FILE":             s.ServerCertFile,
			"OMNISIGHT_PROXY_TLS_KEY_FILE":              s.ServerKeyFile,
			"OMNISIGHT_PROXY_CLIENT_CA_FILE":            s.ClientCAFile,
			"OMNISIGHT_PROXY_PINNED_CLIENT_CERT_SHA256": s.PinnedClientCertSHA256,
			"OMNISIGHT_PROXY_NONCE_HMAC_KEY_FILE":       s.NonceHMACKeyFile,
		}
		for key, value := range required {
			if value == "" {
				return fmt.Errorf("%s must be non-empty when OMNISIGHT_PROXY_AUTH_ENABLED=true", key)
			}
		}
		if !validSHA256Fingerprint(s.PinnedClientCertSHA256) {
			return fmt.Errorf("OMNISIGHT_PROXY_PINNED_CLIENT_CERT_SHA256 must be sha256:<64 hex chars>")
		}
	}
	if s.ProviderCatalog != nil {
		if err := s.ProviderCatalog.Validate(); err != nil {
			return err
		}
	}
	return nil
}

func (s *Settings) SlogLevel() slog.Level {
	switch s.LogLevel {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

func envDefault(key string, fallback string) string {
	if value, ok := os.LookupEnv(key); ok {
		return value
	}
	return fallback
}

func envBoolDefault(key string, fallback bool) (bool, error) {
	value, ok := os.LookupEnv(key)
	if !ok {
		return fallback, nil
	}
	switch strings.ToLower(value) {
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off":
		return false, nil
	default:
		return false, fmt.Errorf("%s must be a boolean", key)
	}
}

func envIntDefault(key string, fallback int) (int, error) {
	value, ok := os.LookupEnv(key)
	if !ok {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer", key)
	}
	return parsed, nil
}

func validSHA256Fingerprint(value string) bool {
	normalized := strings.TrimPrefix(strings.ToLower(strings.TrimSpace(value)), "sha256:")
	if len(normalized) != 64 {
		return false
	}
	_, err := hex.DecodeString(normalized)
	return err == nil
}
