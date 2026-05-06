"""
Dashboard CLI: cruza el Sheet de packs con el estado real de Drive.

Uso:
    python3 dashboard.py
"""

from collections import defaultdict

from sheets_client import read_packs, get_sheet_metadata
from drive_client import list_root_folders, inspect_client


def run():
    print("📊 ASISTENTE REVOLV — Dashboard de packs\n")

    meta = get_sheet_metadata()
    print(f"Sheet: {meta['title']}  |  hojas: {', '.join(meta['tabs'])}")

    packs = read_packs()
    print(f"Packs en sheet: {len(packs)}\n")

    print("🔍 Escaneando Drive...")
    folders = list_root_folders()
    print(f"   {len(folders)} carpetas en Mi Unidad.\n")

    activos = [p for p in packs if not p.completo]
    completos = [p for p in packs if p.completo]

    print(f"📦 PACKS ACTIVOS — {len(activos)}\n")
    print(f"{'Editor':<14} {'Cliente':<28} {'Sheet':<10} {'Drive editados':<14} {'Crudos':<8} Status")
    print("─" * 100)

    sin_carpeta = []
    drive_difiere = []
    por_editor = defaultdict(list)

    for p in activos:
        cf = inspect_client(p.cliente, folders)
        if cf is None:
            sin_carpeta.append(p)
            print(f"{p.editor:<14} {p.cliente:<28} {p.videos_hechos}/{p.videos_pedidos:<7} {'—':<14} {'—':<8} ❌ sin carpeta en Drive")
            continue

        sheet_str = f"{p.videos_hechos}/{p.videos_pedidos}"
        crudos_str = str(cf.crudos) if cf.crudos else "—"

        # Comparar Drive vs Sheet
        if cf.editados != p.videos_hechos:
            drive_difiere.append((p, cf))

        if cf.editados >= p.videos_pedidos:
            status = "✅ Drive ya tiene todos (Sheet desactualizado)"
        elif cf.crudos == 0:
            faltan = p.videos_pedidos - cf.editados
            status = f"⚠️  faltan {faltan}, sin crudos"
        else:
            faltan = p.videos_pedidos - cf.editados
            status = f"📁 faltan {faltan}, hay {cf.crudos} crudos para editar"

        print(f"{p.editor:<14} {p.cliente:<28} {sheet_str:<10} {cf.editados:<14} {crudos_str:<8} {status}")
        por_editor[p.editor].append((p, cf))

    print()
    print("=" * 100)
    print(f"\n👥 RESUMEN POR EDITOR\n")
    for editor in sorted(por_editor.keys()):
        items = por_editor[editor]
        total_pendientes = sum(max(0, p.videos_pedidos - cf.editados) for p, cf in items)
        con_material = sum(1 for _, cf in items if cf.has_material)
        print(f"  {editor:<15}  {len(items)} packs activos  |  {total_pendientes} videos pendientes  |  {con_material} con crudos disponibles")

    if sin_carpeta:
        print(f"\n⚠️  CLIENTES SIN CARPETA EN DRIVE ({len(sin_carpeta)}):")
        for p in sin_carpeta:
            print(f"  - {p.cliente}  (editor: {p.editor})")

    if drive_difiere:
        print(f"\n📈 SHEET DESACTUALIZADO ({len(drive_difiere)} packs donde Drive ≠ Sheet):")
        for p, cf in drive_difiere:
            print(f"  - {p.cliente:<25} sheet dice {p.videos_hechos}, Drive tiene {cf.editados}")

    print(f"\n✅ Packs completos según sheet: {len(completos)}")
    print(f"📊 Total packs: {len(packs)}\n")


if __name__ == "__main__":
    run()
