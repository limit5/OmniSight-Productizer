package server

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/omnisight/productizer/omnisight-proxy/internal/auth"
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

func TestLLMForwarderWritesCustomerAuditAndPostsSaaSMetadataOnly(t *testing.T) {
	var saasMetadata map[string]any
	saas := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&saasMetadata); err != nil {
			t.Fatalf("decode SaaS metadata: %v", err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer saas.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"model":"gpt-4.1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":5,"total_tokens":12}}`))
	}))
	defer upstream.Close()

	auditPath := filepath.Join(t.TempDir(), "proxy-audit.jsonl")
	cfg := forwardingConfig(t, upstream.URL)
	cfg.ProxyID = "proxy-a"
	cfg.TenantID = "tenant-a"
	cfg.CustomerAuditLogFile = auditPath
	cfg.SaaSAuditURL = saas.URL
	req := httptest.NewRequest(http.MethodPost, "/v1/llm/openai/v1/chat/completions", strings.NewReader(`{"model":"gpt-4.1","messages":[{"role":"user","content":"customer secret prompt"}]}`))
	rec := httptest.NewRecorder()

	NewHandler(cfg).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body=%s", rec.Code, http.StatusOK, rec.Body.String())
	}
	rawAudit, err := os.ReadFile(auditPath)
	if err != nil {
		t.Fatalf("read audit log: %v", err)
	}
	var auditRecord map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(rawAudit), &auditRecord); err != nil {
		t.Fatalf("decode audit log: %v", err)
	}
	if !strings.Contains(auditRecord["prompt"].(string), "customer secret prompt") {
		t.Fatalf("customer audit prompt missing full request: %v", auditRecord["prompt"])
	}
	if !strings.Contains(auditRecord["response"].(string), `"total_tokens":12`) {
		t.Fatalf("customer audit response missing full response: %v", auditRecord["response"])
	}
	if auditRecord["token_count"] != float64(12) {
		t.Fatalf("customer audit token_count = %v, want 12", auditRecord["token_count"])
	}
	if saasMetadata["proxy_id"] != "proxy-a" {
		t.Fatalf("SaaS proxy_id = %v, want proxy-a", saasMetadata["proxy_id"])
	}
	if saasMetadata["model"] != "gpt-4.1" {
		t.Fatalf("SaaS model = %v, want gpt-4.1", saasMetadata["model"])
	}
	if saasMetadata["token_count"] != float64(12) {
		t.Fatalf("SaaS token_count = %v, want 12", saasMetadata["token_count"])
	}
	if _, ok := saasMetadata["prompt"]; ok {
		t.Fatal("SaaS metadata leaked prompt")
	}
	if _, ok := saasMetadata["response"]; ok {
		t.Fatal("SaaS metadata leaked response")
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

func TestKS314LLMForwarderStreamsResponseWithoutBufferingPayload(t *testing.T) {
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

func TestKS314LLMForwarderProxyHopP95UnderLatencyBudgetWithMTLSReuse(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	ca := newProxyCertificate(t, nil, "ca", x509.ExtKeyUsageAny, now.Add(-time.Hour), now.Add(time.Hour))
	serverCert := newProxyCertificate(t, &ca, "127.0.0.1", x509.ExtKeyUsageServerAuth, now.Add(-time.Hour), now.Add(time.Hour))
	clientCert := newProxyCertificate(t, &ca, "client", x509.ExtKeyUsageClientAuth, now.Add(-time.Hour), now.Add(time.Hour))

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	cfg := authForwardingConfig(t, upstream.URL, ca.certPEM, auth.CertificateFingerprint(clientCert.leaf))
	tlsConfig, err := auth.ServerTLSConfig(cfg)
	if err != nil {
		t.Fatalf("ServerTLSConfig: %v", err)
	}
	tlsConfig.Certificates = []tls.Certificate{serverCert.tlsCert}
	proxy := httptest.NewUnstartedServer(NewHandler(cfg))
	proxy.TLS = tlsConfig
	proxy.StartTLS()
	defer proxy.Close()

	roots := x509.NewCertPool()
	roots.AddCert(ca.leaf)
	client := &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        1,
			MaxIdleConnsPerHost: 1,
			TLSClientConfig: &tls.Config{
				MinVersion:   tls.VersionTLS12,
				RootCAs:      roots,
				Certificates: []tls.Certificate{clientCert.tlsCert},
			},
		},
		Timeout: 5 * time.Second,
	}

	if _, err := signedProxyRequest(t, client, proxy.URL, "warmup", now); err != nil {
		t.Fatalf("warmup request failed: %v", err)
	}

	const sampleCount = 64
	const budget = 50 * time.Millisecond
	latencies := make([]time.Duration, 0, sampleCount)
	for i := range sampleCount {
		nonce := fmt.Sprintf("latency-%02d", i)
		reused := false
		start := time.Now()
		if _, err := signedProxyRequestWithTrace(t, client, proxy.URL, nonce, now, func(info httptrace.GotConnInfo) {
			reused = info.Reused
		}); err != nil {
			t.Fatalf("latency request %d failed: %v", i, err)
		}
		if !reused {
			t.Fatalf("latency request %d did not reuse the mTLS connection", i)
		}
		latencies = append(latencies, time.Since(start))
	}
	sort.Slice(latencies, func(i, j int) bool { return latencies[i] < latencies[j] })
	p95 := latencies[int(math.Ceil(float64(len(latencies))*0.95))-1]
	if p95 >= budget {
		t.Fatalf("proxy hop p95 = %s, want < %s with mTLS connection reuse", p95, budget)
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

func authForwardingConfig(t *testing.T, baseURL string, caPEM []byte, pinnedFingerprint string) *config.Settings {
	t.Helper()
	cfg := forwardingConfig(t, baseURL)
	dir := t.TempDir()
	caPath := filepath.Join(dir, "ca.crt")
	keyPath := filepath.Join(dir, "nonce.key")
	if err := os.WriteFile(caPath, caPEM, 0o600); err != nil {
		t.Fatalf("write ca: %v", err)
	}
	if err := os.WriteFile(keyPath, proxyNonceKey(), 0o600); err != nil {
		t.Fatalf("write nonce key: %v", err)
	}
	cfg.AuthEnabled = true
	cfg.TenantID = "tenant-a"
	cfg.ClientCAFile = caPath
	cfg.PinnedClientCertSHA256 = pinnedFingerprint
	cfg.NonceHMACKeyFile = keyPath
	return cfg
}

func signedProxyRequest(t *testing.T, client *http.Client, baseURL string, nonce string, now time.Time) (*http.Response, error) {
	t.Helper()
	return signedProxyRequestWithTrace(t, client, baseURL, nonce, now, nil)
}

func signedProxyRequestWithTrace(
	t *testing.T,
	client *http.Client,
	baseURL string,
	nonce string,
	now time.Time,
	gotConn func(httptrace.GotConnInfo),
) (*http.Response, error) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, baseURL+"/v1/llm/openai/v1/chat/completions", strings.NewReader("{}"))
	if err != nil {
		t.Fatalf("new proxy request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(auth.HeaderTenantID, "tenant-a")
	req.Header.Set(auth.HeaderNonce, nonce)
	req.Header.Set(auth.HeaderTimestamp, strconv.FormatInt(now.Unix(), 10))
	req.Header.Set(auth.HeaderSignature, auth.Sign(proxyNonceKey(), req.Method, req.URL.RequestURI(), "tenant-a", nonce, now.Unix()))
	if gotConn != nil {
		trace := &httptrace.ClientTrace{GotConn: gotConn}
		req = req.WithContext(httptrace.WithClientTrace(req.Context(), trace))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status = %d", resp.StatusCode)
	}
	return resp, nil
}

func proxyNonceKey() []byte {
	return []byte("0123456789abcdef0123456789abcdef")
}

type proxyCertificate struct {
	tlsCert tls.Certificate
	leaf    *x509.Certificate
	certPEM []byte
	key     *ecdsa.PrivateKey
}

func newProxyCertificate(t *testing.T, ca *proxyCertificate, commonName string, usage x509.ExtKeyUsage, notBefore time.Time, notAfter time.Time) proxyCertificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	serialLimit := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, serialLimit)
	if err != nil {
		t.Fatalf("generate serial: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName: commonName,
		},
		NotBefore: notBefore,
		NotAfter:  notAfter,
		KeyUsage:  x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{
			usage,
		},
	}
	parent := template
	signer := key
	if ca == nil {
		template.IsCA = true
		template.KeyUsage |= x509.KeyUsageCertSign
		template.BasicConstraintsValid = true
	} else {
		parent = ca.leaf
		signer = ca.key
	}
	if usage == x509.ExtKeyUsageServerAuth {
		template.IPAddresses = []net.IP{net.ParseIP("127.0.0.1")}
	}
	raw, err := x509.CreateCertificate(rand.Reader, template, parent, &key.PublicKey, signer)
	if err != nil {
		t.Fatalf("create cert: %v", err)
	}
	leaf, err := x509.ParseCertificate(raw)
	if err != nil {
		t.Fatalf("parse cert: %v", err)
	}
	keyBytes, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: raw})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyBytes})
	tlsCert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatalf("tls key pair: %v", err)
	}
	tlsCert.Leaf = leaf
	return proxyCertificate{
		tlsCert: tlsCert,
		leaf:    leaf,
		certPEM: certPEM,
		key:     key,
	}
}

func nilLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}
