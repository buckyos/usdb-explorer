package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// economicsFixture exercises the actual HTTP/RPC boundary; no sibling checkout is required.
func economicsFixture(t *testing.T, mutate func(string, []json.RawMessage, map[string]any)) *gateway {
	t.Helper()
	hash := "0x" + strings.Repeat("a", 64)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Method string            `json:"method"`
			Params []json.RawMessage `json:"params"`
		}
		if json.NewDecoder(r.Body).Decode(&request) != nil {
			t.Fatal("invalid request")
		}
		status := uint64(0) // A reverted call still pays its fee.
		result := any(nil)
		switch request.Method {
		case "eth_chainId":
			result = "0x1"
		case "eth_getBlockByHash", "eth_getBlockByNumber":
			result = map[string]any{"number": "0x6", "hash": hash, "parentHash": hash, "stateRoot": hash, "receiptsRoot": hash,
				"miner": "0x" + strings.Repeat("1", 40), "gasUsed": "0x5208", "transactions": []string{hash}}
		case "eth_getUSDBBlockEconomics":
			if string(request.Params[0]) != `"`+hash+`"` {
				t.Error("RPC must use resolved block hash")
			}
			result = blockEconomicReport{Schema: economicsSchema, Status: "verified", Hash: hash, Number: "6", Parent: hash, Root: hash, Receipts: hash,
				Miner: "0x" + strings.Repeat("1", 40), Dividend: "0x" + strings.Repeat("2", 40),
				Versions: map[string]uint32{"payloadVersion": 1, "btcAnchorPolicyVersion": 1, "difficultyPolicyVersion": 1, "rewardRuleVersion": 1,
					"coinbaseEmissionPolicyVersion": 1, "feeSplitPolicyVersion": 1, "collaborationEfficiencyPolicyVersion": 1, "pricePolicyVersion": 1, "quotePolicyVersion": 0, "auxPoolPolicyVersion": 0},
				Selector:     &economicSelector{PassID: fixturePass, Height: 100, Snapshot: strings.Repeat("a", 64), System: strings.Repeat("a", 64), Registry: strings.Repeat("a", 64)},
				Amounts:      &economicAmounts{Before: "9007199254740993000000000000", After: "9007199254740993000000000001", Emission: "1", MinerEmission: "1", Fees: "63000", MinerFees: "37800", DAOFees: "25200"},
				Transactions: []economicTransaction{{Hash: hash, Status: &status, Gas: "21000", Price: "3", Fee: "63000", Miner: "37800", DAO: "25200", Route: "miner_and_dividend"}}}
		default:
			t.Error("unexpected method", request.Method)
		}
		// Convert typed data into a mutable JSON fixture for invalid-response tests.
		encoded, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "result": result})
		var envelope map[string]any
		_ = json.Unmarshal(encoded, &envelope)
		if mutate != nil {
			mutate(request.Method, request.Params, envelope)
		}
		_ = json.NewEncoder(w).Encode(envelope)
	}))
	t.Cleanup(server.Close)
	g, err := newGateway(server.URL, "0x1", hash)
	if err != nil {
		t.Fatal(err)
	}
	g.catalog = networkCatalog{Bundle: "fixture", ChainID: 1, Genesis: hash, BTCNetwork: "btc-mainnet", BTCOrigin: 90, Registry: strings.Repeat("a", 64)}
	return g
}

func TestEconomicsReadOnlyPinnedAndExact(t *testing.T) {
	g := economicsFixture(t, nil)
	w := getPublic(g, "blocks/6/economics")
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"9007199254740993000000000001"`) || !strings.Contains(w.Body.String(), `"status":0`) {
		t.Fatal(w.Code, w.Body)
	}
	for _, path := range []string{"blocks/latest/economics", "blocks/pending/economics", "blocks/06/economics", "blocks/-1/economics", "blocks/18446744073709551616/economics", "blocks/6/economics?height=1"} {
		if w := getPublic(g, path); w.Code < 400 {
			t.Fatal(path, w.Code)
		}
	}
	if methods["eth_getUSDBBlockEconomics"] {
		t.Fatal("expensive replay exposed as arbitrary public RPC")
	}
	w = httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("POST", "/api/usdb/v1/blocks/6/economics", nil))
	if w.Code != 405 {
		t.Fatal(w.Code)
	}
}

func TestEconomicsRejectsInvalidOrMixedReports(t *testing.T) {
	for _, field := range []string{"block_hash", "state_root", "block_number", "schema_version", "amounts", "selector", "versions", "transactions", "status"} {
		t.Run(field, func(t *testing.T) {
			g := economicsFixture(t, func(method string, _ []json.RawMessage, e map[string]any) {
				if method == "eth_getUSDBBlockEconomics" {
					e["result"].(map[string]any)[field] = nil
				}
			})
			if w := getPublic(g, "blocks/6/economics"); w.Code != 502 {
				t.Fatal(field, w.Code, w.Body)
			}
		})
	}
	for _, mutate := range []func(map[string]any){
		func(r map[string]any) { r["amounts"].(map[string]any)["fees_atoms"] = "63001" },
		func(r map[string]any) { r["amounts"].(map[string]any)["issued_after_atoms"] = "1" },
		func(r map[string]any) {
			r["selector"].(map[string]any)["activation_registry_id"] = strings.Repeat("b", 64)
		},
		func(r map[string]any) { r["transactions"].([]any)[0].(map[string]any)["gas_used"] = "21001" },
		func(r map[string]any) { r["transactions"].([]any)[0].(map[string]any)["status"] = nil },
		func(r map[string]any) { r["transactions"].([]any)[0].(map[string]any)["fee_atoms"] = "-1" },
	} {
		g := economicsFixture(t, func(method string, _ []json.RawMessage, e map[string]any) {
			if method == "eth_getUSDBBlockEconomics" {
				mutate(e["result"].(map[string]any))
			}
		})
		if w := getPublic(g, "blocks/6/economics"); w.Code != 502 {
			t.Fatal(w.Code, w.Body)
		}
	}
}

func TestEconomicsErrorsAreSanitizedAndReorgInvalidates(t *testing.T) {
	for code, want := range map[int]string{-32601: "ECONOMICS_NODE_UPGRADE_REQUIRED", -32062: "ECONOMICS_HISTORY_UNAVAILABLE", -32063: "ECONOMICS_POLICY_UNSUPPORTED", -32064: "ECONOMICS_VERIFICATION_FAILED", -32065: "ECONOMICS_TIMEOUT", -32066: "ECONOMICS_REPLAY_LIMIT", -32067: "BUSY"} {
		g := economicsFixture(t, func(method string, _ []json.RawMessage, e map[string]any) {
			if method == "eth_getUSDBBlockEconomics" {
				delete(e, "result")
				e["error"] = map[string]any{"code": code, "message": "PRIVATE http://secret.internal", "data": "PRIVATE"}
			}
		})
		w := getPublic(g, "blocks/6/economics")
		if w.Code < 400 || !strings.Contains(w.Body.String(), want) || strings.Contains(w.Body.String(), "PRIVATE") {
			t.Fatal(w.Code, w.Body)
		}
	}
	queried := false
	g := economicsFixture(t, func(method string, params []json.RawMessage, e map[string]any) {
		if method == "eth_getUSDBBlockEconomics" {
			queried = true
		}
		if queried && method == "eth_getBlockByNumber" {
			e["result"].(map[string]any)["hash"] = "0x" + strings.Repeat("b", 64)
		}
	})
	if w := getPublic(g, "blocks/6/economics"); w.Code != 409 || strings.Contains(w.Body.String(), "fee_atoms") {
		t.Fatal(w.Code, w.Body)
	}
	g = economicsFixture(t, func(method string, params []json.RawMessage, e map[string]any) {
		if method == "eth_getBlockByNumber" && string(params[0]) == `"0x6"` {
			e["result"] = nil
		}
	})
	if w := getPublic(g, "blocks/6/economics"); w.Code != 404 {
		t.Fatal(w.Code, w.Body)
	}
}

func TestEconomicsGenesisDoesNotInventAmounts(t *testing.T) {
	g := economicsFixture(t, func(method string, _ []json.RawMessage, e map[string]any) {
		if method == "eth_getBlockByNumber" {
			r := e["result"].(map[string]any)
			r["number"], r["gasUsed"], r["transactions"] = "0x0", "0x0", []string{}
		}
		if method == "eth_getUSDBBlockEconomics" {
			r := e["result"].(map[string]any)
			r["block_number"], r["status"], r["transactions"] = "0", "genesis_not_applicable", []any{}
			delete(r, "amounts")
			delete(r, "selector")
			delete(r, "versions")
		}
	})
	w := getPublic(g, "blocks/0/economics")
	if w.Code != 200 || strings.Contains(w.Body.String(), "emission_atoms") || !strings.Contains(w.Body.String(), "genesis_not_applicable") {
		t.Fatal(w.Code, w.Body)
	}
}
