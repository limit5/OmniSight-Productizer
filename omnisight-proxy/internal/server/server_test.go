package server

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/omnisight/productizer/omnisight-proxy/internal/config"
)

func TestHealthzReturnsOK(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()

	NewHandler(config.ForTest()).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}
	var body map[string]string
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body["status"] != "ok" {
		t.Fatalf("status body = %q, want ok", body["status"])
	}
	if body["service"] != "omnisight-proxy" {
		t.Fatalf("service body = %q, want omnisight-proxy", body["service"])
	}
}

func TestHealthzRejectsNonGet(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/healthz", nil)
	rec := httptest.NewRecorder()

	NewHandler(config.ForTest()).ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusMethodNotAllowed)
	}
	if rec.Header().Get("Allow") != http.MethodGet {
		t.Fatalf("Allow = %q, want GET", rec.Header().Get("Allow"))
	}
}

func TestAuthVerifyIsAvailableWhenAuthDisabled(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/auth/verify", nil)
	rec := httptest.NewRecorder()

	NewHandler(config.ForTest()).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}
}

func TestAuthVerifyRejectsNonGet(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/auth/verify", nil)
	rec := httptest.NewRecorder()

	NewHandler(config.ForTest()).ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusMethodNotAllowed)
	}
	if rec.Header().Get("Allow") != http.MethodGet {
		t.Fatalf("Allow = %q, want GET", rec.Header().Get("Allow"))
	}
}

func TestLLMForwarderStreamsRequestToConfiguredProvider(t *testing.T) {
	var gotMethod string
	var gotPath string
	var gotQuery string
	var gotAuthorization string
	var gotTenantHeader string
	var gotBody string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotAuthorization = r.Header.Get("Authorization")
		gotTenantHeader = r.Header.Get("X-Omnisight-Tenant-Id")
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("read upstream body: %v", err)
		}
		gotBody = string(body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	req := httptest.NewRequest(http.MethodPost, "/v1/llm/openai/v1/chat/completions?stream=true", strings.NewReader(`{"model":"gpt"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Omnisight-Tenant-Id", "tenant-a")
	rec := httptest.NewRecorder()

	NewHandler(forwardingConfig(t, upstream.URL)).ServeHTTP(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want %d; body=%s", rec.Code, http.StatusCreated, rec.Body.String())
	}
	if gotMethod != http.MethodPost {
		t.Fatalf("method = %q, want POST", gotMethod)
	}
	if gotPath != "/v1/chat/completions" {
		t.Fatalf("path = %q, want /v1/chat/completions", gotPath)
	}
	if gotQuery != "stream=true" {
		t.Fatalf("query = %q, want stream=true", gotQuery)
	}
	if gotAuthorization != "Bearer sk-local-test" {
		t.Fatalf("Authorization = %q, want provider bearer token", gotAuthorization)
	}
	if gotTenantHeader != "" {
		t.Fatalf("tenant header leaked upstream: %q", gotTenantHeader)
	}
	if gotBody != `{"model":"gpt"}` {
		t.Fatalf("body = %q, want raw request body", gotBody)
	}
	if rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json", rec.Header().Get("Content-Type"))
	}
}

func TestLLMForwarderRejectsMissingProviderCatalog(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/llm/openai/v1/chat/completions", strings.NewReader("{}"))
	rec := httptest.NewRecorder()

	NewHandler(config.ForTest()).ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusServiceUnavailable)
	}
}

func TestLLMForwarderRejectsNonLocalKeySource(t *testing.T) {
	cfg := config.ForTest()
	cfg.ProviderCatalog = &config.ProviderCatalog{
		Providers: []config.ProviderConfig{
			{
				Name:    "openai",
				BaseURL: "https://api.openai.com",
				KeySource: config.ProviderKeySource{
					Type:              config.KeySourceKMS,
					KMSProvider:       "aws",
					KMSKeyID:          "arn:aws:kms:us-east-1:111122223333:key/example",
					KMSCiphertextFile: "/run/secrets/openai_api_key.kms",
				},
			},
		},
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/llm/openai/v1/chat/completions", strings.NewReader("{}"))
	rec := httptest.NewRecorder()

	NewHandler(cfg).ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusServiceUnavailable)
	}
}

func TestLLMForwarderStreamsResponseWithoutBufferingPayload(t *testing.T) {
	secondChunk := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprintln(w, "data: first")
		w.(http.Flusher).Flush()
		<-secondChunk
		_, _ = fmt.Fprintln(w, "data: second")
	}))
	defer upstream.Close()

	proxy := httptest.NewServer(NewHandler(forwardingConfig(t, upstream.URL)))
	defer proxy.Close()

	req, err := http.NewRequest(http.MethodPost, proxy.URL+"/v1/llm/openai/v1/chat/completions", strings.NewReader("{}"))
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	resp, err := proxy.Client().Do(req)
	if err != nil {
		t.Fatalf("proxy request: %v", err)
	}
	defer resp.Body.Close()
	defer close(secondChunk)

	firstLine := make(chan string, 1)
	readErr := make(chan error, 1)
	go func() {
		line, err := bufio.NewReader(resp.Body).ReadString('\n')
		if err != nil {
			readErr <- err
			return
		}
		firstLine <- line
	}()

	select {
	case line := <-firstLine:
		if line != "data: first\n" {
			t.Fatalf("first streamed line = %q, want data: first", line)
		}
	case err := <-readErr:
		t.Fatalf("read first line: %v", err)
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for first streamed line before upstream completed")
	}

}

func TestHeartbeatClientPostsHealthPayload(t *testing.T) {
	got := make(chan map[string]any, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %q, want POST", r.Method)
		}
		if r.Header.Get("Content-Type") != "application/json" {
			t.Fatalf("Content-Type = %q, want application/json", r.Header.Get("Content-Type"))
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode heartbeat body: %v", err)
		}
		got <- body
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	cfg := forwardingConfig(t, "https://api.openai.test")
	cfg.ProxyID = "proxy-a"
	cfg.TenantID = "tenant-a"
	cfg.SaaSHeartbeatURL = upstream.URL
	cfg.HeartbeatIntervalSeconds = 30

	if err := newHeartbeatClient(cfg).post(context.Background()); err != nil {
		t.Fatalf("post heartbeat: %v", err)
	}

	select {
	case body := <-got:
		if body["proxy_id"] != "proxy-a" {
			t.Fatalf("proxy_id = %v, want proxy-a", body["proxy_id"])
		}
		if body["tenant_id"] != "tenant-a" {
			t.Fatalf("tenant_id = %v, want tenant-a", body["tenant_id"])
		}
		if body["status"] != "ok" {
			t.Fatalf("status = %v, want ok", body["status"])
		}
		if body["service"] != "omnisight-proxy" {
			t.Fatalf("service = %v, want omnisight-proxy", body["service"])
		}
		if body["provider_count"] != float64(1) {
			t.Fatalf("provider_count = %v, want 1", body["provider_count"])
		}
		if body["heartbeat_interval_seconds"] != float64(30) {
			t.Fatalf("heartbeat_interval_seconds = %v, want 30", body["heartbeat_interval_seconds"])
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for heartbeat post")
	}
}

func TestStartHeartbeatLoopPostsImmediately(t *testing.T) {
	got := make(chan struct{}, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- struct{}{}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	cfg := config.ForTest()
	cfg.ProxyID = "proxy-a"
	cfg.SaaSHeartbeatURL = upstream.URL
	cfg.HeartbeatIntervalSeconds = 30
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	StartHeartbeatLoop(ctx, cfg, nilLogger())

	select {
	case <-got:
	case <-time.After(time.Second):
		t.Fatal("heartbeat loop did not post immediately")
	}
}

func forwardingConfig(t *testing.T, baseURL string) *config.Settings {
	t.Helper()
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "provider.key")
	if err := os.WriteFile(keyFile, []byte("sk-local-test\n"), 0o600); err != nil {
		t.Fatalf("write provider key: %v", err)
	}
	cfg := config.ForTest()
	cfg.ProviderCatalog = &config.ProviderCatalog{
		Providers: []config.ProviderConfig{
			{
				Name:    "openai",
				BaseURL: baseURL,
				KeySource: config.ProviderKeySource{
					Type: config.KeySourceLocalFile,
					Path: keyFile,
				},
			},
		},
	}
	return cfg
}

func nilLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}
