package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const fixturePass = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaai0"

func publicFixture(t *testing.T, mutate func(string, []json.RawMessage, map[string]any)) *gateway {
	t.Helper()
	hash := strings.Repeat("a", 64)
	state := map[string]any{"btc_height": 100, "snapshot_id": hash, "stable_block_hash": hash, "stable_lag": 6,
		"local_state_commit": hash, "system_state_id": hash, "balance_history_api_version": "1.0.0", "balance_history_semantics_version": "v1",
		"activation_registry_id": hash, "active_version_set": map[string]any{"uip0001": 1, "energy": "v1"}, "active_version_set_id": hash}
	profile := map[string]any{"pass_id": fixturePass, "owner_script_hash": hash, "state": "active", "pass_kind": "standard",
		"raw_energy": "340282366920938463463374607431768211455", "collab_contribution": "0", "effective_energy": "340282366920938463463374607431768211455", "level": 1,
		"difficulty_factor_bps": 9900, "collab_breakdown_count": 0}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{map[string]any{"height": 5, "hash": "0x" + hash}}})
			return
		}
		var req struct {
			Method string            `json:"method"`
			Params []json.RawMessage `json:"params"`
		}
		if json.NewDecoder(r.Body).Decode(&req) != nil {
			t.Error("invalid RPC request")
			return
		}
		result := map[string]any{}
		switch req.Method {
		case "eth_chainId":
			_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": 1, "result": "0x1"})
			return
		case "eth_getBlockByNumber":
			result = map[string]any{"number": "0x6", "hash": "0x" + hash, "timestamp": "0x1"}
		case "get_rpc_info":
			result = map[string]any{"service": "usdb-indexer", "api_version": "1.0.0", "network": "bitcoin",
				"economic_state_view_version": economicView, "candidate_set_selection_rule": candidateOrder, "economic_page_max_limit": 100,
				"activation_registry_id": hash, "features": []string{"historical_state_ref", "pass_economic_profile", "candidate_set_view", "pass_snapshot"}}
		case "get_readiness":
			result = map[string]any{"service": "usdb-indexer", "query_ready": true, "consensus_ready": true, "synced_block_height": 100, "balance_history_stable_height": 101, "message": "PRIVATE rpc.internal:28020"}
		case "get_candidate_set_view":
			result = map[string]any{"view_version": economicView, "external_state": state, "selection_rule": candidateOrder, "limit": 25, "total": 1, "next_cursor": "opaque-cursor", "items": []any{profile}}
		case "get_pass_economic_profile":
			result = map[string]any{"view_version": economicView, "external_state": state, "pass": profile}
		case "get_pass_snapshot":
			result = map[string]any{"inscription_id": fixturePass, "resolved_height": 100, "state": "active", "pass_kind": "standard", "mint_block_height": 90, "prev": []string{}, "invalid_reason": "PRIVATE rpc.internal"}
		default:
			t.Errorf("unexpected method %s", req.Method)
		}
		envelope := map[string]any{"jsonrpc": "2.0", "id": 1, "result": result}
		if mutate != nil {
			mutate(req.Method, req.Params, envelope)
		}
		_ = json.NewEncoder(w).Encode(envelope)
	}))
	t.Cleanup(server.Close)
	g, err := newGateway(server.URL, "0x1", "0x"+hash)
	if err != nil {
		t.Fatal(err)
	}
	g.indexer, err = newGateway(server.URL, g.chainID, g.genesis)
	if err != nil {
		t.Fatal(err)
	}
	g.catalog = networkCatalog{Bundle: "fixture", ChainID: 1, Genesis: g.genesis, BTCNetwork: "btc-mainnet", BTCOrigin: 90, Registry: hash}
	g.network = []byte(`{"chainName":"USDB Testnet","rpcUrls":["https://example.invalid/rpc"],"blockExplorerUrls":["https://example.invalid"],"nativeCurrency":{"name":"USDB","symbol":"USDB","decimals":18}}`)
	g.explorerURL = server.URL
	return g
}

func getPublic(g *gateway, path string) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("GET", "/api/usdb/v1/"+path, nil))
	return w
}

func TestUSDBOverviewSeparatesHeightsAndSanitizesReadiness(t *testing.T) {
	g := publicFixture(t, nil)
	w := getPublic(g, "overview")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body)
	}
	var result map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &result)
	if result["chain"].(map[string]any)["height"] != "6" || result["explorer"].(map[string]any)["height"] != "5" || result["indexer"].(map[string]any)["btc_height"] != float64(100) {
		t.Fatal(result)
	}
	if strings.Contains(w.Body.String(), "PRIVATE") || strings.Contains(w.Body.String(), "127.0.0.1") {
		t.Fatal("private upstream data escaped")
	}
	g.indexer = nil
	w = getPublic(g, "overview")
	if w.Code != 200 || !strings.Contains(w.Body.String(), "INDEXER_NOT_CONFIGURED") {
		t.Fatal(w.Code, w.Body)
	}
	if getPublic(g, "passes").Code != 503 {
		t.Fatal("unconfigured indexer accepted")
	}
}

func TestUSDBDetailPinsAllRelatedReadsAndPreservesLargeIntegers(t *testing.T) {
	pinned := false
	g := publicFixture(t, func(method string, params []json.RawMessage, _ map[string]any) {
		if method == "get_pass_snapshot" {
			var p map[string]any
			_ = json.Unmarshal(params[0], &p)
			context := p["context"].(map[string]any)
			if p["at_height"] != float64(100) || context["requested_height"] != float64(100) || context["expected_state"].(map[string]any)["snapshot_id"] != strings.Repeat("a", 64) {
				t.Error(p)
			}
			pinned = true
		}
	})
	w := getPublic(g, "passes/"+fixturePass)
	if w.Code != 200 || !pinned || !strings.Contains(w.Body.String(), `"340282366920938463463374607431768211455"`) || strings.Contains(w.Body.String(), "PRIVATE") {
		t.Fatal(w.Code, w.Body, pinned)
	}
}

func TestUSDBPagesBindHeightStateAndCursor(t *testing.T) {
	g := publicFixture(t, func(method string, params []json.RawMessage, _ map[string]any) {
		if method == "get_candidate_set_view" {
			var p map[string]any
			_ = json.Unmarshal(params[0], &p)
			if p["block_height"] != float64(100) || p["cursor"] != "opaque-cursor" || p["context"].(map[string]any)["expected_state"].(map[string]any)["system_state_id"] != strings.Repeat("a", 64) {
				t.Error(p)
			}
		}
	})
	w := getPublic(g, "passes?height=100&state="+strings.Repeat("a", 64)+"&cursor=opaque-cursor")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body)
	}
	for _, query := range []string{"?cursor=x", "?height=1&cursor=x", "?height=01", "?height=-1", "?height=4294967296", "?height=100&height=100", "?endpoint=http://private", "?limit=999999"} {
		if w = getPublic(g, "passes"+query); w.Code != 400 {
			t.Fatal(query, w.Code, w.Body)
		}
	}
}

func TestUSDBIndexerErrorsFailClosedWithPublicCodes(t *testing.T) {
	for _, row := range []struct {
		code, status int
		public       string
	}{{-32011, 404, "PASS_NOT_FOUND"}, {-32043, 409, "STATE_CHANGED"}, {-32010, 503, "HISTORY_UNAVAILABLE"}, {-32601, 503, "INDEXER_INCOMPATIBLE"}, {-32603, 503, "UPSTREAM_UNAVAILABLE"}} {
		t.Run(row.public, func(t *testing.T) {
			g := publicFixture(t, func(method string, _ []json.RawMessage, result map[string]any) {
				if method == "get_pass_economic_profile" {
					delete(result, "result")
					result["error"] = map[string]any{"code": row.code, "message": "PRIVATE http://user:password@node:28020", "data": "SECRET"}
				}
			})
			w := getPublic(g, "passes/"+fixturePass)
			if w.Code != row.status || !strings.Contains(w.Body.String(), row.public) || strings.Contains(w.Body.String(), "PRIVATE") || strings.Contains(w.Body.String(), "SECRET") {
				t.Fatal(w.Code, w.Body)
			}
		})
	}
}

func TestUSDBNetworkReadinessAndMalformedStateAreRejected(t *testing.T) {
	for _, change := range []struct {
		method, field string
		value         any
		code          string
	}{
		{"get_rpc_info", "network", "regtest", "INDEXER_NETWORK_MISMATCH"},
		{"get_rpc_info", "activation_registry_id", strings.Repeat("b", 64), "INDEXER_NETWORK_MISMATCH"},
		{"get_rpc_info", "economic_state_view_version", "future", "INDEXER_INCOMPATIBLE"},
		{"get_readiness", "consensus_ready", false, "INDEXER_NOT_READY"},
		{"get_candidate_set_view", "external_state", map[string]any{}, "INVALID_UPSTREAM_RESPONSE"},
	} {
		t.Run(change.field, func(t *testing.T) {
			g := publicFixture(t, func(method string, _ []json.RawMessage, response map[string]any) {
				if method == change.method {
					response["result"].(map[string]any)[change.field] = change.value
				}
			})
			w := getPublic(g, "passes")
			if w.Code < 400 || !strings.Contains(w.Body.String(), change.code) {
				t.Fatal(w.Code, w.Body)
			}
		})
	}
	g := publicFixture(t, nil)
	if getPublic(g, "passes?height=99").Code != 502 {
		t.Fatal("accepted wrong historical height")
	}
	if getPublic(g, "passes?height=100&state="+strings.Repeat("b", 64)).Code != 502 {
		t.Fatal("accepted wrong state")
	}
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/api/usdb/v1/passes", strings.NewReader(`{"method":"reset"}`)))
	if w.Code != 405 {
		t.Fatal(w.Code)
	}
}
