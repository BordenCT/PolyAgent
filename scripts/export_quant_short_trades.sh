#!/usr/bin/env bash
# Export resolved quant_short trades to CSV for offline inference work.
#
# Tries paths in order:
#   1. psql against $DATABASE_URL  (works if psql is installed and the URL resolves)
#   2. psql against localhost:5432  (compose exposes the DB port to the host)
#   3. podman compose exec polyagent-db psql  (works without a host psql)
#
# Writes ./quant_short_trades.csv (or $1 if passed).

set -euo pipefail

OUT="${1:-quant_short_trades.csv}"
SQL_FILE="$(dirname "$0")/export_quant_short_trades.sql"

if [ ! -f "$SQL_FILE" ]; then
    echo "missing $SQL_FILE" >&2; exit 1
fi

run_with_psql_url() {
    local url="$1"
    psql "$url" --csv -f "$SQL_FILE"
}

run_with_podman() {
    # -T disables TTY allocation so stdout is clean CSV.
    podman compose exec -T polyagent-db \
        psql -U polyagent -d polyagent --csv -f - < "$SQL_FILE"
}

if command -v psql >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
    echo "exporting via psql + DATABASE_URL..." >&2
    run_with_psql_url "$DATABASE_URL" > "$OUT" || {
        echo "DATABASE_URL path failed, falling back to localhost..." >&2
        run_with_psql_url "postgresql://polyagent:polyagent@localhost:5432/polyagent" > "$OUT"
    }
elif command -v psql >/dev/null 2>&1; then
    echo "exporting via psql + localhost:5432..." >&2
    run_with_psql_url "postgresql://polyagent:polyagent@localhost:5432/polyagent" > "$OUT"
elif command -v podman >/dev/null 2>&1; then
    echo "exporting via podman compose exec..." >&2
    run_with_podman > "$OUT"
else
    echo "need either psql or podman on PATH" >&2; exit 1
fi

ROWS=$(($(wc -l < "$OUT") - 1))
echo "wrote $ROWS resolved trades to $OUT"
