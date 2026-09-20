// Command faucet runs the isolated faucet worker or a local operator command.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/buckyos/usdb-explorer/gateway/faucet"
)

func run() error {
	if len(os.Args) < 2 {
		return errors.New("expected serve, status, fund or retry")
	}
	action := os.Args[1]
	flags := flag.NewFlagSet(action, flag.ContinueOnError)
	id := flags.String("request-id", "", "Funding recovery ID")
	amount := flags.String("amount", "", "USDB amount")
	if err := flags.Parse(os.Args[2:]); err != nil {
		return errors.New("invalid command options")
	}
	c, err := faucet.ParseConfig(os.Getenv("FAUCET_CONFIG"))
	if err != nil {
		return err
	}
	dir := os.Getenv("FAUCET_DATA_DIR")
	if dir == "" {
		dir = "/data"
	}
	// OS locking prevents accidental second workers even across separate containers.
	if action == "serve" {
		if err := os.MkdirAll(dir, 0700); err != nil {
			return errors.New("STORAGE_UNAVAILABLE")
		}
		lock, err := os.OpenFile(filepath.Join(dir, "worker.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
		if err != nil {
			return errors.New("STORAGE_UNAVAILABLE")
		}
		defer lock.Close()
		if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
			return errors.New("WORKER_ALREADY_RUNNING")
		}
	}
	s, err := faucet.Open(dir, c, faucet.NewRPC(c))
	if err != nil {
		return err
	}
	defer s.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if action == "serve" {
		server := &http.Server{Addr: ":8080", Handler: s, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
		done := make(chan struct{})
		go func() { defer close(done); s.Run(ctx) }()
		go func() {
			<-ctx.Done()
			shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cancel()
			_ = server.Shutdown(shutdown)
		}()
		fmt.Println("Faucet started: dedicated wallet, persistent budgets and transaction recovery enabled")
		err := server.ListenAndServe()
		stop()
		<-done
		if err == http.ErrServerClosed {
			return nil
		}
		return errors.New("faucet HTTP server failed")
	}
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	switch action {
	case "status":
		value, e := s.OperatorStatus()
		if e != nil {
			return e
		}
		// Read the running worker's state: recomputing only its balance here would
		// incorrectly report ready while it is paused on a nonce or broadcast error.
		req, e := http.NewRequestWithContext(ctx, "GET", "http://127.0.0.1:8080/api/faucet/v1/status", nil)
		if e != nil {
			return errors.New("WORKER_UNAVAILABLE")
		}
		req.Header.Set("X-USDB-Faucet-Proxy", c.ProxyToken)
		req.Header.Set("X-Forwarded-For", "127.0.0.1")
		response, e := (&http.Client{Timeout: 5 * time.Second}).Do(req)
		if e != nil {
			return errors.New("WORKER_UNAVAILABLE")
		}
		defer response.Body.Close()
		var live faucet.Status
		if response.StatusCode != 200 || json.NewDecoder(io.LimitReader(response.Body, 65536)).Decode(&live) != nil || !live.Enabled {
			return errors.New("WORKER_UNAVAILABLE")
		}
		value["faucet"] = live
		_ = json.NewEncoder(os.Stdout).Encode(value)
		return nil
	case "fund":
		// The key arrives only on stdin, never in process arguments or container environment.
		key, err := io.ReadAll(io.LimitReader(os.Stdin, 256))
		if err != nil || len(key) > 128 {
			return errors.New("INVALID_PRIVATE_KEY")
		}
		defer clear(key)
		j, err := s.Fund(ctx, *id, *amount, string(key))
		_ = json.NewEncoder(os.Stdout).Encode(j)
		return err
	case "retry":
		j, err := s.RetryFund(ctx, *id)
		_ = json.NewEncoder(os.Stdout).Encode(j)
		return err
	default:
		return errors.New("expected serve, status, fund or retry")
	}
}

func main() {
	syscall.Umask(0077)
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "Faucet operation failed:", faucet.ErrorCode(err))
		os.Exit(1)
	}
}
