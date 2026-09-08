"""Verifikasi semua template: deteksi tag multi-baris & uji parse."""
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "inti_project.settings")
django.setup()

from django.template.loader import get_template  # noqa: E402

TEMPLATE_DIR = os.path.join("POSsystem", "templates", "POSsystem")
templates = sorted(f for f in os.listdir(TEMPLATE_DIR) if f.endswith(".html"))

print("=== Deteksi tag multi-baris (buka/tutup tidak seimbang per baris) ===")
ada_masalah = False
for nama in templates:
    with open(os.path.join(TEMPLATE_DIR, nama), encoding="utf-8") as f:
        for no, baris in enumerate(f, 1):
            buka = baris.count("{%") + baris.count("{{") + baris.count("{#")
            tutup = baris.count("%}") + baris.count("}}") + baris.count("#}")
            if buka != tutup:
                ada_masalah = True
                print(f"{nama}:{no} -> {baris.strip()[:70]}")

if not ada_masalah:
    print("Semua tag seimbang dalam satu baris.")

print()
print("=== Uji parse semua template ===")
gagal = False
for nama in templates:
    try:
        get_template(f"POSsystem/{nama}")
        print(f"OK    {nama}")
    except Exception as e:
        gagal = True
        print(f"GAGAL {nama}: {type(e).__name__}: {e}")

raise SystemExit(1 if (ada_masalah or gagal) else 0)