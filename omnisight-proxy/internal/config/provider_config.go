package config

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"
)

const (
	KeySourceLocalFile = "local_file"
	KeySourceKMS       = "kms"
	KeySourceVault     = "vault"
)

// ProviderCatalog is the KS.3.3 multi-provider config schema. It is
// loaded once per proxy process from the customer-owned config file, so
// every replica derives the same provider list from the same mounted
// artifact without sharing mutable module-global state.
type ProviderCatalog struct {
	Providers []ProviderConfig `json:"providers"`
}

type ProviderConfig struct {
	Name      string            `json:"name"`
	BaseURL   string            `json:"base_url,omitempty"`
	Models    []string          `json:"models,omitempty"`
	Headers   map[string]string `json:"headers,omitempty"`
	KeySource ProviderKeySource `json:"key_source"`
}

type ProviderKeySource struct {
	Type string `json:"type"`

	// local_file source.
	Path string `json:"path,omitempty"`

	// kms source. CiphertextFile is the customer-mounted encrypted API key
	// blob; KS.3.4 request forwarding owns decrypt/use semantics.
	KMSProvider       string `json:"kms_provider,omitempty"`
	KMSKeyID          string `json:"kms_key_id,omitempty"`
	KMSCiphertextFile string `json:"kms_ciphertext_file,omitempty"`

	// vault source.
	VaultAddress   string `json:"vault_address,omitempty"`
	VaultTokenFile string `json:"vault_token_file,omitempty"`
	VaultMount     string `json:"vault_mount,omitempty"`
	VaultPath      string `json:"vault_path,omitempty"`
	VaultField     string `json:"vault_field,omitempty"`
}

func LoadProviderCatalogFile(path string) (*ProviderCatalog, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("read provider config: %w", err)
	}
	defer file.Close()

	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	var catalog ProviderCatalog
	if err := decoder.Decode(&catalog); err != nil {
		return nil, fmt.Errorf("parse provider config: %w", err)
	}
	if err := catalog.Validate(); err != nil {
		return nil, err
	}
	return &catalog, nil
}

func (c *ProviderCatalog) Validate() error {
	if c == nil {
		return fmt.Errorf("provider config is required")
	}
	if len(c.Providers) == 0 {
		return fmt.Errorf("provider config must include at least one provider")
	}
	seen := make(map[string]struct{}, len(c.Providers))
	for i := range c.Providers {
		provider := &c.Providers[i]
		if err := provider.Validate(); err != nil {
			return fmt.Errorf("providers[%d]: %w", i, err)
		}
		key := strings.ToLower(strings.TrimSpace(provider.Name))
		if _, ok := seen[key]; ok {
			return fmt.Errorf("providers[%d]: duplicate provider %q", i, provider.Name)
		}
		seen[key] = struct{}{}
	}
	return nil
}

func (p *ProviderConfig) Validate() error {
	p.Name = strings.TrimSpace(p.Name)
	if p.Name == "" {
		return fmt.Errorf("name is required")
	}
	if strings.ContainsAny(p.Name, " \t\r\n") {
		return fmt.Errorf("name must not contain whitespace")
	}
	p.BaseURL = strings.TrimSpace(p.BaseURL)
	if p.BaseURL != "" {
		if err := validateHTTPURL("base_url", p.BaseURL); err != nil {
			return err
		}
	}
	for i, model := range p.Models {
		if strings.TrimSpace(model) == "" {
			return fmt.Errorf("models[%d] must be non-empty", i)
		}
	}
	for key, value := range p.Headers {
		if strings.TrimSpace(key) == "" || strings.TrimSpace(value) == "" {
			return fmt.Errorf("headers keys and values must be non-empty")
		}
	}
	return p.KeySource.Validate()
}

func (s *ProviderKeySource) Validate() error {
	s.Type = strings.TrimSpace(s.Type)
	switch s.Type {
	case KeySourceLocalFile:
		if strings.TrimSpace(s.Path) == "" {
			return fmt.Errorf("key_source.path is required for local_file")
		}
	case KeySourceKMS:
		if strings.TrimSpace(s.KMSProvider) != "aws" && strings.TrimSpace(s.KMSProvider) != "gcp" {
			return fmt.Errorf("key_source.kms_provider must be aws or gcp")
		}
		if strings.TrimSpace(s.KMSKeyID) == "" {
			return fmt.Errorf("key_source.kms_key_id is required for kms")
		}
		if strings.TrimSpace(s.KMSCiphertextFile) == "" {
			return fmt.Errorf("key_source.kms_ciphertext_file is required for kms")
		}
	case KeySourceVault:
		if err := validateHTTPURL("key_source.vault_address", strings.TrimSpace(s.VaultAddress)); err != nil {
			return err
		}
		required := map[string]string{
			"key_source.vault_token_file": s.VaultTokenFile,
			"key_source.vault_mount":      s.VaultMount,
			"key_source.vault_path":       s.VaultPath,
			"key_source.vault_field":      s.VaultField,
		}
		for key, value := range required {
			if strings.TrimSpace(value) == "" {
				return fmt.Errorf("%s is required for vault", key)
			}
		}
	default:
		return fmt.Errorf("key_source.type must be local_file, kms, or vault")
	}
	return nil
}

func (s ProviderKeySource) ReadLocalFileKey() (string, error) {
	if s.Type != KeySourceLocalFile {
		return "", fmt.Errorf("key_source.type %q does not read from local file", s.Type)
	}
	body, err := os.ReadFile(s.Path)
	if err != nil {
		return "", fmt.Errorf("read provider key file: %w", err)
	}
	key := strings.TrimSpace(string(body))
	if key == "" {
		return "", fmt.Errorf("provider key file is empty")
	}
	return key, nil
}

func validateHTTPURL(field string, value string) error {
	if value == "" {
		return fmt.Errorf("%s is required", field)
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Host == "" {
		return fmt.Errorf("%s must be an absolute http(s) URL", field)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return fmt.Errorf("%s must use http or https", field)
	}
	return nil
}
