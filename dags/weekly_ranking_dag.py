"""Weekly DeckSupreme pipeline: extract -> transform/load -> score -> publish,
plus a report-only check_cards branch after the load (flags seed maintenance).

Runs Mondays 06:00 UTC. Every task is idempotent for its week, so rerunning a
failed run (or one task) is safe:
* extract   overwrites year=YYYY/week=WW/raw_battles.json in MinIO
* load      inserts with ON CONFLICT DO NOTHING in one transaction
* score     rewrites rankings/latest.json + the week's archive + README
* publish   makes no commit when GitHub already has identical files

The ISO year/week comes from the extract task (when the data was actually
pulled) and reaches the downstream tasks through XCom, so all four agree even
if a run starts just before midnight on Sunday.

Logic lives in src/decksupreme; this file only wires tasks together. Heavy
imports happen inside tasks so DAG parsing stays fast.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow.decorators import dag, task

DEFAULT_ARGS = {
    "owner": "decksupreme",
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
}


@dag(
    dag_id="decksupreme_weekly_rankings",
    description="Top-1,000 battle logs -> warehouse -> card rankings -> GitHub",
    schedule="0 6 * * 1",
    start_date=datetime(2026, 11, 2),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    dagrun_timeout=timedelta(hours=2),
    tags=["decksupreme", "weekly"],
    doc_md=__doc__,
)
def decksupreme_weekly_rankings():
    @task(execution_timeout=timedelta(minutes=45))
    def extract() -> dict:
        """Task 1: leaderboard + battle logs -> MinIO. Returns {year, week, ...}."""
        from decksupreme.extract.client import ClashAuthError
        from decksupreme.extract.pipeline import run_extraction

        try:
            summary = run_extraction()
        except ClashAuthError as exc:
            # A bad key or an IP that isn't whitelisted won't fix itself on retry.
            from airflow.exceptions import AirflowFailException

            raise AirflowFailException(str(exc)) from exc
        return summary

    @task(execution_timeout=timedelta(minutes=30))
    def transform_load(extracted: dict) -> dict:
        """Tasks 2+3: filter, dedupe, upsert into Postgres."""
        from decksupreme.db.session import init_db
        from decksupreme.load.pipeline import run_transform_and_load

        init_db()  # idempotent
        stats = run_transform_and_load(extracted["year"], extracted["week"])
        stats.pop("top_discarded_modes", None)  # keep XCom small
        return {"year": extracted["year"], "week": extracted["week"], **stats}

    @task(execution_timeout=timedelta(minutes=15))
    def score(loaded: dict) -> dict:
        """Tasks 4+5: rank cards, write rankings/ artifacts."""
        from decksupreme.scoring.pipeline import run_scoring_and_artifacts

        summary = run_scoring_and_artifacts(loaded["year"], loaded["week"])
        return {"year": loaded["year"], "week": loaded["week"], **summary}

    @task(execution_timeout=timedelta(minutes=10))
    def publish(scored: dict) -> dict:
        """Task 6: commit artifacts to GitHub (skips with a warning if unconfigured)."""
        from decksupreme.publish.github import publish_rankings

        return publish_rankings(scored["year"], scored["week"]).to_dict()

    @task(execution_timeout=timedelta(minutes=5), retries=0)
    def check_cards(_loaded: dict) -> dict:
        """Report-only: compare dim_cards with GET /v1/cards (new cards, stats still
        missing from the seed, cost changes). Never fails the run; read the warnings
        in this task's log or its XCom."""
        from decksupreme.extract.card_check import run_card_check

        try:
            return run_card_check().to_dict()
        except Exception as exc:  # noqa: BLE001 - advisory check must not block publishing
            import logging

            logging.getLogger(__name__).warning("card check could not run: %s", exc)
            return {"error": str(exc)}

    loaded = transform_load(extract())
    publish(score(loaded))
    check_cards(loaded)


decksupreme_weekly_rankings()
