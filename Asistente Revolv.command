#!/bin/bash
# Asistente Revolv — abre el dashboard con datos frescos.
# Doble click en este archivo para ejecutar.

set -e

PROJECT_DIR="/Users/ignaciorodriguezpirotta/Documents/Claude/asistente-revolv"
PYTHON="/usr/bin/python3"

cd "$PROJECT_DIR"

echo "🔄 Trayendo datos frescos del repo..."
git pull --rebase --autostash 2>&1 | grep -E "Updating|Already" || true

echo "📊 Generando dashboard..."
"$PYTHON" generate_dashboard.py

echo "🌐 Abriendo en el browser..."
open "$PROJECT_DIR/dashboard.html"

echo ""
echo "✅ Listo. Cerrá esta ventana cuando quieras."
sleep 2
