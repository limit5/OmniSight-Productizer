// Package config centralises omnisight-proxy runtime settings.

package config

import (
	"fmt"
	"log/slog"
	"os"
	"strings"
)

// Settings aggregates every runtime knob owned by KS.3.1. Later KS.3
// rows add auth and provider configuration here instead of scattering
// env reads through request-handling code.
type Settings struct {
	Addr     string
	LogLevel string
}

// Load parses environment-backed settings and validates their shape.
func Load() (*Settings, error) {
	s := &Settings{
		Addr:     envDefault("OMNISIGHT_PROXY_ADDR", ":8080"),
		LogLevel: strings.ToLower(envDefault("OMNISIGHT_PROXY_LOG_LEVEL", "info")),
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	return s, nil
}

// ForTest returns deterministic settings without reading process env.
func ForTest() *Settings {
	return &Settings{
		Addr:     "127.0.0.1:0",
		LogLevel: "error",
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
