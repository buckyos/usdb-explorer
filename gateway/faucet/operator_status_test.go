package faucet

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
)

type statusChain struct {
	Chain
	failMethod string
	reorganize bool
	calls      []string
	pinnedTags []string
}

func (c *statusChain) Call(ctx context.Context, writer bool, method string, params any, target any) error {
	c.calls = append(c.calls, method)
	if method == c.failMethod {
		return errors.New("RPC_UNAVAILABLE")
	}
	p := params.([]any)
	var value any
	switch method {
	case "eth_getBalance":
		c.pinnedTags = append(c.pinnedTags, p[1].(string))
		amount, _ := Units("10.989978999999853", 18)
		if p[1] == "pending" {
			amount.SetInt64(1)
		}
		value = "0x" + amount.Text(16)
	case "eth_getTransactionCount":
		c.pinnedTags = append(c.pinnedTags, p[1].(string))
		value = "0x7"
		if p[1] == "pending" {
			value = "0x9"
		}
	case "eth_getBlockByNumber":
		if c.reorganize && p[0] != "latest" && p[0] != "0x0" {
			value = map[string]string{"hash": "0x" + strings.Repeat("d", 64)}
		} else {
			return c.Chain.Call(ctx, writer, method, params, target)
		}
	default:
		return c.Chain.Call(ctx, writer, method, params, target)
	}
	data, _ := json.Marshal(value)
	return json.Unmarshal(data, target)
}

func TestOperatorStatusFreshBalancePreservesWorkerHealth(t *testing.T) {
	s, rpc, _ := setup(t)
	chain := &statusChain{Chain: rpc}
	s.rpc = chain
	s.setStatus("NONCE_CONFLICT", "1000000000000000000", "12100000000000000")
	live := s.snapshot()
	report, err := s.OperatorStatus(context.Background(), live, DefaultStatusLimit)
	if err != nil {
		t.Fatal(err)
	}
	worker := report["faucet"].(operatorWorker)
	if worker.Status.Status != "NONCE_CONFLICT" || worker.BalanceAtoms != live.BalanceAtoms || worker.BalanceUSDB != "1" || worker.ReservedUSDB != "0.0121" {
		t.Fatalf("worker health or cached values were replaced: %+v", worker)
	}
	fresh := report["chain"].(map[string]any)
	if fresh["status"] != "ok" || fresh["balance_usdb"] != "10.989978999999853" || fresh["balance_atoms"] != "10989978999999853000" || fresh["pending_balance_usdb"] != "0.000000000000000001" || fresh["nonce"] != "7" || fresh["pending_nonce"] != "9" || fresh["block_number"] != uint64(12) || fresh["checked_at"] != s.now().Unix() {
		t.Fatalf("unexpected chain observation: %+v", fresh)
	}
	if strings.Join(chain.pinnedTags, ",") != "0xc,0xc,pending,pending" {
		t.Fatalf("unpinned mined state: %v", chain.pinnedTags)
	}
	if s.snapshot() != live || len(rpc.sent) != 0 {
		t.Fatal("status mutated the worker or broadcast")
	}
}

func insertStatusHistory(t *testing.T, s *Service, kind string, count int) {
	t.Helper()
	tx, err := s.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for i := 0; i < count; i++ {
		prefix := "c_"
		if kind == "fund" {
			prefix = "f_"
		}
		// Reverse lexical IDs and equal timestamps expose unstable tie ordering.
		j := Job{ID: fmt.Sprintf("%shistory-request-%03d", prefix, 100-i), Kind: kind, Address: recipient, Sender: "private-sender-marker", Amount: "10000000000000000", Reserved: "0", IP: "private-ip-marker", Day: "2026-09-19", Created: s.now().Unix(), Status: []string{"queued", "pending", "confirming", "confirmed", "failed", "rejected"}[i%6], Raw: "private-signed-bytes", Hash: "0x" + strings.Repeat("e", 64)}
		if err := insertJob(tx, j); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}

func TestOperatorStatusBoundsHistoryAndKeepsAllPersistedClaimStates(t *testing.T) {
	s, _, _ := setup(t)
	insertStatusHistory(t, s, "claim", 25)
	insertStatusHistory(t, s, "fund", 22)
	for _, limit := range []int{DefaultStatusLimit, 3, MaxStatusLimit} {
		report, err := s.OperatorStatus(context.Background(), s.snapshot(), limit)
		if err != nil {
			t.Fatal(err)
		}
		claims := report["claims"].([]operatorJob)
		funding := report["funding"].([]operatorJob)
		history := report["history"].(map[string]any)
		if len(claims) != min(limit, 25) || len(funding) != min(limit, 22) || history["limit"] != limit || history["claims_has_more"] != (limit < 25) || history["funding_has_more"] != (limit < 22) {
			t.Fatalf("history limit ignored: %+v", history)
		}
		if claims[0].ID != "c_history-request-076" || funding[0].ID != "history-request-079" || claims[0].AmountUSDB != "0.01" || claims[0].CreatedAtUTC != "2026-09-19T12:00:00Z" {
			t.Fatalf("invalid newest-first display: %+v %+v", claims[0], funding[0])
		}
		if limit == MaxStatusLimit {
			states := map[string]bool{}
			for _, j := range claims {
				states[j.Status] = true
			}
			if len(states) != 6 {
				t.Fatalf("history lost persisted states: %v", states)
			}
		}
		encoded, err := json.Marshal(report)
		if err != nil {
			t.Fatal(err)
		}
		for _, secret := range []string{"private-sender-marker", "private-ip-marker", "private-signed-bytes", minerKey, s.config.ProxyToken} {
			if strings.Contains(string(encoded), secret) {
				t.Fatalf("private data leaked: %s", secret)
			}
		}
	}
	empty, _, _ := setup(t)
	report, err := empty.OperatorStatus(context.Background(), empty.snapshot(), DefaultStatusLimit)
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(report)
	if !strings.Contains(string(encoded), `"claims":[]`) || !strings.Contains(string(encoded), `"funding":[]`) {
		t.Fatal("empty history must be an array")
	}
}

func TestOperatorStatusRPCFailuresAndReorgKeepHistoryWithoutInventingBalance(t *testing.T) {
	for _, mode := range []string{"rpc", "wrong-network", "reorg"} {
		t.Run(mode, func(t *testing.T) {
			s, rpc, _ := setup(t)
			j := claim(t, s, "status-claim-012345")
			chain := &statusChain{Chain: rpc}
			s.rpc = chain
			expected := "RPC_UNAVAILABLE"
			switch mode {
			case "rpc":
				chain.failMethod = "eth_getBalance"
			case "wrong-network":
				rpc.wrong = true
				expected = "WRONG_NETWORK"
			case "reorg":
				chain.reorganize = true
				expected = "CHAIN_CHANGED"
			}
			report, err := s.OperatorStatus(context.Background(), s.snapshot(), 20)
			if err != nil {
				t.Fatal(err)
			}
			fresh := report["chain"].(map[string]any)
			if fresh["status"] != "unavailable" || fresh["error"] != expected || fresh["balance_atoms"] != nil || fresh["balance_usdb"] != nil {
				t.Fatalf("false live balance: %+v", fresh)
			}
			claims := report["claims"].([]operatorJob)
			if len(claims) != 1 || claims[0].ID != j.ID || claims[0].Status != "queued" {
				t.Fatal("lost local history")
			}
			persisted, err := s.job(j.ID)
			if err != nil || persisted.Status != "queued" || persisted.Raw != "" || len(rpc.sent) != 0 {
				t.Fatal("status changed a claim")
			}
		})
	}
}

func TestOperatorStatusValidatesLimitBeforeReadingChain(t *testing.T) {
	s, rpc, _ := setup(t)
	chain := &statusChain{Chain: rpc}
	s.rpc = chain
	for _, limit := range []int{-1, 0, 101} {
		if _, err := s.OperatorStatus(context.Background(), s.snapshot(), limit); err == nil || ErrorCode(err) != "INVALID_LIMIT" {
			t.Fatal(err)
		}
	}
	if len(chain.calls) != 0 {
		t.Fatal("invalid history limit reached RPC")
	}
}

func TestFormatAtomsIsExact(t *testing.T) {
	for raw, want := range map[string]string{"0": "0", "1": "0.000000000000000001", "1000000000000000000": "1", "10989978999999853000": "10.989978999999853", "999999999999999999999999999999999999": "999999999999999999.999999999999999999"} {
		got, err := formatAtoms(raw)
		if err != nil || got != want {
			t.Fatalf("%s = %s, %v", raw, got, err)
		}
	}
	for _, raw := range []string{"", "-1", "1e18", "not-a-number"} {
		if _, err := formatAtoms(raw); err == nil {
			t.Fatalf("accepted %q", raw)
		}
	}
}
