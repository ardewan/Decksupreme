"""DAG integrity: imports cleanly, has the expected shape. Skipped where Airflow
isn't installed (host venv); runs inside the Airflow image."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("airflow")

from airflow.models import DagBag  # noqa: E402

DAGS = Path(__file__).resolve().parents[2] / "dags"


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(dag_folder=str(DAGS), include_examples=False)


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_weekly_dag_shape(dagbag):
    dag = dagbag.dags.get("decksupreme_weekly_rankings")  # parsed; no metadata DB needed
    assert dag is not None
    assert dag.schedule_interval == "0 6 * * 1"
    assert dag.catchup is False
    assert dag.max_active_runs == 1
    assert [t.task_id for t in dag.topological_sort()] == [
        "extract", "transform_load", "score", "publish",
    ]
    for t in dag.tasks:
        assert t.retries == 2
        assert t.execution_timeout is not None
