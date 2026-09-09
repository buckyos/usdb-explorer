package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func request(method string, params string) string {
	return `{"jsonrpc":"2.0","id":1,"method":"` + method + `","params":` + params + `}`
}

func TestPolicyRejectsDangerousAndAmbiguousRequests(t *testing.T) {
	for _, body := range []string{
		request("admin_addPeer", "[]"), request("miner_stop", "[]"), request("debug_traceTransaction", "[]"),
		request("eth_sendTransaction", "[]"), request("eth_sign", "[]"),
		`{"jsonrpc":"2.0","id":1,"method":"eth_chainId","method":"miner_stop","params":[]}`,
		`{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[],"params":[]}`,
		request("eth_chainId", "[]") + ` {}`, `[]`, `null`, request("eth_getLogs", `[{}]`),
		request("eth_getLogs", `[{"fromBlock":"0x0","toBlock":"0x3e8"}]`),
		request("eth_getLogs", `[{"fromBlock":"0x10","toBlock":"0x0"}]`),
		request("eth_getLogs", `[{"fromBlock":"0x10000","FromBlock":"0x0","toBlock":"0x10001"}]`),
		request("eth_feeHistory", `["0x65","latest",[]]`),
	} {
		if _, err := validate([]byte(body)); err == nil {
			t.Fatalf("unsafe request accepted: %s", body)
		}
	}
	for _, body := range []string{request("eth_chainId", "[]"), request("eth_sendRawTransaction", `["0x01"]`), request("eth_getLogs", `[{"fromBlock":"0x0","toBlock":"0x3e7"}]`)} {
		if _, err := validate([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestRejectedBatchNeverForwardsEvenAllowedTransaction(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); _, _ = w.Write([]byte(`{}`)) }))
	defer upstream.Close()
	g, _ := newGateway(upstream.URL, "0x1", "0x"+strings.Repeat("a", 64))
	body := `[` + request("eth_sendRawTransaction", `["0x01"]`) + `,` + request("miner_stop", "[]") + `]`
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/", strings.NewReader(body)))
	if w.Code != 400 || calls.Load() != 0 {
		t.Fatalf("batch forwarded: status=%d calls=%d", w.Code, calls.Load())
	}
}

func TestIdentityGateAndSignedBroadcastPassThrough(t *testing.T) {
	genesis := "0x" + strings.Repeat("a", 64)
	var wrong atomic.Bool
	var submissions atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		var result any
		switch req.Method {
		case "eth_chainId":
			result = "0x1"
			if wrong.Load() {
				result = "0x2"
			}
		case "eth_getBlockByNumber":
			result = map[string]string{"hash": genesis}
		case "eth_sendRawTransaction":
			submissions.Add(1)
			result = "0x" + strings.Repeat("b", 64)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": 1, "result": result})
	}))
	defer upstream.Close()
	g, _ := newGateway(upstream.URL, "0x1", genesis)
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/", strings.NewReader(request("eth_sendRawTransaction", `["0x01"]`))))
	if w.Code != 200 || submissions.Load() != 1 {
		t.Fatalf("valid broadcast failed: %s", w.Body)
	}
	wrong.Store(true)
	w = httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/", strings.NewReader(request("eth_sendRawTransaction", `["0x01"]`))))
	if w.Code != 503 || submissions.Load() != 1 {
		t.Fatal("wrong-chain broadcast reached upstream")
	}
}

func TestLimitsAndUntrustedForwardedHeader(t *testing.T) {
	g, _ := newGateway("http://127.0.0.1:1", "0x1", "0x"+strings.Repeat("a", 64))
	for i := 0; i < 40; i++ {
		if !g.allow("192.0.2.1", 1) {
			t.Fatal("early limit")
		}
	}
	req := httptest.NewRequest("POST", "/", strings.NewReader(request("eth_chainId", "[]")))
	req.RemoteAddr = "192.0.2.1:5000"
	req.Header.Set("X-Forwarded-For", "198.51.100.2")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, req)
	if w.Code != 429 {
		t.Fatal("forwarded header bypassed quota")
	}
	req = httptest.NewRequest("POST", "/", strings.NewReader(strings.Repeat(" ", maxBody+1)))
	req.RemoteAddr = "192.0.2.2:5000"
	w = httptest.NewRecorder()
	g.ServeHTTP(w, req)
	if w.Code != 413 {
		t.Fatal("body limit missing")
	}
	for i := 0; i < cap(g.active); i++ {
		g.active <- struct{}{}
	}
	req = httptest.NewRequest("POST", "/", strings.NewReader(request("eth_chainId", "[]")))
	w = httptest.NewRecorder()
	g.ServeHTTP(w, req)
	if w.Code != 503 {
		t.Fatal("concurrency limit missing")
	}
}

func TestRedirectCannotChangeUpstream(t *testing.T) {
	var reached atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached.Store(true) }))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, 307) }))
	defer redirect.Close()
	g, _ := newGateway(redirect.URL, "0x1", "0x"+strings.Repeat("a", 64))
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/", strings.NewReader(request("eth_chainId", "[]"))))
	if w.Code != 503 || reached.Load() {
		t.Fatal("redirect followed")
	}
}

func TestSeparateWriterPreservesReadRoutingAndRejectsWrongNetwork(t *testing.T) {
	genesis := "0x" + strings.Repeat("a", 64)
	var wrong atomic.Bool
	var reads, writes atomic.Int32
	handler := func(writer bool) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			var req rpcRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			var result any
			switch req.Method {
			case "eth_chainId":
				result = "0x1"
				if writer && wrong.Load() {
					result = "0x2"
				}
			case "eth_getBlockByNumber":
				result = map[string]string{"hash": genesis}
			case "eth_getBalance":
				if writer {
					t.Error("historical read sent to writer")
				}
				reads.Add(1)
				result = "0x7"
			case "eth_sendRawTransaction":
				if !writer {
					t.Error("transaction sent to archive")
				}
				writes.Add(1)
				result = "0x" + strings.Repeat("b", 64)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": result})
		}
	}
	reader := httptest.NewServer(handler(false))
	defer reader.Close()
	writer := httptest.NewServer(handler(true))
	defer writer.Close()
	g, _ := newGateway(reader.URL, "0x1", genesis)
	g.broadcast, _ = newGateway(writer.URL, "0x1", genesis)
	body := `[` + request("eth_getBalance", `["0x0","0x1"]`) + `,` + request("eth_sendRawTransaction", `["0x01"]`) + `]`
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/", strings.NewReader(body)))
	if w.Code != 200 || reads.Load() != 1 || writes.Load() != 1 {
		t.Fatal(w.Code, w.Body)
	}
	var results []map[string]any
	if json.Unmarshal(w.Body.Bytes(), &results) != nil || len(results) != 2 || results[0]["result"] != "0x7" {
		t.Fatal(w.Body)
	}
	wrong.Store(true)
	w = httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/", strings.NewReader(body)))
	if w.Code != 503 || reads.Load() != 1 || writes.Load() != 1 {
		t.Fatal("wrong writer caused partial batch effects")
	}
}

func TestTLSUpstreamAndWalletMetadataWithoutNginx(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":"0x1"}`))
	}))
	defer upstream.Close()
	g, err := newGateway(upstream.URL, "0x1", "0x"+strings.Repeat("a", 64))
	if err != nil {
		t.Fatal(err)
	}
	g.client = upstream.Client()
	if _, err = g.forward(t.Context(), []byte(request("eth_chainId", "[]"))); err != nil {
		t.Fatal(err)
	}
	g.network = []byte(`{"rpcUrls":["https://explorer.example.com/rpc"]}`)
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("GET", "/network.json", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), "https://explorer.example.com/rpc") {
		t.Fatal(w.Code, w.Body)
	}
}
