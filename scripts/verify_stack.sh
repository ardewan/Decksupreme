#!/usr/bin/env bash
# End-to-end check that the local stack is healthy. Exits non-zero on any failure.
set -uo pipefail
cd "$(dirname "$0")/.."

DC="docker compose"
fail=0
ok()   { printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$1"; fail=1; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# shellcheck disable=SC1091
set -a; source .env; set +a

step "1. Long-running services are healthy"
for svc in postgres minio airflow-webserver airflow-scheduler; do
  cid="$($DC ps -q "$svc")"
  status="$( [[ -n "$cid" ]] && docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo missing)"
  [[ "$status" == "healthy" ]] && ok "$svc: $status" || bad "$svc: $status"
done

step "2. One-shot init jobs exited 0"
for svc in minio-init airflow-init; do
  cid="$($DC ps -aq "$svc")"
  code="$( [[ -n "$cid" ]] && docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null || echo missing)"
  [[ "$code" == "0" ]] && ok "$svc exit=$code" || bad "$svc exit=$code"
done

step "3. Databases exist"
for db in "$POSTGRES_DB" "${AIRFLOW_DB_NAME:-airflow}"; do
  if $DC exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
       "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    ok "database $db"
  else
    bad "database $db"
  fi
done

step "4. Raw bucket exists"
if $DC exec -T minio sh -c \
     'mc alias set l http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls l' \
     | grep -q "${MINIO_BUCKET:-decksupreme-raw}/"; then
  ok "s3://${MINIO_BUCKET:-decksupreme-raw}"
else
  bad "s3://${MINIO_BUCKET:-decksupreme-raw} not found"
fi

step "5. Airflow webserver reachable from host"
if curl -fsS "http://localhost:${AIRFLOW_WEBSERVER_PORT:-8080}/health" | grep -q '"healthy"'; then
  ok "http://localhost:${AIRFLOW_WEBSERVER_PORT:-8080}/health"
else
  bad "webserver /health"
fi

step "6. DAGs import cleanly"
errors="$($DC exec -T airflow-scheduler airflow dags list-import-errors -o plain 2>/dev/null)"
if [[ -z "$errors" || "$errors" == *"No data found"* ]]; then ok "no import errors"; else bad "import errors:"; echo "$errors"; fi

step "7. infra_healthcheck tasks (Airflow → Postgres, MinIO, app package)"
for t in check_warehouse check_raw_bucket check_app_package; do
  if out="$($DC exec -T airflow-scheduler airflow tasks test infra_healthcheck "$t" 2026-01-01 2>&1)"; then
    ok "$t: $(grep -oE 'Returned value was: .*' <<<"$out" | tail -1 | cut -c21-)"
  else
    bad "$t"; tail -20 <<<"$out"
  fi
done

echo
if [[ $fail -eq 0 ]]; then echo "Stack OK."; else echo "Stack has problems (see above)."; fi
exit $fail
