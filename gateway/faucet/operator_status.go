package faucet

import (
	"context"
	"errors"
	"math/big"
	"strings"
	"time"
)

const (
	// DefaultStatusLimit keeps operator output bounded without hiding recent activity.
	DefaultStatusLimit = 20
	// MaxStatusLimit bounds ledger reads even when the caller bypasses the Python CLI.
	MaxStatusLimit = 100
)

type operatorJob struct {
	OperatorReceipt
	AmountUSDB   string `json:"amount_usdb"`
	CreatedAtUTC string `json:"created_at_utc"`
}

type operatorWorker struct {
	Status
	BalanceUSDB  string `json:"balance_usdb,omitempty"`
	ReservedUSDB string `json:"reserved_usdb,omitempty"`
	UpdatedAtUTC string `json:"updated_at_utc,omitempty"`
}

// formatAtoms renders native-token amounts exactly, including zero and one atom.
func formatAtoms(raw string) (string, error) {
	n, ok := new(big.Int).SetString(raw, 10)
	if !ok || n.Sign() < 0 || n.BitLen() > 256 {
		return "", errors.New("LEDGER_INVALID")
	}
	digits := n.String()
	if len(digits) <= 18 {
		digits = strings.Repeat("0", 19-len(digits)) + digits
	}
	split := len(digits) - 18
	fraction := strings.TrimRight(digits[split:], "0")
	if fraction == "" {
		return digits[:split], nil
	}
	return digits[:split] + "." + fraction, nil
}

// OperatorStatus combines the actual worker state, a fresh chain observation and
// bounded local history. Reading status never signs, broadcasts or updates jobs.
func (s *Service) OperatorStatus(ctx context.Context, live Status, limit int) (map[string]any, error) {
	if limit < 1 || limit > MaxStatusLimit {
		return nil, errors.New("INVALID_LIMIT")
	}
	worker := operatorWorker{Status: live}
	if live.UpdatedAt > 0 {
		worker.UpdatedAtUTC = time.Unix(live.UpdatedAt, 0).UTC().Format(time.RFC3339)
	}
	var err error
	if live.BalanceAtoms != "" {
		worker.BalanceUSDB, err = formatAtoms(live.BalanceAtoms)
		if err != nil {
			return nil, err
		}
	}
	if live.ReservedAtoms != "" {
		worker.ReservedUSDB, err = formatAtoms(live.ReservedAtoms)
		if err != nil {
			return nil, err
		}
	}
	funding, fundingMore, err := s.recentJobs(ctx, "fund", limit)
	if err != nil {
		return nil, err
	}
	claims, claimsMore, err := s.recentJobs(ctx, "claim", limit)
	if err != nil {
		return nil, err
	}
	transfers, transfersMore, err := s.recentJobs(ctx, "transfer", limit)
	if err != nil {
		return nil, err
	}
	// Report RPC failures alongside the ledger, not as a fabricated zero balance
	// or a replacement for the running worker's health (for example NONCE_CONFLICT).
	chainCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	chain, err := s.chainSnapshot(chainCtx)
	if err != nil {
		chain = map[string]any{"status": "unavailable", "error": safeCode(err)}
	}
	chain["checked_at"] = s.now().Unix()
	chain["checked_at_utc"] = s.now().UTC().Format(time.RFC3339)
	return map[string]any{
		"faucet": worker, "chain": chain, "funding": funding, "claims": claims, "transfers": transfers,
		"history": map[string]any{"limit": limit, "funding_has_more": fundingMore, "claims_has_more": claimsMore, "transfers_has_more": transfersMore},
	}, nil
}

func (s *Service) recentJobs(ctx context.Context, kind string, limit int) ([]operatorJob, bool, error) {
	// Row ID breaks same-second ties by insertion order; LIMIT also bounds SQL
	// work returned to the process instead of loading the entire ledger first.
	predicate := "kind=?"
	args := []any{kind, limit + 1}
	if kind == "transfer" {
		predicate = "kind IN ('miner_transfer','faucet_transfer')"
		args = []any{limit + 1}
	}
	rows, err := s.db.QueryContext(ctx, "SELECT "+jobColumns+" FROM jobs WHERE "+predicate+" ORDER BY created DESC,rowid DESC LIMIT ?", args...)
	if err != nil {
		return nil, false, errors.New("STORAGE_UNAVAILABLE")
	}
	defer rows.Close()
	jobs := []operatorJob{}
	for rows.Next() {
		if len(jobs) == limit {
			return jobs, true, nil
		}
		j, err := scanJob(rows)
		if err != nil {
			return nil, false, errors.New("STORAGE_UNAVAILABLE")
		}
		if j.Kind == "fund" {
			j.ID = strings.TrimPrefix(j.ID, "f_")
		}
		if j.Kind == "miner_transfer" || j.Kind == "faucet_transfer" {
			j.ID = strings.TrimPrefix(j.ID, "t_")
		}
		amount, err := formatAtoms(j.Amount)
		if err != nil {
			return nil, false, err
		}
		jobs = append(jobs, operatorJob{OperatorReceipt: DescribeJob(j), AmountUSDB: amount, CreatedAtUTC: time.Unix(j.Created, 0).UTC().Format(time.RFC3339)})
	}
	if rows.Err() != nil {
		return nil, false, errors.New("STORAGE_UNAVAILABLE")
	}
	return jobs, false, nil
}

func (s *Service) chainSnapshot(ctx context.Context) (map[string]any, error) {
	if err := identity(ctx, s.rpc, s.config); err != nil {
		return nil, err
	}
	var head *struct {
		Hash   string `json:"hash"`
		Number string `json:"number"`
	}
	if err := s.rpc.Call(ctx, false, "eth_getBlockByNumber", []any{"latest", false}, &head); err != nil {
		return nil, err
	}
	if head == nil || !hashPattern.MatchString(head.Hash) {
		return nil, errors.New("RPC_INVALID_RESPONSE")
	}
	height, err := quantity(head.Number)
	if err != nil || !height.IsUint64() {
		return nil, errors.New("RPC_INVALID_RESPONSE")
	}
	// Pin mined-state reads to one height, then check the hash again so a reorg
	// during the query cannot mix two different chain views into a healthy report.
	balance, err := number(ctx, s.rpc, "eth_getBalance", s.sender(), head.Number)
	if err != nil {
		return nil, err
	}
	nonce, err := number(ctx, s.rpc, "eth_getTransactionCount", s.sender(), head.Number)
	if err != nil {
		return nil, err
	}
	pendingBalance, err := number(ctx, s.rpc, "eth_getBalance", s.sender(), "pending")
	if err != nil {
		return nil, err
	}
	pendingNonce, err := number(ctx, s.rpc, "eth_getTransactionCount", s.sender(), "pending")
	if err != nil {
		return nil, err
	}
	var canonical *struct {
		Hash string `json:"hash"`
	}
	if err := s.rpc.Call(ctx, false, "eth_getBlockByNumber", []any{head.Number, false}, &canonical); err != nil {
		return nil, err
	}
	if canonical == nil || !strings.EqualFold(canonical.Hash, head.Hash) {
		return nil, errors.New("CHAIN_CHANGED")
	}
	balanceUSDB, _ := formatAtoms(balance.String())
	pendingUSDB, _ := formatAtoms(pendingBalance.String())
	return map[string]any{
		"status": "ok", "block_number": height.Uint64(), "block_hash": head.Hash,
		"balance_atoms": balance.String(), "balance_usdb": balanceUSDB,
		"pending_balance_atoms": pendingBalance.String(), "pending_balance_usdb": pendingUSDB,
		"nonce": nonce.String(), "pending_nonce": pendingNonce.String(),
	}, nil
}
