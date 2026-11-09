#!/bin/sh
# Own the shared socket for the entire Nginx lifetime, including worker restarts.
set -eu

socket=/run/usdb-rpc/upstream.sock
exec 9>/run/usdb-rpc/owner.lock
if ! flock -n 9; then
    echo 'RPC socket is already owned; refusing to start another host relay' >&2
    exit 1
fi

if [ -L "$socket" ] || { [ -e "$socket" ] && [ ! -S "$socket" ]; }; then
    echo 'RPC socket path is not a socket; refusing to remove it' >&2
    exit 1
fi
if [ -S "$socket" ]; then
    # An older relay may not hold the lock. Only a failed connection permits cleanup;
    # any HTTP response (even 404) or timeout could belong to a live socket owner.
    result=0
    curl --noproxy '*' --silent --max-time 2 --unix-socket "$socket" \
        --output /dev/null http://localhost/healthz || result=$?
    if [ "$result" -ne 7 ]; then
        echo 'RPC socket may still be active; refusing to remove it' >&2
        exit 1
    fi
    rm -- "$socket"
    echo 'Removed stale RPC socket after a failed connection; starting host relay' >&2
fi

# Keep the lock descriptor open in the foreground master process.
exec nginx -g 'daemon off;'
