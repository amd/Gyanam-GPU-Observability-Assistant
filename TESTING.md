# Testing

Unit and API-route tests for the collector/api code base live under
`collector/tests/` and run with `pytest` (+ `pytest-asyncio`, `pytest-cov`,
`pytest-httpx`).

## Running

```bash
./scripts/run-tests.sh                     # full suite + coverage summary
./scripts/run-tests.sh tests/test_config.py    # a single file
./scripts/run-tests.sh -k dedup            # by keyword
```

`run-tests.sh` executes the suite **inside the `gyanam-collector` image** so the
exact runtime dependencies are used, installing the dev-only test deps
(`pytest-cov`, `pytest-httpx`) into the ephemeral container. Behind a
TLS-intercepting proxy, set `PIP_TRUSTED_HOST=1` (in `.env` or the environment)
so the dev-dep install can reach PyPI.

To run locally instead of via Docker:

```bash
cd collector
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. python -m pytest
```

## Layout

| File | Covers |
|------|--------|
| `test_alert_parsing.py` | timestamp parser, severity normalization/filtering, baseline member ordering |
| `test_alert_repository.py` | alert store: dedup, `occurred_at` ordering/window, count/grouped-count, pagination, deferred vs. `include_raw`, cursors, bulk delete |
| `test_extractor.py` / `test_discovery.py` / `test_schema.py` | schema-based extraction, auto-discovery, schema loading |
| `test_redfish_log_parser.py` / `test_unpacker.py` | log-block parsing, blob extraction + path-traversal / size limits |
| `test_config.py` | YAML load, env overrides, alert defaults |
| `test_csrf.py` / `test_auth.py` | CSRF tokens; password hashing, session cookies, request typing |
| `test_exporters.py` / `test_influxdb_exporter.py` | InfluxDB point formatting; buffering, cap/drop, reconnect, health |
| `test_client.py` | Redfish client pure helpers (auth headers, attachment URI) |
| `test_alert_subscriber_more.py` | SSE error classification, backoff, event parsing |
| `test_validators.py` / `test_log_collector.py` | target name/host (SSRF) validators; filename sanitization + delete path-traversal |
| `test_routes_*.py` | FastAPI routes: auth enforcement, CSRF, alerts, targets CRUD + bulk CSV import, health |

## Conventions

- `asyncio_mode = auto` (`collector/pytest.ini`) — async tests need no explicit
  marker.
- Tests run against **SQLite** (throwaway temp DBs via the `repo` fixture); no
  PostgreSQL or live InfluxDB/BMC is required. HTTP is mocked (`pytest-httpx`)
  or exercised in-process (`httpx.ASGITransport`).
- Route tests use `client` (authenticated) / `noauth_client` fixtures with the
  app wired to a temp repository (see `conftest.py`).

## Coverage

`run-tests.sh` prints a coverage summary and enforces **`--cov-fail-under=95`**
(on full runs; subset runs skip the gate). Current overall coverage is **~96%**
across 1000+ tests — the run fails if coverage drops below 95%, so new code must
land with tests.

Well-covered: parser (extractor/discovery/schema/log-parser/unpacker), config,
auth/CSRF, the InfluxDB exporter (buffering/flush/reconnect), Redfish client
(helpers + HTTP task flow), metric-report discovery, inventory collection/
enrichment, the policy engine, webhook subscriber + auto-retry, baseline log
pull, alert repository + manager + subscriber, validators, log-collector, the
collector health app + webhook receiver, poller helpers, the Data Hall heatmap
cache/snapshot, and the API routes (auth, CSRF, targets CRUD + bulk import,
alerts, logs, schemas, health, Redfish PolicyService, login/pages).

**Harder to cover** — the async orchestration loops that are costly/brittle to
test in isolation (`poller` scheduling loop, `sse_subscriber` manager,
`alert_subscriber` reconnect loop, `influxdb` reconnect/flush-loop internals, and
the `collector_main`/`api_main` lifespans) are exercised by the live smoke suite
(`scripts/smoke-test.sh`) against the real stack rather than mocked.
