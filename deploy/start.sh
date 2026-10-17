#!/bin/sh
# Boot the hosted demo: FalkorDB on loopback only, loading the graph snapshotted at build time,
# then the server. Runtime writes stay in memory and vanish on restart, by design.
set -eu

# shellcheck disable=SC2086
redis-server --bind 127.0.0.1 --port 6379 --protected-mode yes --dir /data \
    --dbfilename dump.rdb --save "" --appendonly no --daemonize yes \
    --loadmodule "${FALKORDB_BIN_PATH}/falkordb.so" ${FALKORDB_ARGS}

# PONG, not just a reply: while the snapshot loads, the server answers LOADING.
i=0
until redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; do
    i=$((i + 1))
    [ "$i" -gt 300 ] && { echo "FalkorDB did not come up"; exit 1; }
    sleep 0.2
done

exec /venv/bin/python -m ariadne.server
