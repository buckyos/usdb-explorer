package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const economicView = "uip-0006-usdb-economic-state-view:v1"
const candidateOrder = "uip-0006:effective-energy-desc-pass-id-asc:v1"
const publicSchema = "usdb-explorer-public:v1"

var digestText = regexp.MustCompile(`^[0-9a-f]{64}$`)
var chainHash = regexp.MustCompile(`^0x[0-9a-f]{64}$`)
var passText = regexp.MustCompile(`^[0-9a-f]{64}i(0|[1-9][0-9]{0,9})$`)
var decimalText = regexp.MustCompile(`^(0|[1-9][0-9]*)$`)

// networkCatalog contains only public, release-pinned network identity fields.
type networkCatalog struct {
	Bundle     string `json:"bundle_id"`
	ChainID    uint64 `json:"chain_id"`
	Genesis    string `json:"genesis_block_hash"`
	BTCNetwork string `json:"btc_network_id"`
	BTCOrigin  uint32 `json:"btc_index_origin_height"`
	Registry   string `json:"btc_activation_registry_id"`
}

func (n networkCatalog) valid(g *gateway) bool {
	return n.Bundle != "" && "0x"+strconv.FormatUint(n.ChainID, 16) == g.chainID && n.Genesis == g.genesis &&
		digestText.MatchString(n.Registry) && (n.BTCNetwork == "btc-mainnet" || n.BTCNetwork == "btc-testnet")
}

type publicFailure struct {
	status int
	code   string
}

func (e *publicFailure) Error() string { return e.code }
func unavailable() error               { return &publicFailure{503, "UPSTREAM_UNAVAILABLE"} }
func invalidResponse() error           { return &publicFailure{502, "INVALID_UPSTREAM_RESPONSE"} }

// rpcValue never returns upstream messages, error.data or endpoint addresses.
func (g *gateway) rpcValue(ctx context.Context, method string, params any, target any) error {
	payload, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
	data, err := g.forward(ctx, payload)
	if err != nil {
		return unavailable()
	}
	var envelope struct {
		Version string          `json:"jsonrpc"`
		ID      json.RawMessage `json:"id"`
		Result  json.RawMessage `json:"result"`
		Error   *struct {
			Code int `json:"code"`
		} `json:"error"`
	}
	if strictJSON(data) != nil || json.Unmarshal(data, &envelope) != nil || envelope.Version != "2.0" || string(envelope.ID) != "1" {
		return invalidResponse()
	}
	if envelope.Error != nil {
		switch envelope.Error.Code {
		case -32011, -32018:
			return &publicFailure{404, "PASS_NOT_FOUND"}
		case -32042, -32043, -32045, -32046, -32056:
			return &publicFailure{409, "STATE_CHANGED"}
		case -32010, -32012, -32013, -32040, -32041, -32047, -32048, -32049:
			return &publicFailure{503, "HISTORY_UNAVAILABLE"}
		case -32601, -32044, -32050, -32052, -32053, -32054, -32055, -32057:
			return &publicFailure{503, "INDEXER_INCOMPATIBLE"}
		case -32015, -32602:
			return &publicFailure{400, "INVALID_QUERY"}
		default:
			return unavailable()
		}
	}
	if len(envelope.Result) == 0 || bytes.Equal(envelope.Result, []byte("null")) || json.Unmarshal(envelope.Result, target) != nil {
		return invalidResponse()
	}
	return nil
}

type indexerReadiness struct {
	Service        string  `json:"service"`
	QueryReady     bool    `json:"query_ready"`
	ConsensusReady bool    `json:"consensus_ready"`
	Height         *uint32 `json:"synced_block_height"`
	StableHeight   *uint32 `json:"balance_history_stable_height"`
}

func (g *gateway) indexerReady(ctx context.Context) (*indexerReadiness, error) {
	if g.indexer == nil {
		return nil, &publicFailure{503, "INDEXER_NOT_CONFIGURED"}
	}
	var info struct {
		Service  string   `json:"service"`
		Version  string   `json:"api_version"`
		Network  string   `json:"network"`
		View     string   `json:"economic_state_view_version"`
		Order    string   `json:"candidate_set_selection_rule"`
		Limit    uint32   `json:"economic_page_max_limit"`
		Registry string   `json:"activation_registry_id"`
		Features []string `json:"features"`
	}
	if err := g.indexer.rpcValue(ctx, "get_rpc_info", []any{}, &info); err != nil {
		return nil, err
	}
	features := map[string]bool{}
	for _, name := range info.Features {
		features[name] = true
	}
	if info.Service != "usdb-indexer" || info.Version != "1.0.0" || info.View != economicView || info.Order != candidateOrder ||
		info.Limit < 25 || !features["historical_state_ref"] || !features["pass_economic_profile"] || !features["candidate_set_view"] || !features["pass_snapshot"] {
		return nil, &publicFailure{503, "INDEXER_INCOMPATIBLE"}
	}
	// rust-bitcoin's Network::Bitcoin displays as "bitcoin"; the public catalog uses "btc-mainnet".
	btcNetwork := map[string]string{"bitcoin": "btc-mainnet", "mainnet": "btc-mainnet", "testnet": "btc-testnet"}[info.Network]
	if info.Registry != g.catalog.Registry || btcNetwork != g.catalog.BTCNetwork {
		return nil, &publicFailure{503, "INDEXER_NETWORK_MISMATCH"}
	}
	var ready indexerReadiness
	if err := g.indexer.rpcValue(ctx, "get_readiness", []any{}, &ready); err != nil {
		return nil, err
	}
	if ready.Service != "usdb-indexer" {
		return nil, invalidResponse()
	}
	return &ready, nil
}

// externalState pins all pages and related queries to one historical BTC-side view.
type externalState struct {
	Height     uint32                     `json:"btc_height"`
	Snapshot   string                     `json:"snapshot_id"`
	BlockHash  string                     `json:"stable_block_hash"`
	StableLag  uint32                     `json:"stable_lag"`
	Commit     string                     `json:"local_state_commit"`
	System     string                     `json:"system_state_id"`
	APIVersion string                     `json:"balance_history_api_version"`
	Semantics  string                     `json:"balance_history_semantics_version"`
	Registry   string                     `json:"activation_registry_id"`
	Versions   map[string]json.RawMessage `json:"active_version_set"`
	VersionID  string                     `json:"active_version_set_id"`
}

func (s externalState) valid(g *gateway, q url.Values) bool {
	if s.Registry != g.catalog.Registry || s.Height < g.catalog.BTCOrigin || !digestText.MatchString(s.Snapshot) ||
		!digestText.MatchString(s.BlockHash) || !digestText.MatchString(s.Commit) || !digestText.MatchString(s.System) ||
		!digestText.MatchString(s.VersionID) || len(s.Versions) == 0 || s.APIVersion == "" || s.Semantics == "" {
		return false
	}
	for key, raw := range s.Versions {
		if len(key) > 64 || len(raw) > 130 {
			return false
		}
		var text string
		if json.Unmarshal(raw, &text) != nil {
			if !decimalText.Match(raw) {
				return false
			}
		} else if !regexp.MustCompile(`^[a-zA-Z0-9:._-]{1,128}$`).MatchString(text) {
			return false
		}
	}
	return (q.Get("height") == "" || q.Get("height") == strconv.FormatUint(uint64(s.Height), 10)) &&
		(q.Get("state") == "" || q.Get("state") == s.System)
}

func (s externalState) context() map[string]any {
	return map[string]any{"requested_height": s.Height, "expected_state": map[string]any{
		"snapshot_id": s.Snapshot, "system_state_id": s.System, "local_state_commit": s.Commit,
		"activation_registry_id": s.Registry, "active_version_set_id": s.VersionID}}
}

type passProfile struct {
	ID              string  `json:"pass_id"`
	Owner           string  `json:"owner_script_hash"`
	BTCAddress      *string `json:"owner_btc_addr,omitempty"`
	State           string  `json:"state"`
	Kind            string  `json:"pass_kind"`
	RewardAddress   *string `json:"usdb_main,omitempty"`
	RawEnergy       string  `json:"raw_energy"`
	CollabEnergy    string  `json:"collab_contribution"`
	EffectiveEnergy string  `json:"effective_energy"`
	Level           uint8   `json:"level"`
	Difficulty      uint64  `json:"difficulty_factor_bps"`
	Collaborators   uint64  `json:"collab_breakdown_count"`
}

func validPassID(value string) bool {
	if !passText.MatchString(value) {
		return false
	}
	_, err := strconv.ParseUint(strings.Split(value, "i")[1], 10, 32)
	return err == nil
}

func (p passProfile) valid() bool {
	if !validPassID(p.ID) || !digestText.MatchString(p.Owner) || p.Difficulty > 10000 || p.Collaborators > 9007199254740991 {
		return false
	}
	if p.Kind != "standard" && p.Kind != "collab" {
		return false
	}
	switch p.State {
	case "active", "dormant", "consumed", "burned", "invalid":
	default:
		return false
	}
	for _, energy := range []string{p.RawEnergy, p.CollabEnergy, p.EffectiveEnergy} {
		if (len(energy) > 39 || (len(energy) == 39 && energy > "340282366920938463463374607431768211455")) || !decimalText.MatchString(energy) {
			return false
		}
	}
	return (p.BTCAddress == nil || len(*p.BTCAddress) <= 128) && (p.RewardAddress == nil || regexp.MustCompile(`^0x[0-9a-fA-F]{40}$`).MatchString(*p.RewardAddress))
}

func publicJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func publicError(w http.ResponseWriter, err error) {
	var failure *publicFailure
	if !errors.As(err, &failure) {
		failure = &publicFailure{503, "UPSTREAM_UNAVAILABLE"}
	}
	publicJSON(w, failure.status, map[string]any{"schema_version": publicSchema, "error": map[string]string{"code": failure.code}})
}

func parsePublicQuery(r *http.Request) (url.Values, error) {
	if len(r.URL.RawQuery) > 5000 {
		return nil, &publicFailure{400, "INVALID_QUERY"}
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return nil, &publicFailure{400, "INVALID_QUERY"}
	}
	for name, values := range q {
		if len(values) != 1 || values[0] == "" {
			return nil, &publicFailure{400, "INVALID_QUERY"}
		}
		switch name {
		case "height":
			if !decimalText.MatchString(values[0]) {
				return nil, &publicFailure{400, "INVALID_QUERY"}
			}
			if _, err := strconv.ParseUint(values[0], 10, 32); err != nil {
				return nil, &publicFailure{400, "INVALID_QUERY"}
			}
		case "state":
			if !digestText.MatchString(values[0]) || q.Get("height") == "" {
				return nil, &publicFailure{400, "INVALID_QUERY"}
			}
		case "cursor":
			if len(values[0]) > 4096 || q.Get("height") == "" || q.Get("state") == "" {
				return nil, &publicFailure{400, "INVALID_QUERY"}
			}
		default:
			return nil, &publicFailure{400, "INVALID_QUERY"}
		}
	}
	return q, nil
}

func (g *gateway) usdb(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		publicError(w, &publicFailure{405, "READ_ONLY"})
		return
	}
	if path.Clean(r.URL.Path) != r.URL.Path || r.URL.RawPath != "" {
		publicError(w, &publicFailure{400, "INVALID_QUERY"})
		return
	}
	q, err := parsePublicQuery(r)
	if err != nil {
		publicError(w, err)
		return
	}
	resource := strings.TrimPrefix(r.URL.Path, "/api/usdb/v1/")
	if resource != "overview" && resource != "passes" && !(strings.HasPrefix(resource, "passes/") && validPassID(strings.TrimPrefix(resource, "passes/"))) {
		publicError(w, &publicFailure{404, "NOT_FOUND"})
		return
	}
	if (resource == "overview" && len(q) != 0) || (resource != "passes" && q.Get("cursor") != "") {
		publicError(w, &publicFailure{400, "INVALID_QUERY"})
		return
	}
	ip, _, _ := net.SplitHostPort(r.RemoteAddr)
	if !g.allow(ip, 1) {
		publicError(w, &publicFailure{429, "RATE_LIMITED"})
		return
	}
	select {
	case g.active <- struct{}{}:
		defer func() { <-g.active }()
	default:
		publicError(w, &publicFailure{503, "BUSY"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 12*time.Second)
	defer cancel()
	if !g.catalog.valid(g) || !g.identity(ctx, false) {
		publicError(w, &publicFailure{503, "CHAIN_IDENTITY_UNAVAILABLE"})
		return
	}
	if resource == "overview" {
		value, err := g.overview(ctx)
		if err != nil {
			publicError(w, err)
			return
		}
		publicJSON(w, 200, value)
		return
	}
	ready, err := g.indexerReady(ctx)
	if err != nil {
		publicError(w, err)
		return
	}
	if !ready.QueryReady || !ready.ConsensusReady || ready.Height == nil {
		publicError(w, &publicFailure{503, "INDEXER_NOT_READY"})
		return
	}
	params := map[string]any{"view_version": economicView}
	if height := q.Get("height"); height != "" {
		number, _ := strconv.ParseUint(height, 10, 32)
		params["block_height"] = number
		if state := q.Get("state"); state != "" {
			params["context"] = map[string]any{"requested_height": number, "expected_state": map[string]string{"system_state_id": state, "activation_registry_id": g.catalog.Registry}}
		}
	}
	var result any
	if resource == "passes" {
		result, err = g.passList(ctx, params, q)
	} else {
		result, err = g.passDetail(ctx, strings.TrimPrefix(resource, "passes/"), params, q)
	}
	if err != nil {
		publicError(w, err)
		return
	}
	publicJSON(w, 200, result)
}

func (g *gateway) passList(ctx context.Context, params map[string]any, q url.Values) (any, error) {
	params["limit"], params["selection_rule"] = 25, candidateOrder
	if q.Get("cursor") != "" {
		params["cursor"] = q.Get("cursor")
	}
	var result struct {
		View  string        `json:"view_version"`
		State externalState `json:"external_state"`
		Order string        `json:"selection_rule"`
		Total uint64        `json:"total"`
		Limit uint32        `json:"limit"`
		Next  *string       `json:"next_cursor"`
		Items []passProfile `json:"items"`
	}
	if err := g.indexer.rpcValue(ctx, "get_candidate_set_view", []any{params}, &result); err != nil {
		return nil, err
	}
	if result.View != economicView || !result.State.valid(g, q) || result.Order != candidateOrder || result.Limit != 25 || result.Items == nil || len(result.Items) > 25 || uint64(len(result.Items)) > result.Total ||
		(result.Next != nil && len(result.Items) == 0) ||
		(result.Next != nil && (len(*result.Next) == 0 || len(*result.Next) > 4096)) {
		return nil, invalidResponse()
	}
	for _, item := range result.Items {
		if !item.valid() || item.Kind != "standard" || item.State != "active" {
			return nil, invalidResponse()
		}
	}
	return map[string]any{"schema_version": publicSchema, "updated_at": time.Now().UTC().Format(time.RFC3339),
		"external_state": result.State, "selection_rule": result.Order, "total": strconv.FormatUint(result.Total, 10),
		"next_cursor": result.Next, "items": result.Items}, nil
}

func (g *gateway) passDetail(ctx context.Context, id string, params map[string]any, q url.Values) (any, error) {
	params["pass_id"] = id
	var result struct {
		View  string        `json:"view_version"`
		State externalState `json:"external_state"`
		Pass  passProfile   `json:"pass"`
	}
	if err := g.indexer.rpcValue(ctx, "get_pass_economic_profile", []any{params}, &result); err != nil {
		return nil, err
	}
	if result.View != economicView || !result.State.valid(g, q) || !result.Pass.valid() || result.Pass.ID != id {
		return nil, invalidResponse()
	}
	// The second read is pinned to the profile identity; never combine a new head with an old profile.
	var snapshot struct {
		ID            string   `json:"inscription_id"`
		Height        uint32   `json:"resolved_height"`
		State         string   `json:"state"`
		Kind          string   `json:"pass_kind"`
		MintHeight    uint32   `json:"mint_block_height"`
		LeaderPass    *string  `json:"leader_pass_id"`
		LeaderAddress *string  `json:"leader_btc_addr"`
		Previous      []string `json:"prev"`
	}
	if err := g.indexer.rpcValue(ctx, "get_pass_snapshot", []any{map[string]any{"inscription_id": id,
		"at_height": result.State.Height, "context": result.State.context()}}, &snapshot); err != nil {
		return nil, err
	}
	if snapshot.ID != id || snapshot.Height != result.State.Height || snapshot.State != result.Pass.State || snapshot.Kind != result.Pass.Kind ||
		snapshot.MintHeight > snapshot.Height || len(snapshot.Previous) > 100 ||
		(snapshot.LeaderPass != nil && !validPassID(*snapshot.LeaderPass)) || (snapshot.LeaderAddress != nil && len(*snapshot.LeaderAddress) > 128) {
		return nil, invalidResponse()
	}
	for _, previous := range snapshot.Previous {
		if !validPassID(previous) {
			return nil, invalidResponse()
		}
	}
	return map[string]any{"schema_version": publicSchema, "updated_at": time.Now().UTC().Format(time.RFC3339),
		"external_state": result.State, "pass": result.Pass, "inscription": snapshot}, nil
}

func (g *gateway) overview(ctx context.Context) (any, error) {
	var head struct {
		Number    string `json:"number"`
		Hash      string `json:"hash"`
		Timestamp string `json:"timestamp"`
	}
	if err := g.rpcValue(ctx, "eth_getBlockByNumber", []any{"latest", false}, &head); err != nil {
		return nil, err
	}
	height, e1 := hexNumber(json.RawMessage(strconv.Quote(head.Number)))
	timestamp, e2 := hexNumber(json.RawMessage(strconv.Quote(head.Timestamp)))
	if e1 != nil || e2 != nil || !chainHash.MatchString(head.Hash) {
		return nil, invalidResponse()
	}
	var wallet struct {
		ChainName string   `json:"chainName"`
		RPC       []string `json:"rpcUrls"`
		Explorer  []string `json:"blockExplorerUrls"`
		Currency  struct {
			Name     string `json:"name"`
			Symbol   string `json:"symbol"`
			Decimals uint8  `json:"decimals"`
		} `json:"nativeCurrency"`
	}
	if json.Unmarshal(g.network, &wallet) != nil {
		return nil, invalidResponse()
	}
	indexer := map[string]any{"status": "unavailable"}
	indexerContext, cancelIndexer := context.WithTimeout(ctx, 4*time.Second)
	ready, err := g.indexerReady(indexerContext)
	cancelIndexer()
	if err != nil {
		var failure *publicFailure
		if errors.As(err, &failure) {
			indexer["error_code"] = failure.code
		}
	} else {
		indexer["status"], indexer["query_ready"], indexer["consensus_ready"] = "ready", ready.QueryReady, ready.ConsensusReady
		indexer["btc_height"], indexer["btc_stable_height"] = ready.Height, ready.StableHeight
		if !ready.QueryReady || !ready.ConsensusReady || ready.Height == nil {
			indexer["status"] = "syncing"
		}
	}
	indexed := map[string]any{"status": "unavailable"}
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, g.explorerURL+"/api/v2/blocks?type=block", nil)
	response, err := g.client.Do(request)
	if err == nil {
		defer response.Body.Close()
		body, readErr := io.ReadAll(io.LimitReader(response.Body, maxResponse+1))
		var page struct {
			Items []struct {
				Height uint64 `json:"height"`
				Hash   string `json:"hash"`
			} `json:"items"`
		}
		if readErr == nil && len(body) <= maxResponse && response.StatusCode == 200 && json.Unmarshal(body, &page) == nil && page.Items != nil {
			if len(page.Items) > 0 && chainHash.MatchString(page.Items[0].Hash) {
				indexed = map[string]any{"status": "ready", "height": strconv.FormatUint(page.Items[0].Height, 10), "hash": page.Items[0].Hash}
			} else if len(page.Items) == 0 {
				indexed["status"] = "empty"
			}
		}
	}
	return map[string]any{"schema_version": publicSchema, "updated_at": time.Now().UTC().Format(time.RFC3339),
		"network": map[string]any{"name": wallet.ChainName, "chain_id": strconv.FormatUint(g.catalog.ChainID, 10), "chain_id_hex": g.chainID,
			"genesis_hash": g.genesis, "bundle_id": g.catalog.Bundle, "btc_network": g.catalog.BTCNetwork, "btc_index_origin_height": g.catalog.BTCOrigin,
			"rpc_urls": wallet.RPC, "explorer_urls": wallet.Explorer, "native_currency": wallet.Currency},
		"chain":    map[string]any{"height": strconv.FormatUint(height, 10), "hash": head.Hash, "timestamp": strconv.FormatUint(timestamp, 10)},
		"explorer": indexed, "indexer": indexer}, nil
}
