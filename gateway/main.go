// Command public-rpc-gateway exposes a bounded subset of an isolated archive RPC.
package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const maxBody = 256 << 10
const maxResponse = 8 << 20
const maxBatch = 20
const maxLogRange = 1000

var methods = map[string]bool{}

func init() {
	for _, name := range strings.Fields(`web3_clientVersion web3_sha3 net_version net_listening net_peerCount eth_chainId eth_syncing eth_blockNumber eth_getBlockByHash eth_getBlockByNumber eth_getBlockTransactionCountByHash eth_getBlockTransactionCountByNumber eth_getTransactionByHash eth_getTransactionByBlockHashAndIndex eth_getTransactionByBlockNumberAndIndex eth_getTransactionReceipt eth_getBalance eth_getCode eth_getStorageAt eth_getTransactionCount eth_call eth_estimateGas eth_gasPrice eth_maxPriorityFeePerGas eth_feeHistory eth_getLogs eth_sendRawTransaction`) {
		methods[name] = true
	}
}

type rpcRequest struct {
	Version string            `json:"jsonrpc"`
	ID      json.RawMessage   `json:"id"`
	Method  string            `json:"method"`
	Params  []json.RawMessage `json:"params"`
}

// strictJSON rejects duplicate keys before decoding: policy and upstream must see the same request.
func strictJSON(data []byte) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	var walk func(int) error
	walk = func(depth int) error {
		if depth > 32 {
			return errors.New("JSON nesting limit exceeded")
		}
		t, err := d.Token()
		if err != nil {
			return err
		}
		if delim, ok := t.(json.Delim); ok {
			switch delim {
			case '{':
				seen := map[string]bool{}
				for d.More() {
					key, err := d.Token()
					if err != nil {
						return err
					}
					name, ok := key.(string)
					name = strings.ToLower(name)
					if !ok || seen[name] {
						return errors.New("duplicate JSON key")
					}
					seen[name] = true
					if err := walk(depth + 1); err != nil {
						return err
					}
				}
			case '[':
				for d.More() {
					if err := walk(depth + 1); err != nil {
						return err
					}
				}
			default:
				return errors.New("invalid JSON delimiter")
			}
			_, err = d.Token()
			return err
		}
		return nil
	}
	if err := walk(0); err != nil {
		return err
	}
	if _, err := d.Token(); err != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}

func hexNumber(v json.RawMessage) (uint64, error) {
	var s string
	if json.Unmarshal(v, &s) != nil || !strings.HasPrefix(s, "0x") || len(s) < 3 || (len(s) > 3 && s[2] == '0') {
		return 0, errors.New("explicit hex block quantity required")
	}
	return strconv.ParseUint(s[2:], 16, 64)
}

// validate rejects the entire batch before any member, including a signed transaction, is forwarded.
func validate(body []byte) ([]rpcRequest, error) {
	if err := strictJSON(body); err != nil {
		return nil, err
	}
	trim := bytes.TrimSpace(body)
	var items []json.RawMessage
	if len(trim) > 0 && trim[0] == '[' {
		_ = json.Unmarshal(trim, &items)
	} else {
		items = append(items, trim)
	}
	if len(items) == 0 || len(items) > maxBatch {
		return nil, errors.New("batch size must be 1..20")
	}
	requests := make([]rpcRequest, 0, len(items))
	for _, item := range items {
		var r rpcRequest
		d := json.NewDecoder(bytes.NewReader(item))
		d.DisallowUnknownFields()
		if d.Decode(&r) != nil || r.Version != "2.0" || len(r.ID) == 0 || bytes.Equal(r.ID, []byte("null")) {
			return nil, errors.New("JSON-RPC 2.0 with a request ID required")
		}
		var id any
		_ = json.Unmarshal(r.ID, &id)
		switch id.(type) {
		case string, float64:
		default:
			return nil, errors.New("invalid request ID")
		}
		if !methods[r.Method] {
			return nil, errors.New("RPC method is not allowed")
		}
		if r.Method == "eth_getLogs" {
			if len(r.Params) != 1 {
				return nil, errors.New("one log filter required")
			}
			var filter map[string]json.RawMessage
			if json.Unmarshal(r.Params[0], &filter) != nil || filter == nil {
				return nil, errors.New("invalid log filter")
			}
			for name := range filter {
				switch name {
				case "fromBlock", "toBlock", "blockHash", "address", "topics":
				default:
					return nil, errors.New("unknown log filter field")
				}
			}
			if hash, ok := filter["blockHash"]; ok {
				var s string
				if json.Unmarshal(hash, &s) != nil || len(s) != 66 || !strings.HasPrefix(s, "0x") || filter["fromBlock"] != nil || filter["toBlock"] != nil {
					return nil, errors.New("invalid blockHash filter")
				}
				if _, err := hex.DecodeString(s[2:]); err != nil {
					return nil, errors.New("invalid blockHash filter")
				}
			} else {
				from, e1 := hexNumber(filter["fromBlock"])
				to, e2 := hexNumber(filter["toBlock"])
				if e1 != nil || e2 != nil || to < from || to-from >= maxLogRange {
					return nil, errors.New("logs require explicit fromBlock/toBlock spanning at most 1000 blocks")
				}
			}
		}
		if r.Method == "eth_feeHistory" {
			if len(r.Params) < 2 {
				return nil, errors.New("invalid fee history parameters")
			}
			count, err := hexNumber(r.Params[0])
			if err != nil || count == 0 || count > 100 {
				return nil, errors.New("fee history limit is 100 blocks")
			}
		}
		requests = append(requests, r)
	}
	return requests, nil
}

type bucket struct {
	tokens  float64
	updated time.Time
}
type gateway struct {
	broadcast                  *gateway
	network                    []byte
	explorerURL                string
	upstream, chainID, genesis string
	client                     *http.Client
	active                     chan struct{}
	mu                         sync.Mutex
	buckets                    map[string]bucket
	checked                    time.Time
	identityOK                 bool
}

func newGateway(upstream, chainID, genesis string) (*gateway, error) {
	u, err := url.Parse(upstream)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("HTTP(S) upstream without credentials required")
	}
	return &gateway{upstream: upstream, chainID: chainID, genesis: genesis, active: make(chan struct{}, 8), buckets: map[string]bucket{},
		client: &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{MaxConnsPerHost: 8, MaxIdleConnsPerHost: 8, ResponseHeaderTimeout: 8 * time.Second}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (g *gateway) forward(ctx context.Context, body []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "POST", g.upstream, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := g.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return nil, errors.New("upstream HTTP error")
	}
	result, err := io.ReadAll(io.LimitReader(res.Body, maxResponse+1))
	if err != nil || len(result) > maxResponse || !json.Valid(result) {
		return nil, errors.New("invalid or oversized upstream response")
	}
	return result, nil
}

func (g *gateway) identity(ctx context.Context, force bool) bool {
	// Serialize and briefly cache the read-only identity check; never cache a failure as success.
	g.mu.Lock()
	defer g.mu.Unlock()
	if !force && time.Since(g.checked) < 5*time.Second {
		return g.identityOK
	}
	g.identityOK = false
	for method, expected := range map[string]string{"eth_chainId": g.chainID, "eth_getBlockByNumber": g.genesis} {
		params := []any{}
		if method == "eth_getBlockByNumber" {
			params = []any{"0x0", false}
		}
		body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
		result, err := g.forward(ctx, body)
		if err != nil {
			g.checked = time.Now()
			return false
		}
		var v struct {
			Result json.RawMessage `json:"result"`
		}
		if json.Unmarshal(result, &v) != nil {
			g.checked = time.Now()
			return false
		}
		var actual string
		if method == "eth_chainId" {
			_ = json.Unmarshal(v.Result, &actual)
		} else {
			var b struct {
				Hash string `json:"hash"`
			}
			_ = json.Unmarshal(v.Result, &b)
			actual = b.Hash
		}
		if !strings.EqualFold(actual, expected) {
			g.checked = time.Now()
			return false
		}
	}
	g.checked = time.Now()
	g.identityOK = true
	return true
}

func (g *gateway) allow(ip string, cost int) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	now := time.Now()
	b, exists := g.buckets[ip]
	if !exists {
		if len(g.buckets) >= 8192 {
			for key, value := range g.buckets {
				if now.Sub(value.updated) > time.Minute {
					delete(g.buckets, key)
				}
			}
			if len(g.buckets) >= 8192 {
				return false
			}
		}
		b = bucket{40, now}
	}
	b.tokens = min(40, b.tokens+now.Sub(b.updated).Seconds()*10)
	b.updated = now
	ok := b.tokens >= float64(cost)
	if ok {
		b.tokens -= float64(cost)
	}
	g.buckets[ip] = b
	return ok
}

func rpcError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": nil, "error": map[string]any{"code": -32000, "message": message}})
}

func (g *gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.URL.Path == "/network.json" && r.Method == http.MethodGet && len(g.network) != 0 {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(g.network)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/v2/") {
		g.explorer(w, r)
		return
	}
	if r.URL.Path != "/" && r.URL.Path != "/healthz" {
		http.NotFound(w, r)
		return
	}
	if r.Method == "OPTIONS" {
		w.WriteHeader(204)
		return
	}
	select {
	case g.active <- struct{}{}:
		defer func() { <-g.active }()
	default:
		rpcError(w, 503, "RPC concurrency limit reached")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 12*time.Second)
	defer cancel()
	if r.URL.Path == "/healthz" && r.Method == "GET" {
		if g.identity(ctx, false) {
			w.WriteHeader(200)
			_, _ = w.Write([]byte("ready\n"))
		} else {
			rpcError(w, 503, "upstream network identity unavailable")
		}
		return
	}
	if r.Method != "POST" {
		rpcError(w, 405, "POST required")
		return
	}
	ip, _, _ := net.SplitHostPort(r.RemoteAddr)
	// Forwarded headers are deliberately ignored. Behind a proxy this is a shared, conservative quota.
	if !g.allow(ip, 1) {
		rpcError(w, 429, "RPC rate limit reached")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		rpcError(w, 413, "RPC request body limit exceeded")
		return
	}
	requests, err := validate(body)
	if err != nil {
		rpcError(w, 400, err.Error())
		return
	}
	if len(requests) > 1 && !g.allow(ip, len(requests)-1) {
		rpcError(w, 429, "RPC rate limit reached")
		return
	}
	freshIdentity := false
	for _, request := range requests {
		if request.Method == "eth_sendRawTransaction" {
			freshIdentity = true
		}
	}
	if !g.identity(ctx, freshIdentity) {
		rpcError(w, 503, "upstream network identity unavailable")
		return
	}
	// Validate both destinations before forwarding any member of a mixed batch.
	// Historical reads retain their archive destination; only signed transactions use the writer.
	if freshIdentity && g.broadcast != nil {
		if !g.broadcast.identity(ctx, true) {
			rpcError(w, 503, "broadcast upstream network identity unavailable")
			return
		}
		results := make([]json.RawMessage, 0, len(requests))
		for _, request := range requests {
			target := g
			if request.Method == "eth_sendRawTransaction" {
				target = g.broadcast
			}
			payload, _ := json.Marshal(request)
			result, err := target.forward(ctx, payload)
			if err != nil {
				// Do not retry a possibly submitted transaction, or lose the other batch results.
				result, _ = json.Marshal(map[string]any{"jsonrpc": "2.0", "id": request.ID,
					"error": map[string]any{"code": -32000, "message": "upstream unavailable; submission may have succeeded, check transaction hash before retrying"}})
			}
			results = append(results, result)
		}
		w.Header().Set("Content-Type", "application/json")
		if bytes.TrimSpace(body)[0] == '[' {
			_ = json.NewEncoder(w).Encode(results)
		} else {
			_, _ = w.Write(results[0])
		}
		return
	}
	result, err := g.forward(ctx, body)
	if err != nil {
		rpcError(w, 502, "upstream RPC unavailable; transaction submission may have succeeded, check its hash before retrying")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(result)
}

func main() {
	g, err := newGateway(os.Getenv("RPC_UPSTREAM"), os.Getenv("CHAIN_ID_HEX"), os.Getenv("GENESIS_HASH"))
	if err != nil || g.chainID == "" || len(g.genesis) != 66 {
		log.Fatal("Invalid public RPC upstream or network identity configuration")
	}
	g.explorerURL = "http://backend:4000"
	if writer := os.Getenv("BROADCAST_UPSTREAM"); writer != "" && writer != g.upstream {
		g.broadcast, err = newGateway(writer, g.chainID, g.genesis)
		if err != nil {
			log.Fatal("Invalid broadcast upstream configuration")
		}
	}
	if file := os.Getenv("NETWORK_FILE"); file != "" {
		g.network, err = os.ReadFile(file)
		if err != nil || len(g.network) > maxBody || !json.Valid(g.network) {
			log.Fatal("Invalid wallet network configuration")
		}
	}
	s := &http.Server{Addr: ":8080", Handler: g, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 20 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = s.Shutdown(shutdown)
	}()
	fmt.Println("Public RPC gateway started: bounded methods, configured upstreams, identity gate enabled")
	if err := s.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
