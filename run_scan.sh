#!/bin/bash
# Wrapper que corre el scan + notify desde cron.
# Loggea con timestamp a logs/scan.log

set -e

PROJECT_DIR="/Users/ignaciorodriguezpirotta/Documents/Claude/asistente-revolv"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/scan.log"
PYTHON="/usr/bin/python3"

mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

{
    echo ""
    echo "════════════ $(date '+%Y-%m-%d %H:%M:%S') ════════════"
    "$PYTHON" scan.py --notify 2>&1
    echo ""
} >> "$LOG_FILE"
