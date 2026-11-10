package faucet

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"

	"github.com/ethereum/go-ethereum/core/types"
)

func TestMinerTransferAndFundingShareRecoveryAndNonceGate(t *testing.T) {
	s, rpc, _ := setup(t)
	ctx := context.Background()
	rpc.failMethod = "eth_sendRawTransaction"
	j, err := s.Transfer(ctx, "miner", "t_directed", "0x2222222222222222222222222222222222222222", "10", minerKey)
	if err == nil || ErrorCode(err) != "BROADCAST_UNCERTAIN" || j.ID != "t_directed" {
		t.Fatalf("%+v %v", j, err)
	}
	if got := DescribeJob(j); got.ID != "t_directed" || got.Source != "miner" || got.Operation != "transfer" {
		t.Fatal(got)
	}
	sent := rpc.sent[0]
	if sent.To().Hex() != j.Address || sent.Value().String() != "10000000000000000000" {
		t.Fatal("wrong directed transfer")
	}
	rpc.failMethod = ""
	retry, err := s.RetryTransfer(ctx, j.ID)
	if err != nil || retry.Hash != j.Hash || len(rpc.sent) != 2 || rpc.sent[1].Hash() != sent.Hash() {
		t.Fatalf("retry: %+v %v", retry, err)
	}
	blocked, err := s.Fund(ctx, "fund-after-transfer", "1", minerKey)
	if err == nil || ErrorCode(err) != "TRANSFER_PENDING" || blocked.ID != j.ID {
		t.Fatalf("gate: %+v %v", blocked, err)
	}
	for _, params := range [][2]string{{recipient, "10"}, {j.Address, "11"}} {
		if _, err := s.Transfer(ctx, "miner", j.ID, params[0], params[1], minerKey); err == nil || ErrorCode(err) != "REQUEST_ID_CONFLICT" {
			t.Fatal(err)
		}
	}
	rpc.mine(sent, "0x1")
	refill, err := s.Fund(ctx, "t_refill", "1", minerKey)
	if err != nil || rpc.sent[2].Nonce() != 1 {
		t.Fatalf("refill: %+v %v", refill, err)
	}
	if DescribeJob(refill).ID != "t_refill" {
		t.Fatal("recovery ID prefix was removed twice")
	}
	blocked, err = s.Transfer(ctx, "miner", "next", recipient, "1", minerKey)
	if err == nil || ErrorCode(err) != "FUNDING_PENDING" || blocked.ID != "t_refill" {
		t.Fatalf("fund gate: %+v %v", blocked, err)
	}
	rpc.mine(rpc.sent[2], "0x1")
	if _, err := s.Transfer(ctx, "miner", "next", recipient, "1", minerKey); err != nil {
		t.Fatal(err)
	}
	if rpc.sent[3].Nonce() != 2 {
		t.Fatal("miner nonce did not advance")
	}
	rpc.mine(rpc.sent[3], "0x1")
	if err := s.RefreshFunding(ctx); err != nil {
		t.Fatal(err)
	}
	old, _ := s.job("t_next")
	if old.Status != "confirmed" {
		t.Fatal("directed miner transfer was not refreshed")
	}
}

func TestFaucetTransferSharesClaimWorkerButNotPublicPolicy(t *testing.T) {
	s, rpc, dir := setup(t)
	ctx := context.Background()
	s.config.DailyBudget = "1.01"
	public := claim(t, s, "public-claim-012345")
	if err := s.Refresh(ctx); err != nil || s.snapshot().Status != "DAILY_BUDGET_EXHAUSTED" {
		t.Fatal(err, s.snapshot())
	}
	manual, err := s.Transfer(ctx, "faucet", "t_manual", recipient, "10", "")
	if err != nil || manual.Status != "queued" || manual.ID != "t_manual" || len(rpc.sent) != 0 {
		t.Fatalf("queue: %+v %v", manual, err)
	}
	if _, err := s.Transfer(ctx, "faucet", "t_manual", recipient, "10", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Transfer(ctx, "faucet", "t_manual", recipient, "11", ""); err == nil || ErrorCode(err) != "REQUEST_ID_CONFLICT" {
		t.Fatal(err)
	}
	if err := s.Step(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rpc.sent) != 1 || rpc.sent[0].Value().String() != "1000000000000000000" {
		t.Fatal("manual payment bypassed older claim")
	}
	rpc.mine(rpc.sent[0], "0x1")
	if err := s.Step(ctx); err != nil {
		t.Fatal(err)
	}
	rpc.failMethod = "eth_sendRawTransaction"
	if err := s.Step(ctx); err == nil || ErrorCode(err) != "BROADCAST_UNCERTAIN" {
		t.Fatal(err)
	}
	saved, _ := s.job("t_" + manual.ID)
	if saved.Raw == "" || saved.Nonce.String != "1" {
		t.Fatal("manual transaction was not journaled")
	}
	before := len(rpc.sent)
	retry, err := s.RetryTransfer(ctx, manual.ID)
	if err != nil || retry.Hash != saved.Hash || len(rpc.sent) != before {
		t.Fatal("operator retry signed or rebroadcast from faucet wallet")
	}
	s.Close()
	rpc.failMethod = ""
	restored, err := Open(dir, s.config, rpc)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	if err := restored.Step(ctx); err != nil {
		t.Fatal(err)
	}
	last := rpc.sent[len(rpc.sent)-1]
	if last.Hash() != rpc.sent[before-1].Hash() || last.Nonce() != 1 || last.Value().String() != "10000000000000000000" {
		t.Fatal("restart duplicated the transfer")
	}
	from, err := types.Sender(types.NewEIP155Signer(last.ChainId()), last)
	if err != nil || from.Hex() != restored.sender() {
		t.Fatal("wrong faucet signer")
	}
	rpc.mine(last, "0x1")
	if err := restored.Step(ctx); err != nil {
		t.Fatal(err)
	}
	saved, _ = restored.job("t_" + manual.ID)
	if saved.Status != "confirmed" {
		t.Fatal(saved)
	}
	tx, err := restored.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	total, err := used(tx, public.Day)
	tx.Rollback()
	if err != nil || total.String() != public.Reserved {
		t.Fatal("operator payment consumed the public daily budget", total, err)
	}
}

func TestConcurrentFaucetTransferRequestsAreIdempotent(t *testing.T) {
	s, rpc, _ := setup(t)
	ctx := context.Background()
	var wg sync.WaitGroup
	results := make(chan error, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.Transfer(ctx, "faucet", "same-manual", recipient, "1", "")
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
	var count int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM jobs WHERE kind='faucet_transfer'").Scan(&count); err != nil || count != 1 || len(rpc.sent) != 0 {
		t.Fatal(count, err)
	}
	if _, err := s.Transfer(ctx, "miner", "same-manual", recipient, "1", minerKey); err == nil || ErrorCode(err) != "REQUEST_ID_CONFLICT" {
		t.Fatal(err)
	}
}

func TestManualTransferPreservesQueuedReservationsAndRejectsUnsafeInputs(t *testing.T) {
	s, rpc, _ := setup(t)
	ctx := context.Background()
	claim(t, s, "reserved-claim-012345")
	balance, _ := Units("3", 18)
	rpc.balance = "0x" + balance.Text(16)
	if _, err := s.Transfer(ctx, "faucet", "too-much", recipient, "2", ""); err == nil || ErrorCode(err) != "INSUFFICIENT_FUNDS" {
		t.Fatal(err)
	}
	if _, err := s.Transfer(ctx, "faucet", "fits", recipient, "1", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Transfer(ctx, "faucet", "overcommits", recipient, "1", ""); err == nil || ErrorCode(err) != "INSUFFICIENT_FUNDS" {
		t.Fatal(err)
	}
	for _, mode := range []string{"zero", "self", "contract", "gas", "wrong-network", "unexpected-key"} {
		t.Run(mode, func(t *testing.T) {
			fresh, chain, _ := setup(t)
			target, key := recipient, ""
			switch mode {
			case "zero":
				target = "0x" + strings.Repeat("0", 40)
			case "self":
				target = fresh.sender()
			case "contract":
				chain.contract = true
			case "gas":
				chain.gas = "0xffffffffffff"
			case "wrong-network":
				chain.wrongBroadcast = true
			case "unexpected-key":
				key = minerKey
			}
			if _, err := fresh.Transfer(ctx, "faucet", "invalid", target, "1", key); err == nil {
				t.Fatal("unsafe input accepted")
			}
			if len(chain.sent) != 0 {
				t.Fatal("invalid request broadcast")
			}
		})
	}
}

func TestDirectedTransfersAppearOnlyInOperatorHistory(t *testing.T) {
	s, rpc, _ := setup(t)
	ctx := context.Background()
	queued, err := s.Transfer(ctx, "faucet", "t_wallet", recipient, "1", "")
	if err != nil {
		t.Fatal(err)
	}
	miner, err := s.Transfer(ctx, "miner", "f_external", recipient, "2", minerKey)
	if err != nil {
		t.Fatal(err)
	}
	report, err := s.OperatorStatus(ctx, s.snapshot(), 1)
	if err != nil {
		t.Fatal(err)
	}
	history := report["transfers"].([]operatorJob)
	if len(history) != 1 || history[0].ID != miner.ID || history[0].Source != "miner" || history[0].AmountUSDB != "2" || report["history"].(map[string]any)["transfers_has_more"] != true {
		t.Fatal(report)
	}
	report, err = s.OperatorStatus(ctx, s.snapshot(), 20)
	if err != nil {
		t.Fatal(err)
	}
	history = report["transfers"].([]operatorJob)
	if len(history) != 2 || history[1].ID != queued.ID || history[1].Source != "faucet" {
		t.Fatal(history)
	}
	data, _ := json.Marshal(report)
	if strings.Contains(string(data), minerKey) {
		t.Fatal("private key exposed")
	}
	// Status reads do not sign queued wallet transfers or send additional payments.
	if len(rpc.sent) != 1 {
		t.Fatal("history query sent a transaction")
	}
	for _, j := range history {
		if j.Raw != "" && strings.Contains(string(data), j.Raw) {
			t.Fatal("signed bytes exposed")
		}
	}
}
