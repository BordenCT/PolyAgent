#!/usr/bin/env bash
# Install / remove the cron entry that runs the microstructure pipeline.
#
# Usage:
#   ./scripts/install_microstructure_cron.sh install   # default
#   ./scripts/install_microstructure_cron.sh remove
#   ./scripts/install_microstructure_cron.sh status

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIPELINE="$REPO_ROOT/scripts/microstructure_pipeline.sh"
MARKER="# polyagent-microstructure-pipeline"
CRON_LINE="0 */6 * * * cd $REPO_ROOT && $PIPELINE >> microstructure_cron.log 2>&1 $MARKER"

cmd="${1:-install}"

case "$cmd" in
    install)
        if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
            echo "cron entry already present:"
            crontab -l | grep -F "$MARKER"
            exit 0
        fi
        # Append to existing crontab (or create if none).
        ( crontab -l 2>/dev/null; echo ""; echo "$CRON_LINE" ) | crontab -
        echo "installed cron entry:"
        echo "  $CRON_LINE"
        echo
        echo "log file: $REPO_ROOT/microstructure_cron.log"
        echo "report (when ready): $REPO_ROOT/docs/feat/microstructure-estimator-report.md"
        ;;
    remove)
        if ! crontab -l 2>/dev/null | grep -qF "$MARKER"; then
            echo "no marker found; nothing to remove"
            exit 0
        fi
        crontab -l | grep -vF "$MARKER" | crontab -
        echo "removed cron entry"
        ;;
    status)
        if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
            echo "INSTALLED"
            crontab -l | grep -F "$MARKER"
        else
            echo "NOT INSTALLED"
        fi
        ;;
    *)
        echo "usage: $0 [install|remove|status]" >&2
        exit 2
        ;;
esac
