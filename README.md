# DeckSupreme

Weekly ELT pipeline that ingests battle logs for the top 1,000 Clash Royale
players, lands raw JSON in MinIO, models it in PostgreSQL, computes a
multi-factor card ranking, and commits the results to GitHub.

> **Latest card rankings:** [`rankings/README.md`](rankings/README.md) (JSON: `rankings/latest.json`).

## Stack

| Service             | Image                         | Host port   | Role                                   |
|---------------------|-------------------------------|-------------|----------------------------------------|
| `postgres`          | `postgres:16`                 | 5432        | Warehouse DB + Airflow metadata DB     |
| `minio`             | `minio/minio` (pinned)        | 9000 / 9001 | Raw data lake (API / console)          |
| `minio-init`        | same as `minio`               | —           | Creates `decksupreme-raw` bucket       |
| `airflow-init`      | `decksupreme-airflow` (built) | —           | `db migrate` + admin user              |
| `airflow-webserver` | `decksupreme-airflow`         | 8080        | UI                                     |
| `airflow-scheduler` | `decksupreme-airflow`         | —           | LocalExecutor                          |

## Quick start

```bash
make env      # creates .env with generated secrets (prints the Airflow login)
#   then fill in CLASH_ROYALE_API_KEY, GITHUB_TOKEN, GITHUB_REPO
make up       # builds the Airflow image, starts everything, waits for healthy
make verify   # end-to-end check: health, DBs, bucket, DAG imports, Airflow→Postgres/MinIO
```

- Airflow UI: http://localhost:8080
- MinIO console: http://localhost:9001

`make down` stops the stack and keeps data; `make nuke` deletes the volumes too.
Postgres init scripts only run on an **empty** volume — after changing
`AIRFLOW_DB_*` values, run `make nuke` before `make up`.

## Warehouse & card seed

```bash
make up                 # recreates Airflow containers with the data/ and scripts/ mounts
make seed               # validate data/cards_seed.json → create tables → upsert dim_cards
make seed args=--reconcile   # also check ids/names/elixir against GET /v1/cards
make seed args=--dry-run     # validate only
```

Host-side tests (Python 3.11 venv, `pip install -r requirements-dev.txt`):

```bash
make test               # unit: schema DDL, seed validation, reconcile logic
make test-integration   # creates a throwaway DB on the stack's Postgres, then drops it
```

`data/cards_seed.json` holds level-11 stats compiled by hand, not exported from
game files. Treat it as reviewable data: run `--reconcile` once your API key is
set, and spot-check HP/DPS for cards that matter. Because every metric is
min-max scaled, only *consistency* across cards affects rankings — the absolute
level does not.

## Extraction (Task 1)

```bash
make extract args="--limit 20"   # smoke run
make extract                     # top 1,000 → s3://decksupreme-raw/year=YYYY/week=WW/raw_battles.json
make extract args="--limit 20 --no-upload --out /tmp/raw.json"
```

- One shared token bucket keeps all requests at or below `CLASH_ROYALE_MAX_RPS` (default 30, hard cap 30).
- 429/5xx/timeouts are retried with exponential backoff and jitter, honouring `Retry-After`.
- A player whose battle log still fails is logged and listed under `failures` in the
  payload; the run fails only if every player fails. 401/403 aborts immediately (bad key or IP).
- The object is keyed by ISO year and week, so a rerun in the same week overwrites it.
  It carries `record-count` / `extracted-at` as object metadata and tags.
- Airflow entry point: `decksupreme.extract.pipeline.run_extraction()` returns an XCom-safe summary.

## Transform & load (Tasks 2–3)

```bash
make load                              # current ISO week
make load args="--year 2026 --week 45"
```

- **Filter:** keeps 1v1 collection-deck games in Path of Legend (`Ranked1v1*`) and Trophy Road
  (`Ladder`). Friendlies, clan-mate games, 2v2, tournaments, river race, challenges and draft
  modes are dropped. The allowlist is in `transform/parser.py`.
- **Dedup:** `match_id = sha256(min_tag:max_tag:battleTime)`. A match from both players' logs
  keeps the higher-ranked player's copy, so the result doesn't depend on input order.
- **Cards:** checked against `dim_cards` by id. Cards the seed doesn't have get placeholder rows
  (empty stats, never overwriting seeded rows). Name mismatches are reported in the run stats.
- `week_id` is the snapshot week (the raw object's year/week), not each battle's calendar week.
- **Load:** everything goes in one transaction with chunked `INSERT … ON CONFLICT`, so a rerun
  of the same week inserts 0 rows.
- `dim_cards.elixir` is nullable (Mirror has no fixed cost). On a warehouse created before
  Phase 4, run once: `ALTER TABLE dim_cards ALTER COLUMN elixir DROP NOT NULL;`

## Scoring & artifacts (Tasks 4–5)

```bash
make rank                              # current ISO week
make rank args="--year 2026 --week 45"
```

- **Per card, for the snapshot week:** win rate, usage rate (share of decks),
  APE = `base_dps × unit_count / elixir`, DPE = `base_hp × unit_count / elixir`, and speed score.
  The seed stores per-unit stats, so the formulas multiply by unit count.
- **Score:** each feature is min-max scaled over active cards (a constant feature scales to 0),
  then weighted by `SCORING_WEIGHTS` (default `wr=0.35,ur=0.25,ape=0.15,dpe=0.15,ss=0.10`).
- **Only eligible cards are ranked:** every stat known and at least `SCORING_MIN_MATCHES` deck
  appearances (default 100). Everything else is listed under **Unranked** with a reason:
  `stats_pending` (in the seed without stats), `variable_cost` (Mirror, never ranked) or
  `below_min_matches`. Unranked cards don't affect the min-max scaling.
- **Ties** break by WR → APE → DPE → speed → UR (all desc), then name (asc).
- **Output:** `rankings/latest.json`, `rankings/rankings_YYYY_WW.json` and `rankings/README.md`.
  Risers/fallers compare against the previous ISO week's archive when it exists.

## Weekly DAG & GitHub publishing (Task 6)

`dags/weekly_ranking_dag.py` (`decksupreme_weekly_rankings`) runs **Mondays 06:00 UTC**:
`extract → transform_load → score → publish`. After the load, a report-only `check_cards`
task compares `dim_cards` with `GET /v1/cards` and logs new cards, stats still missing from the
seed, cost changes and renames. It never fails the run; you can also run `make check-cards`.
The API has no HP/DPS, so new cards' stats still have to be added to `data/cards_seed.json`
by hand (then `make seed`).

```bash
make pipeline                          # unpause + trigger a run now (watch it at :8080)
make publish args="--year 2026 --week 45"   # just the GitHub step
```

- **Reruns are safe:** each task is idempotent for its week. The ISO year/week comes from the
  extract task through XCom, so all four tasks agree on it.
- **Retries:** 2, with exponential backoff, and each task has a timeout. A bad API key or an
  IP that isn't whitelisted fails at once instead of retrying.
- **Publishing:** `rankings/latest.json`, `rankings/rankings_YYYY_WW.json` and
  `rankings/README.md` go in one commit through the GitHub REST API, with the message
  `ci(data): update weekly card rankings [YYYY-WW]`. If the files are unchanged, no commit is
  made. If the branch moves mid-publish, the publish retries on the new head.
- **Without `GITHUB_TOKEN`/`GITHUB_REPO`:** publish logs a warning and reports `skipped`. The
  files stay in `rankings/` and the run still succeeds.
- **Token:** use a fine-grained PAT scoped to `GITHUB_REPO` with **Contents: read & write**,
  and run `make up` after setting it.

## Layout

```
dags/                     Airflow DAG definitions only (thin; logic lives in src/)
src/decksupreme/
  config.py               env-driven settings, same code on host and in containers
  extract/                Task 1  API client, rate limiter, raw → MinIO
  transform/              Task 2  match_id hashing, dedup, mode filtering
  load/                   Task 3  star-schema upserts
  scoring/                Task 4  features, min-max, weighted index, tie-breaks
  publish/                Task 5-6 JSON/README rendering, GitHub commit
  db/                     SQLAlchemy models + engine helpers
migrations/               Alembic (warehouse schema)
rankings/                 generated artifacts (latest.json, rankings_YYYY_WW.json)
docker/                   Airflow Dockerfile, Postgres init scripts
scripts/                  bootstrap_env.sh, verify_stack.sh
tests/unit, tests/integration
```

## Notes

- **Clash Royale API keys are IP-bound.** Whitelist your current public IP at
  developer.clashroyale.com; if your ISP rotates it, requests return 403.
- Airflow 2.x pins **SQLAlchemy 1.4**; app DB code targets that API.
- Connections are injected as env vars: `decksupreme_warehouse` (Postgres)
  and `minio_s3` (AWS-type, endpoint `http://minio:9000`).
