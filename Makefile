DC := docker compose

.PHONY: help env build up down restart ps logs verify nuke init-db seed extract load rank publish pipeline test test-integration lint psql

help:          ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*## "}{printf "  %-10s %s\n",$$1,$$2}'

env:           ## Create .env with generated secrets (never overwrites)
	@bash scripts/bootstrap_env.sh

build:         ## Build the Airflow image
	$(DC) build

up: env        ## Start the full stack and wait until healthy
	$(DC) up -d --build --wait

down:          ## Stop containers (keeps data volumes)
	$(DC) down

restart: down up

ps:            ## Service status
	$(DC) ps -a

logs:          ## Tail logs (make logs s=airflow-scheduler)
	$(DC) logs -f --tail=200 $(s)

verify:        ## End-to-end health check of the running stack
	@bash scripts/verify_stack.sh

nuke:          ## Stop and DELETE all volumes (Postgres + MinIO data)
	$(DC) down -v --remove-orphans

init-db:       ## Create warehouse tables (idempotent)
	$(DC) exec -T airflow-scheduler python -c "from decksupreme.db.session import init_db; print(init_db())"

seed:          ## Validate data/cards_seed.json and upsert into dim_cards
	$(DC) exec -T airflow-scheduler python /opt/airflow/scripts/seed_dim_cards.py $(args)

extract:       ## Task 1: leaderboard + battle logs -> MinIO (make extract args="--limit 20")
	$(DC) exec -T airflow-scheduler python /opt/airflow/scripts/run_extract.py $(args)

load:          ## Tasks 2+3: raw week in MinIO -> Postgres (make load args="--year 2026 --week 45")
	$(DC) exec -T airflow-scheduler python /opt/airflow/scripts/run_load.py $(args)

rank:          ## Tasks 4+5: score cards, write rankings/ (make rank args="--year 2026 --week 45")
	$(DC) exec -T airflow-scheduler python /opt/airflow/scripts/run_scoring.py $(args)

publish:       ## Task 6: commit rankings/ to GitHub (skips if GITHUB_* unset)
	$(DC) exec -T airflow-scheduler python /opt/airflow/scripts/run_publish.py $(args)

pipeline:      ## Trigger the weekly DAG now (unpauses it first)
	$(DC) exec -T airflow-scheduler airflow dags unpause decksupreme_weekly_rankings
	$(DC) exec -T airflow-scheduler airflow dags trigger decksupreme_weekly_rankings

test:          ## Host-side unit tests (no services needed)
	pytest tests/unit -q

test-integration: ## Integration tests against the running Postgres (throwaway DB)
	pytest -m integration tests/integration -q

lint:
	ruff check src dags tests

psql:          ## Open psql on the warehouse
	$(DC) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
