#!/usr/bin/env bash
# Runs once, on first boot of an empty postgres-data volume.
# Creates a dedicated role + database for Airflow's metadata so it never shares
# a schema with the DeckSupreme warehouse ($POSTGRES_DB).
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v airflow_user="${AIRFLOW_DB_USER:-airflow}" \
     -v airflow_password="${AIRFLOW_DB_PASSWORD:?AIRFLOW_DB_PASSWORD is required}" \
     -v airflow_db="${AIRFLOW_DB_NAME:-airflow}" <<'EOSQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'airflow_user', :'airflow_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'airflow_user')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'airflow_db', :'airflow_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'airflow_db')\gexec
EOSQL

echo "Airflow metadata database '${AIRFLOW_DB_NAME:-airflow}' is ready."
