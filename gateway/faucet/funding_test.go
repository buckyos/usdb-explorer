package faucet

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestFundingReconcilesBeforeNextTransfer(t *testing.T) {
	for _, receiptStatus := range []string{"0x1", "0x0"} {
		t.Run(receiptStatus, func(t *testing.T) {
			s, rpc, dir := setup(t)
			ctx := context.Background()
			first, err := s.Fund(ctx, "first-refill", "1", minerKey)
			if err != nil {
				t.Fatal(err)
			}
			rpc.mine(rpc.sent[0], receiptStatus)
			// Older deployments leave this record pending even after it is mined.
			s.Close()
			restored, err := Open(dir, s.config, rpc)
			if err != nil {
				t.Fatal(err)
			}
			defer restored.Close()
			second, err := restored.Fund(ctx, "second-refill", "10", minerKey)
			if err != nil {
				t.Fatalf("mined refill blocked the next transfer: %v", err)
			}
			want := "confirmed"
			if receiptStatus == "0x0" {
				want = "failed"
			}
			old, err := restored.job("f_" + first.ID)
			if err != nil || old.Status != want {
				t.Fatalf("old refill: %+v %v", old, err)
			}
			if second.ID != "second-refill" || second.Status != "pending" || len(rpc.sent) != 2 || rpc.sent[1].Nonce() != 1 || rpc.sent[1].Value().String() != "10000000000000000000" {
				t.Fatalf("second refill: %+v, broadcasts: %d", second, len(rpc.sent))
			}
		})
	}
}

func TestFundingWorkerConfirmsAfterRestartWithoutRetry(t *testing.T) {
	s, rpc, dir := setup(t)
	ctx := context.Background()
	rpc.failMethod = "eth_sendRawTransaction"
	first, err := s.Fund(ctx, "uncertain-refill", "1", minerKey)
	if err == nil || err.Error() != "BROADCAST_UNCERTAIN" {
		t.Fatal(err)
	}
	s.Close()
	rpc.failMethod = ""
	rpc.mine(rpc.sent[0], "0x1")
	restored, err := Open(dir, s.config, rpc)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Close()
	workerCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { defer close(done); restored.Run(workerCtx) }()
	defer func() { cancel(); <-done }()
	deadline := time.Now().Add(2 * time.Second)
	for {
		j, err := restored.job("f_" + first.ID)
		if err != nil {
			t.Fatal(err)
		}
		if j.Status == "confirmed" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("worker left funding %s", j.Status)
		}
		time.Sleep(10 * time.Millisecond)
	}
	rpc.mu.Lock()
	defer rpc.mu.Unlock()
	if len(rpc.sent) != 1 {
		t.Fatal("receipt observation rebroadcast a miner transfer")
	}
}

func TestUnconfirmedFundingStaysBlockedWithoutRebroadcast(t *testing.T) {
	for _, mode := range []string{"pending", "confirming", "reorg"} {
		t.Run(mode, func(t *testing.T) {
			s, rpc, _ := setup(t)
			ctx := context.Background()
			first, err := s.Fund(ctx, "first-refill", "1", minerKey)
			if err != nil {
				t.Fatal(err)
			}
			if mode != "pending" {
				rpc.mine(rpc.sent[0], "0x1")
			}
			want := "pending"
			if mode == "confirming" {
				rpc.head = 10
				want = "confirming"
			}
			if mode == "reorg" {
				rpc.reorg = true
			}
			if err := s.RefreshFunding(ctx); err != nil {
				t.Fatal(err)
			}
			// A pending predecessor is reported even if a new payment would fail
			// balance checks: no nonce allocation or signing is needed yet.
			rpc.balance = "0x0"
			blocked, err := s.Fund(ctx, "second-refill", "10", minerKey)
			if err == nil || ErrorCode(err) != "FUNDING_PENDING" || blocked.ID != first.ID || blocked.Hash != first.Hash || blocked.Status != want {
				t.Fatalf("blocker: %+v %v", blocked, err)
			}
			var count int
			if err := s.db.QueryRow("SELECT COUNT(*) FROM jobs WHERE kind='fund'").Scan(&count); err != nil {
				t.Fatal(err)
			}
			if count != 1 || len(rpc.sent) != 1 {
				t.Fatal("created or broadcast a second payment")
			}
		})
	}
}

func TestFundingRefreshFailsClosedOnRPCOrNetworkErrors(t *testing.T) {
	for _, mode := range []string{"receipt", "wrong-read", "wrong-broadcast"} {
		t.Run(mode, func(t *testing.T) {
			s, rpc, _ := setup(t)
			ctx := context.Background()
			first, err := s.Fund(ctx, "first-refill", "1", minerKey)
			if err != nil {
				t.Fatal(err)
			}
			rpc.mine(rpc.sent[0], "0x1")
			switch mode {
			case "receipt":
				rpc.failMethod = "eth_getTransactionReceipt"
			case "wrong-read":
				rpc.wrong = true
			case "wrong-broadcast":
				rpc.wrongBroadcast = true
			}
			if err := s.RefreshFunding(ctx); err == nil {
				t.Fatal("refresh accepted an unverifiable receipt")
			}
			if _, err := s.Fund(ctx, "second-refill", "10", minerKey); err == nil {
				t.Fatal("new refill accepted despite RPC failure")
			}
			old, err := s.job("f_" + first.ID)
			if err != nil || old.Status != "pending" || len(rpc.sent) != 1 {
				t.Fatalf("old refill: %+v %v", old, err)
			}
		})
	}
}

func TestConcurrentFundingAfterConfirmationKeepsOneNonceOwner(t *testing.T) {
	s, rpc, dir := setup(t)
	ctx := context.Background()
	if _, err := s.Fund(ctx, "first-refill", "1", minerKey); err != nil {
		t.Fatal(err)
	}
	rpc.mine(rpc.sent[0], "0x1")
	other, err := Open(dir, s.config, rpc)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for i, service := range []*Service{s, other} {
		wg.Add(1)
		go func(i int, service *Service) {
			defer wg.Done()
			id := []string{"next-refill-a", "next-refill-b"}[i]
			_, err := service.Fund(ctx, id, "10", minerKey)
			results <- err
		}(i, service)
	}
	wg.Wait()
	close(results)
	accepted := 0
	for err := range results {
		if err == nil {
			accepted++
		} else if err.Error() != "FUNDING_PENDING" {
			t.Fatal(err)
		}
	}
	if accepted != 1 || len(rpc.sent) != 2 || rpc.sent[1].Nonce() != 1 {
		t.Fatal("concurrent commands reused a nonce")
	}
}
