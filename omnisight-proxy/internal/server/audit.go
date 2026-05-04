package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"
)

type proxyAuditSink struct {
	path string
	mu   sync.Mutex
}

type proxyAuditRecord struct {
	RecordedAt string `json:"recorded_at"`
	ProxyID    string `json:"proxy_id,omitempty"`
	TenantID   string `json:"tenant_id,omitempty"`
	Provider   string `json:"provider"`
	Method     string `json:"method"`
	Path       string `json:"path"`
	StatusCode int    `json:"status_code"`
	Model      string `json:"model,omitempty"`
	TokenCount int    `json:"token_count"`
	Prompt     string `json:"prompt"`
	Response   string `json:"response"`
}

type proxyAuditMetadata struct {
	RecordedAt       string `json:"recorded_at"`
	ProxyID          string `json:"proxy_id,omitempty"`
	TenantID         string `json:"tenant_id,omitempty"`
	Provider         string `json:"provider"`
	Method           string `json:"method"`
	Path             string `json:"path"`
	StatusCode       int    `json:"status_code"`
	Model            string `json:"model,omitempty"`
	TokenCount       int    `json:"token_count"`
	PromptTokens     int    `json:"prompt_tokens"`
	CompletionTokens int    `json:"completion_tokens"`
	TotalTokens      int    `json:"total_tokens"`
}

type saasAuditClient struct {
	url    string
	client *http.Client
}

type usageMetadata struct {
	model            string
	promptTokens     int
	completionTokens int
	totalTokens      int
}

func newProxyAuditSink(path string) *proxyAuditSink {
	if path == "" {
		return nil
	}
	return &proxyAuditSink{path: path}
}

func (s *proxyAuditSink) write(record proxyAuditRecord) error {
	if s == nil {
		return nil
	}
	line, err := json.Marshal(record)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	file, err := os.OpenFile(s.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.Write(append(line, '\n')); err != nil {
		return err
	}
	return nil
}

func newSaaSAuditClient(url string) *saasAuditClient {
	if url == "" {
		return nil
	}
	return &saasAuditClient{
		url: url,
		client: &http.Client{
			Timeout: 5 * time.Second,
		},
	}
}

func (c *saasAuditClient) post(ctx context.Context, metadata proxyAuditMetadata) error {
	if c == nil {
		return nil
	}
	body, err := json.Marshal(metadata)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func extractUsageMetadata(requestBody []byte, responseBody []byte) usageMetadata {
	metadata := usageMetadata{
		model: extractJSONText(requestBody, "model"),
	}
	if responseModel := extractJSONText(responseBody, "model"); responseModel != "" {
		metadata.model = responseModel
	}
	metadata.promptTokens = extractNestedJSONInt(responseBody, "usage", "prompt_tokens")
	metadata.completionTokens = extractNestedJSONInt(responseBody, "usage", "completion_tokens")
	metadata.totalTokens = extractNestedJSONInt(responseBody, "usage", "total_tokens")
	if metadata.totalTokens == 0 {
		metadata.totalTokens = metadata.promptTokens + metadata.completionTokens
	}
	return metadata
}

func extractJSONText(raw []byte, key string) string {
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return ""
	}
	value, ok := data[key].(string)
	if !ok {
		return ""
	}
	return value
}

func extractNestedJSONInt(raw []byte, objectKey string, intKey string) int {
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return 0
	}
	nested, ok := data[objectKey].(map[string]any)
	if !ok {
		return 0
	}
	switch value := nested[intKey].(type) {
	case float64:
		return int(value)
	case int:
		return value
	default:
		return 0
	}
}
