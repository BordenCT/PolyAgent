#!/usr/bin/env bash
# Run the ML-data inventory query. Same connection logic as the other
# diagnostics scripts (DATABASE_URL -> localhost -> podman exec).

set -euo pipefail
SQL_FILE="$(dirname "$0")/inventory_ml_data.sql"

if command -v psql >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
    psql "$DATABASE_URL" -f "$SQL_FILE" 2>/dev/null \
        || psql "postgresql://polyagent:polyagent@localhost:5432/polyagent" -f "$SQL_FILE"
elif command -v psql >/dev/null 2>&1; then
    psql "postgresql://polyagent:polyagent@localhost:5432/polyagent" -f "$SQL_FILE"
elif command -v podman >/dev/null 2>&1; then
    podman compose exec -T polyagent-db psql -U polyagent -d polyagent -f - < "$SQL_FILE"
else
    echo "need psql or podman on PATH" >&2; exit 1
fi
