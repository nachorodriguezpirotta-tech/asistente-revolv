# Asistente Revolv

Sistema que monitorea Google Drive de Revolv y avisa por mail cuando un cliente sube material nuevo a su carpeta `/Material/`.

## Arquitectura

```
GitHub Actions (cron cada 30 min)
    ↓
scan.py
    ↓
1. Lee Google Drive (177 carpetas de cliente)
2. Detecta archivos nuevos en /Material/
3. Lee Sheet → identifica editor responsable
4. Manda mail al editor (vía Gmail API)
5. Actualiza DB SQLite local (committed al repo)
```

## Componentes

| Archivo | Función |
|---|---|
| `auth.py` | OAuth Google (local + cloud) |
| `config.py` | Configuración central (lee env vars) |
| `drive_client.py` | API Drive: descubrir carpetas, listar archivos |
| `sheets_client.py` | API Sheets: leer packs (READONLY) |
| `mail_client.py` | API Gmail: enviar mails |
| `tracker.py` | DB SQLite: state persistente |
| `baseline.py` | Snapshot inicial (estado HOY = "ya conocido") |
| `scan.py` | Detecta archivos nuevos + crea tareas |
| `notifier.py` | Agrupa tareas y manda mails |
| `.github/workflows/scan.yml` | Cron de GitHub Actions |

## Setup local (ya hecho)

```bash
# 1. Autorizar OAuth (abre browser)
python3 auth.py

# 2. Snapshot inicial
python3 baseline.py
```

## Setup en GitHub Actions

### 1. Subir el repo a GitHub

```bash
git init
git add .
git commit -m "init"
git remote add origin git@github.com:USER/asistente-revolv.git
git push -u origin main
```

### 2. Configurar Secrets en GitHub

GitHub repo → Settings → Secrets and variables → Actions → New repository secret

Agregar estos 5 secrets (los valores los imprime `python3 auth.py` al final):

- `OAUTH_REFRESH_TOKEN` — token long-lived de OAuth
- `OAUTH_CLIENT_ID` — ID del OAuth client
- `OAUTH_CLIENT_SECRET` — secret del OAuth client
- `SHEET_ID` — ID del Google Sheet de packs
- `TEST_EMAIL` — destinatario de los mails (durante testing)

### 3. Habilitar Actions

Settings → Actions → General → "Allow all actions" + permitir workflows con write access.

### 4. Probar

Actions tab → "Scan Drive" → Run workflow.

## Mantenimiento

### Cambiar destinatario de mails

Editar Secret `TEST_EMAIL`. Sin re-deploy.

### Pausar el sistema

Actions tab → Scan Drive → "..." → Disable workflow.

### Ver logs

Actions tab → última corrida → ver output.

### Ver tareas pendientes

```bash
git pull
sqlite3 tracker.db "SELECT cliente, editor, file_name, detected_at FROM tasks WHERE status='pending'"
```

