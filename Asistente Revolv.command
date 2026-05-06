#!/bin/bash
# Asistente Revolv — abre el dashboard interactivo con datos frescos.
# Doble click para usar.

set -e

PROJECT_DIR="/Users/ignaciorodriguezpirotta/Documents/Claude/asistente-revolv"
PYTHON="/usr/bin/python3"
PORT=8767

cd "$PROJECT_DIR"

echo "🔄 Trayendo datos frescos del repo..."
git pull --rebase --autostash 2>&1 | grep -E "Updating|Already" || true

echo "📊 Generando dashboard..."
"$PYTHON" generate_dashboard.py

# Matar server viejo si está corriendo
echo "🔧 Asegurando server local..."
pkill -f "dashboard_server.py" 2>/dev/null || true
sleep 1

# Arrancar el server en background con nohup (sigue vivo aunque cierre la terminal)
nohup "$PYTHON" dashboard_server.py > "$PROJECT_DIR/logs/server.log" 2>&1 &
SERVER_PID=$!
mkdir -p "$PROJECT_DIR/logs"

# Esperar a que el server esté listo
sleep 2

# Verificar que arrancó
if ! curl -s -o /dev/null "http://localhost:${PORT}/dashboard.html" 2>/dev/null; then
    echo "⚠️  El server tardó en arrancar, esperando 2s más..."
    sleep 2
fi

echo "🌐 Abriendo dashboard en el browser..."
open "http://localhost:${PORT}/"

echo ""
echo "✅ Listo. Server corriendo (PID $SERVER_PID)."
echo "   El browser muestra datos frescos cada vez que recargás."
echo "   Cerrá esta ventana cuando quieras (el server sigue vivo en background)."
sleep 3
