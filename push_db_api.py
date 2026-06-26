"""Pushea tracker.db por la GitHub Contents API (no toca el historial git).
Reemplaza el `git pull --rebase + git push` del commit step de los scans, que
tardaba 9+ min porque el .git del repo es gigante (miles de commits de tracker.db
de 6-12MB). La Contents API sube el blob directo en ~5s. Retry por conflicto de
sha (re-GET + re-PUT). El mail ya se mandó antes; si un push pisa a otro, el
page_token/dedupe recuperan sin duplicar."""
import os, sys, json, base64, urllib.request, urllib.error, time

REPO = os.environ["GITHUB_REPOSITORY"]
TOK = os.environ["GH_TOKEN"]
URL = f"https://api.github.com/repos/{REPO}/contents/tracker.db"
HDR = {"Authorization": f"Bearer {TOK}", "Accept": "application/vnd.github+json",
       "X-GitHub-Api-Version": "2022-11-28"}

b64 = base64.b64encode(open("tracker.db", "rb").read()).decode()

for attempt in range(1, 7):
    # sha actual remoto
    try:
        req = urllib.request.Request(URL + "?ref=main", headers=HDR)
        sha = json.load(urllib.request.urlopen(req, timeout=60)).get("sha")
    except Exception as e:
        print(f"  GET sha falló: {e}"); sha = None
    body = {"message": "[bot] update tracker.db [skip ci]", "content": b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="PUT", headers=HDR)
        urllib.request.urlopen(req, timeout=90)
        print(f"✅ tracker.db pusheado (intento {attempt})")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        print(f"  PUT HTTP {e.code} (intento {attempt}) — reintento")
        time.sleep(min(attempt * 2, 10))
    except Exception as e:
        print(f"  PUT error {e} (intento {attempt})")
        time.sleep(min(attempt * 2, 10))
print("❌ no se pudo pushear tracker.db por Contents API")
sys.exit(1)
