package faucet

import (
	"context"
	"crypto/ecdsa"
	"database/sql"
	"encoding/hex"
	"errors"
	"log"
	"math/big"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
)

// Status is a cached operational snapshot, not a promise that a claim will be admitted.
type Status struct {
	Enabled         bool   `json:"enabled"`
	Status          string `json:"status"`
	Address         string `json:"address"`
	ChainID         string `json:"chain_id"`
	ClaimAmount     string `json:"claim_amount"`
	DailyBudget     string `json:"daily_budget"`
	ReservedAtoms   string `json:"reserved_atoms"`
	BalanceAtoms    string `json:"balance_atoms"`
	CooldownSeconds int64  `json:"cooldown_seconds"`
	Confirmations   uint64 `json:"confirmations"`
	UpdatedAt       int64  `json:"updated_at"`
}

// Service owns one faucet wallet, one durable ledger and one serialized sending worker.
type Service struct {
	config Config
	db     *sql.DB
	key    *ecdsa.PrivateKey
	rpc    Chain
	now    func() time.Time
	mu     sync.RWMutex
	state  Status
	active chan struct{}
}

// Open binds storage to the wallet and chain; missing keys are never regenerated over an existing ledger.
func Open(dir string, c Config, rpc Chain) (*Service, error) {
	if err := c.validate(); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, errors.New("STORAGE_UNAVAILABLE")
	}
	key, err := wallet(dir)
	if err != nil {
		return nil, err
	}
	db, err := openDB(dir, c, crypto.PubkeyToAddress(key.PublicKey).Hex())
	if err != nil {
		return nil, errors.New("STORAGE_IDENTITY_OR_IO_ERROR")
	}
	s := &Service{config: c, db: db, key: key, rpc: rpc, now: time.Now, active: make(chan struct{}, 32)}
	s.state = Status{Enabled: true, Status: "STARTING", Address: s.sender(), ChainID: c.ChainID, ClaimAmount: c.ClaimAmount, DailyBudget: c.DailyBudget, CooldownSeconds: c.CooldownSeconds, Confirmations: c.Confirmations}
	return s, nil
}

// Close releases storage; the caller must first stop the HTTP server and worker.
func (s *Service) Close() error     { return s.db.Close() }
func (s *Service) sender() string   { return crypto.PubkeyToAddress(s.key.PublicKey).Hex() }
func (s *Service) snapshot() Status { s.mu.RLock(); defer s.mu.RUnlock(); return s.state }

func (s *Service) setStatus(status, balance, reserved string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state.Status != status {
		log.Printf("Faucet state: %s", status)
	}
	s.state.Status = status
	s.state.UpdatedAt = s.now().Unix()
	if balance != "" {
		s.state.BalanceAtoms = balance
	}
	if reserved != "" {
		s.state.ReservedAtoms = reserved
	}
}

// Refresh verifies identity and reports policy/funding availability without signing.
func (s *Service) Refresh(ctx context.Context) error {
	if err := identity(ctx, s.rpc, s.config); err != nil {
		return err
	}
	balance, err := number(ctx, s.rpc, "eth_getBalance", s.sender(), "pending")
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	total, err := used(tx, s.now().UTC().Format("2006-01-02"))
	tx.Rollback()
	if err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	status := "ready"
	gasReserve, _ := Units(s.config.GasReserve, 18)
	if balance.Cmp(new(big.Int).Add(s.config.reserve(), gasReserve)) < 0 {
		status = "INSUFFICIENT_FUNDS"
	}
	budget, _ := Units(s.config.DailyBudget, 18)
	if new(big.Int).Add(total, s.config.reserve()).Cmp(budget) > 0 {
		status = "DAILY_BUDGET_EXHAUSTED"
	}
	s.setStatus(status, balance.String(), total.String())
	return nil
}

// Step handles at most one claim, always persisting signed bytes before their first broadcast.
func (s *Service) Step(ctx context.Context) error {
	j, err := scanJob(s.db.QueryRow("SELECT " + jobColumns + " FROM jobs WHERE kind='claim' AND status IN ('queued','pending','confirming') ORDER BY CASE WHEN raw='' THEN 1 ELSE 0 END,created,id LIMIT 1"))
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	if err = identity(ctx, s.rpc, s.config); err != nil {
		return err
	}
	if j.Raw == "" {
		to, _ := address(j.Address)
		amount, _ := new(big.Int).SetString(j.Amount, 10)
		signed, err := signedTransfer(ctx, s.rpc, s.config, s.key, to, amount)
		if err != nil {
			if err.Error() == "RECIPIENT_NOT_EOA" {
				_, e := s.db.Exec("UPDATE jobs SET status='rejected' WHERE id=?", j.ID)
				return e
			}
			return err
		}
		// This wallet has one nonce owner. A gap indicates outside spending or an
		// out-of-date restored ledger; do not silently pay again from a newer nonce.
		var previous string
		expected := new(big.Int)
		err = s.db.QueryRow("SELECT nonce FROM jobs WHERE sender=? AND nonce IS NOT NULL ORDER BY length(nonce) DESC,nonce DESC LIMIT 1", s.sender()).Scan(&previous)
		if err == nil {
			if _, ok := expected.SetString(previous, 10); !ok {
				return errors.New("LEDGER_INVALID")
			}
			expected.Add(expected, big.NewInt(1))
		} else if !errors.Is(err, sql.ErrNoRows) {
			return errors.New("STORAGE_UNAVAILABLE")
		}
		if expected.Cmp(new(big.Int).SetUint64(signed.Nonce())) != 0 {
			return errors.New("NONCE_CONFLICT")
		}
		reserved, _ := new(big.Int).SetString(j.Reserved, 10)
		if new(big.Int).Add(amount, new(big.Int).Mul(signed.GasPrice(), new(big.Int).SetUint64(signed.Gas()))).Cmp(reserved) > 0 {
			return errors.New("RESERVATION_TOO_SMALL")
		}
		raw, err := signed.MarshalBinary()
		if err != nil {
			return errors.New("SIGNING_FAILED")
		}
		j.Raw = "0x" + hex.EncodeToString(raw)
		j.Hash = signed.Hash().Hex()
		j.Nonce = sql.NullString{String: nonceText(signed.Nonce()), Valid: true}
		j.Status = "pending"
		// The unique sender/nonce index also halts if a deep reorg would reuse a recorded nonce.
		if _, err = s.db.Exec("UPDATE jobs SET raw=?,hash=?,nonce=?,status=? WHERE id=? AND raw=''", j.Raw, j.Hash, j.Nonce, j.Status, j.ID); err != nil {
			return errors.New("NONCE_OR_STORAGE_CONFLICT")
		}
	}
	return s.resume(ctx, j)
}

func (s *Service) resume(ctx context.Context, j Job) error {
	state, err := receiptState(ctx, s.rpc, j.Hash, s.config.Confirmations)
	if err != nil {
		return err
	}
	// Carry the charge into the UTC day when a pending transfer settles. Otherwise
	// yesterday's queue could complete today and then free today's entire budget.
	if _, err = s.db.Exec("UPDATE jobs SET status=?,day=CASE WHEN ? IN ('confirmed','failed') THEN ? ELSE day END WHERE id=?", state, state, s.now().UTC().Format("2006-01-02"), j.ID); err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	if state == "confirmed" || state == "failed" || state == "confirming" {
		return nil
	}
	// Validate the durable record before retransmitting; never allocate a fresh nonce on uncertainty.
	var tx types.Transaction
	data, err := hex.DecodeString(strings.TrimPrefix(j.Raw, "0x"))
	if err != nil || tx.UnmarshalBinary(data) != nil || tx.Hash().Hex() != j.Hash {
		return errors.New("LEDGER_INVALID")
	}
	chainID, _ := new(big.Int).SetString(s.config.ChainID, 10)
	from, err := types.Sender(types.NewEIP155Signer(chainID), &tx)
	if err != nil || from.Hex() != j.Sender || tx.To() == nil || tx.To().Hex() != j.Address || tx.Value().String() != j.Amount || tx.ChainId().Cmp(chainID) != 0 {
		return errors.New("LEDGER_INVALID")
	}
	latest, err := number(ctx, s.rpc, "eth_getTransactionCount", j.Sender, "latest")
	if err != nil {
		return err
	}
	if latest.Cmp(new(big.Int).SetUint64(tx.Nonce())) > 0 {
		return errors.New("NONCE_CONFLICT")
	}
	// A known pending transaction should wait for its receipt. Repeatedly sending
	// it would turn the node's normal "already known" response into a false outage.
	var known *struct {
		Hash string `json:"hash"`
	}
	if err := s.rpc.Call(ctx, true, "eth_getTransactionByHash", []any{j.Hash}, &known); err != nil {
		return err
	}
	if known != nil {
		if !strings.EqualFold(known.Hash, j.Hash) {
			return errors.New("RPC_INVALID_RESPONSE")
		}
		return nil
	}
	return broadcast(ctx, s.rpc, s.config, j)
}

// Run is the sole claim worker. Funding is explicit and never uses the miner key in this process.
func (s *Service) Run(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		cycle, cancel := context.WithTimeout(ctx, 55*time.Second)
		err := s.Refresh(cycle)
		if err == nil {
			err = s.Step(cycle)
		}
		if err != nil {
			s.setStatus(safeCode(err), "", "")
		}
		cancel()
		s.pruneRates()
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// Fund signs a single operator-requested refill, or reuses the exact journaled transaction for this ID.
func (s *Service) Fund(ctx context.Context, id, amountText, keyText string) (Job, error) {
	if !fundingID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	amount, err := Units(amountText, 18)
	if err != nil {
		return Job{}, err
	}
	key, err := crypto.HexToECDSA(strings.TrimPrefix(strings.TrimSpace(keyText), "0x"))
	if err != nil {
		return Job{}, errors.New("INVALID_PRIVATE_KEY")
	}
	defer key.D.SetInt64(0)
	sender := crypto.PubkeyToAddress(key.PublicKey).Hex()
	if sender == s.sender() {
		return Job{}, errors.New("FUNDING_REQUIRES_DIFFERENT_ACCOUNT")
	}
	id = "f_" + id
	if old, e := s.job(id); e == nil {
		if old.Sender != sender || old.Amount != amount.String() {
			return Job{}, errors.New("REQUEST_ID_CONFLICT")
		}
		return s.RetryFund(ctx, strings.TrimPrefix(id, "f_"))
	} else if !errors.Is(e, sql.ErrNoRows) {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	to, _ := address(s.sender())
	signed, err := signedTransfer(ctx, s.rpc, s.config, key, to, amount)
	if err != nil {
		return Job{}, err
	}
	raw, err := signed.MarshalBinary()
	if err != nil {
		return Job{}, errors.New("SIGNING_FAILED")
	}
	j := Job{ID: id, Kind: "fund", Address: s.sender(), Sender: sender, Amount: amount.String(), Reserved: "0", Day: s.now().UTC().Format("2006-01-02"), Created: s.now().Unix(), Status: "pending", Raw: "0x" + hex.EncodeToString(raw), Hash: signed.Hash().Hex(), Nonce: sql.NullString{String: nonceText(signed.Nonce()), Valid: true}}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	defer tx.Rollback()
	var active int
	if err = tx.QueryRow("SELECT COUNT(*) FROM jobs WHERE kind='fund' AND sender=? AND status NOT IN ('confirmed','failed')", sender).Scan(&active); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	if active > 0 {
		return Job{}, errors.New("FUNDING_PENDING_USE_RETRY")
	}
	if err = insertJob(tx, j); err != nil {
		return Job{}, errors.New("NONCE_OR_REQUEST_CONFLICT")
	}
	if err = tx.Commit(); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	return s.RetryFund(ctx, strings.TrimPrefix(id, "f_"))
}

// RetryFund reconciles or rebroadcasts saved funding bytes without asking for the miner key again.
func (s *Service) RetryFund(ctx context.Context, id string) (Job, error) {
	if !fundingID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	j, err := s.job("f_" + id)
	if err != nil {
		return Job{}, errors.New("FUNDING_NOT_FOUND")
	}
	if err = identity(ctx, s.rpc, s.config); err != nil {
		return j, err
	}
	err = s.resume(ctx, j)
	if current, e := s.job(j.ID); e == nil {
		j = current
	}
	j.ID = id
	return j, err
}
