# The hosted demo: FalkorDB and the Ariadne server in one container.
#
# One container rather than two services because free hosting tiers run one process group with
# no private network or persistent disk. The graph is seeded and verified at *build* time and
# shipped in the image as a FalkorDB snapshot, so a cold start only loads it — on a free host's
# fraction of a CPU, seeding at boot cost about a minute. Nothing is persisted at runtime: every
# restart is the same deterministic world the committed measurements were taken on.
#
# Same FalkorDB image, pinned by the same digest, as docker-compose.yml (a multi-arch index, so
# it builds on amd64 hosts and arm64 laptops alike).
FROM falkordb/falkordb@sha256:107455e862a824e45e8e9f5a7fbfdf5e00a08b14e13eae2577c72fd9e3f59ba6

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

# The query timeout is raised from the image's 1s: a free host's fraction of a CPU makes a cold
# traversal slower than a laptop's. The FalkorDB Browser is not run; only the demo listens.
ENV BROWSER=0 \
    FALKORDB_ARGS="MAX_QUEUED_QUERIES 25 TIMEOUT 15000 RESULTSET_SIZE 10000" \
    FALKORDB_HOST=127.0.0.1 \
    FALKORDB_PORT=6379 \
    ARIADNE_HOST=0.0.0.0 \
    ARIADNE_PUBLIC=1 \
    PORT=8000

WORKDIR /app

# Dependencies only, read from pyproject.toml so there is one list. The package itself is run
# from /app rather than installed: the server finds web/ and docs/measurements/ relative to its
# own source file, which an install into site-packages would move away from them.
COPY pyproject.toml ./
RUN python3 -m venv /venv \
 && python3 -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY ariadne ./ariadne
COPY web ./web
COPY fixtures ./fixtures
COPY docs/measurements ./docs/measurements
RUN /venv/bin/python -m compileall -q ariadne

# Seed, verify, snapshot. A build whose graph fails verification fails here, not in front of a
# judge.
RUN mkdir -p /data \
 && redis-server --bind 127.0.0.1 --port 6379 --dir /data --save "" --daemonize yes \
        --loadmodule "${FALKORDB_BIN_PATH}/falkordb.so" ${FALKORDB_ARGS} \
 && until redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.2; done \
 && /venv/bin/python -m ariadne.seed \
 && /venv/bin/python -m ariadne.verify \
 && redis-cli SAVE \
 && redis-cli shutdown nosave \
 && test -s /data/dump.rdb

COPY deploy/start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 8000
ENTRYPOINT ["/start.sh"]
