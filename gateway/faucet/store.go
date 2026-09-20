package faucet

import (
	"context"
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/crypto"
	_ "modernc.org/sqlite"
)

// Job is the public receipt of an application. Raw signed bytes never leave the private ledger.
type Job struct {
	ID       string         `json:"id"`
	Kind     string         `json:"-"`
	Address  string         `json:"address"`
	Sender   string         `json:"-"`
	Amount   string         `json:"amount_atoms"`
	Reserved string         `json:"-"`
	IP       string         `json:"-"`
	Day      string         `json:"-"`
	Created  int64          `json:"created_at"`
	Status   string         `json:"status"`
	Raw      string         `json:"-"`
	Hash     string         `json:"transaction_hash,omitempty"`
	Nonce    sql.NullString `json:"-"`
}

const jobColumns = `id,kind,address,sender,amount,reserved,ip,day,created,status,raw,hash,nonce`

type scanner interface{ Scan(...any) error }

func scanJob(row scanner) (Job, error) {
	var j Job
	err := row.Scan(&j.ID, &j.Kind, &j.Address, &j.Sender, &j.Amount, &j.Reserved, &j.IP, &j.Day, &j.Created, &j.Status, &j.Raw, &j.Hash, &j.Nonce)
	return j, err
}

func wallet(dir string) (*ecdsa.PrivateKey, error) {
	path := filepath.Join(dir, "wallet.key")
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		if _, err := os.Stat(filepath.Join(dir, "faucet.sqlite")); !os.IsNotExist(err) {
			return nil, errors.New("WALLET_MISSING_RESTORE_BACKUP")
		}
		key, err := crypto.GenerateKey()
		if err != nil {
			return nil, errors.New("WALLET_GENERATION_FAILED")
		}
		// A crash during creation must fail closed, never silently replace a funded key.
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return nil, errors.New("WALLET_CREATE_FAILED")
		}
		_, err = f.WriteString(hex.EncodeToString(crypto.FromECDSA(key)) + "\n")
		if err == nil {
			err = f.Sync()
		}
		closeErr := f.Close()
		if err != nil || closeErr != nil {
			return nil, errors.New("WALLET_CREATE_FAILED")
		}
		d, err := os.Open(dir)
		if err != nil {
			return nil, errors.New("WALLET_CREATE_FAILED")
		}
		err = d.Sync()
		d.Close()
		if err != nil {
			return nil, errors.New("WALLET_CREATE_FAILED")
		}
		return key, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("WALLET_PERMISSIONS_INVALID")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("WALLET_READ_FAILED")
	}
	key, err := crypto.HexToECDSA(strings.TrimSpace(string(data)))
	clear(data)
	if err != nil {
		return nil, errors.New("WALLET_INVALID")
	}
	return key, nil
}

func openDB(dir string, c Config, sender string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", filepath.Join(dir, "faucet.sqlite")+"?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=synchronous(FULL)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, address TEXT NOT NULL, sender TEXT NOT NULL,
 amount TEXT NOT NULL, reserved TEXT NOT NULL, ip TEXT NOT NULL, day TEXT NOT NULL,
 created INTEGER NOT NULL, status TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL DEFAULT '', nonce TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS sender_nonce ON jobs(sender,nonce) WHERE nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS address_created ON jobs(address,created);
CREATE INDEX IF NOT EXISTS ip_created ON jobs(ip,created);
CREATE INDEX IF NOT EXISTS job_status ON jobs(kind,status,created);
CREATE TABLE IF NOT EXISTS rate_limits (bucket TEXT PRIMARY KEY, minute INTEGER NOT NULL, count INTEGER NOT NULL);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	want := "v1:" + c.ChainID + ":" + strings.ToLower(c.Genesis) + ":" + strings.ToLower(sender)
	_, err = db.Exec("INSERT OR IGNORE INTO identity VALUES (1,?)", want)
	var got string
	if err == nil {
		err = db.QueryRow("SELECT value FROM identity WHERE id=1").Scan(&got)
	}
	if err != nil || got != want {
		db.Close()
		return nil, errors.New("STORAGE_IDENTITY_MISMATCH")
	}
	return db, nil
}

func (s *Service) job(id string) (Job, error) {
	return scanJob(s.db.QueryRow("SELECT "+jobColumns+" FROM jobs WHERE id=?", id))
}

func insertJob(tx *sql.Tx, j Job) error {
	_, err := tx.Exec("INSERT INTO jobs ("+jobColumns+") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", j.ID, j.Kind, j.Address, j.Sender, j.Amount, j.Reserved, j.IP, j.Day, j.Created, j.Status, j.Raw, j.Hash, j.Nonce)
	return err
}

func (s *Service) ipKey(ip string) string {
	key := crypto.FromECDSA(s.key)
	input := append(key, []byte(ip)...)
	sum := sha256.Sum256(input)
	clear(input)
	return hex.EncodeToString(sum[:])
}

func used(tx *sql.Tx, day string) (*big.Int, error) {
	// Pending reservations carry over midnight; a restart or a new UTC day cannot free them.
	rows, err := tx.Query("SELECT reserved FROM jobs WHERE kind='claim' AND (day=? OR status IN ('queued','pending','confirming'))", day)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	total := new(big.Int)
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		n, ok := new(big.Int).SetString(raw, 10)
		if !ok || n.Sign() < 0 {
			return nil, errors.New("LEDGER_INVALID")
		}
		total.Add(total, n)
	}
	return total, rows.Err()
}

func (s *Service) rate(ip, group string, limit int) error {
	minute := s.now().Unix() / 60
	var count int
	err := s.db.QueryRow(`INSERT INTO rate_limits VALUES (?,?,1) ON CONFLICT(bucket) DO UPDATE SET
 count=CASE WHEN minute=excluded.minute THEN count+1 ELSE 1 END, minute=excluded.minute RETURNING count`, s.ipKey(ip)+group, minute).Scan(&count)
	if err != nil {
		return errors.New("STORAGE_UNAVAILABLE")
	}
	if count > limit {
		return errors.New("IP_RATE_LIMIT")
	}
	return nil
}

// Claim atomically records cooldowns, daily reservations and an idempotent request.
func (s *Service) Claim(ctx context.Context, id, recipient, ip string) (Job, error) {
	if !requestID.MatchString(id) {
		return Job{}, errors.New("INVALID_REQUEST_ID")
	}
	to, err := address(recipient)
	if err != nil {
		return Job{}, err
	}
	if to == crypto.PubkeyToAddress(s.key.PublicKey) {
		return Job{}, errors.New("INVALID_ADDRESS")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	defer tx.Rollback()
	id = "c_" + id
	old, err := scanJob(tx.QueryRow("SELECT "+jobColumns+" FROM jobs WHERE id=?", id))
	if err == nil {
		if old.Address != to.Hex() {
			return Job{}, errors.New("REQUEST_ID_CONFLICT")
		}
		return old, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	if state := s.snapshot(); state.Status != "ready" {
		return Job{}, errors.New(state.Status)
	}
	now := s.now()
	ipHash := s.ipKey(ip)
	var last sql.NullInt64
	if err = tx.QueryRow("SELECT MAX(created) FROM jobs WHERE kind='claim' AND address=?", to.Hex()).Scan(&last); err != nil {
		return Job{}, err
	}
	if last.Valid && now.Unix() < last.Int64+s.config.CooldownSeconds {
		return Job{}, errors.New("ADDRESS_COOLDOWN")
	}
	var active, count int
	if err = tx.QueryRow("SELECT COUNT(*) FROM jobs WHERE kind='claim' AND status IN ('queued','pending','confirming') AND address=?", to.Hex()).Scan(&active); err != nil {
		return Job{}, err
	}
	if active > 0 {
		return Job{}, errors.New("ADDRESS_PENDING")
	}
	if err = tx.QueryRow("SELECT COUNT(*) FROM jobs WHERE kind='claim' AND ip=? AND created>?", ipHash, now.Unix()-86400).Scan(&count); err != nil {
		return Job{}, err
	}
	if count >= s.config.IPDailyClaims {
		return Job{}, errors.New("IP_DAILY_LIMIT")
	}
	if err = tx.QueryRow("SELECT COUNT(*) FROM jobs WHERE kind='claim' AND status IN ('queued','pending','confirming')").Scan(&active); err != nil {
		return Job{}, err
	}
	if active >= 100 {
		return Job{}, errors.New("QUEUE_FULL")
	}
	day := now.UTC().Format("2006-01-02")
	total, err := used(tx, day)
	if err != nil {
		return Job{}, err
	}
	reservation := s.config.reserve()
	budget, _ := Units(s.config.DailyBudget, 18)
	if new(big.Int).Add(total, reservation).Cmp(budget) > 0 {
		return Job{}, errors.New("DAILY_BUDGET_EXHAUSTED")
	}
	amount, _ := Units(s.config.ClaimAmount, 18)
	j := Job{ID: id, Kind: "claim", Address: to.Hex(), Sender: s.sender(), Amount: amount.String(), Reserved: reservation.String(), IP: ipHash, Day: day, Created: now.Unix(), Status: "queued"}
	if err = insertJob(tx, j); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	if err = tx.Commit(); err != nil {
		return Job{}, errors.New("STORAGE_UNAVAILABLE")
	}
	return j, nil
}

// NewFundingID supplies an opaque recovery handle before any funding broadcast.
func NewFundingID() (string, error) {
	var b [16]byte
	_, err := rand.Read(b[:])
	return hex.EncodeToString(b[:]), err
}

func (s *Service) pruneRates() {
	_, _ = s.db.Exec("DELETE FROM rate_limits WHERE minute<?", s.now().Add(-time.Hour).Unix()/60)
}
