#!/usr/bin/env bash
# Create .env from .env.example with freshly generated secrets.
# Safe to re-run: never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists — leaving it untouched."
  exit 0
fi

gen() { python3 -c "import secrets;print(secrets.token_urlsafe($1))"; }
fernet() { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

cp .env.example .env

set_var() {  # set_var KEY VALUE  (portable across GNU/BSD sed)
  local tmp; tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' .env > "$tmp" && mv "$tmp" .env
}

set_var POSTGRES_PASSWORD           "$(gen 24)"
set_var MINIO_ROOT_PASSWORD         "$(gen 24)"
set_var AIRFLOW_DB_PASSWORD         "$(gen 24)"
set_var AIRFLOW_ADMIN_PASSWORD      "$(gen 12)"
set_var AIRFLOW_FERNET_KEY          "$(fernet)"
set_var AIRFLOW_WEBSERVER_SECRET_KEY "$(gen 32)"
[[ "$(uname -s)" == "Linux" ]] && set_var AIRFLOW_UID "$(id -u)"

chmod 600 .env
echo "Created .env with generated secrets."
echo "Still to fill in: CLASH_ROYALE_API_KEY, GITHUB_TOKEN, GITHUB_REPO"
echo "Airflow login: admin / $(grep '^AIRFLOW_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
