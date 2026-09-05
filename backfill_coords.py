"""
Backfill delle coordinate mancanti sui place già in tabella.

Si lancia a mano, una volta:
    export SUPABASE_URL=... SUPABASE_SERVICE_KEY=...
    python backfill_coords.py --city milano --dry-run
    python backfill_coords.py --city milano

A 1,1 secondi per richiesta: 60 place = ~1 minuto, 300 = ~6 minuti.
Scrive solo dove lat/lng sono NULL e non tocca mai un dato esistente.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from supabase import create_client  # type: ignore
from app.services.ingestion.resolver import resolve_place  # type: ignore

try:  # legge il .env del backend, come fa il resto del progetto
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))
except ImportError:
    pass


def _env(*names: str) -> str | None:
    """Il nome della chiave cambia da progetto a progetto: le proviamo tutte."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True, help="slug della città, es. milano")
    ap.add_argument("--dry-run", action="store_true", help="mostra e basta, non scrive")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--min-score", type=float, default=0.72,
                    help="sotto questa soglia non scrive: meglio vuoto che sbagliato")
    args = ap.parse_args()

    url = _env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL", "SUPABASE_PROJECT_URL")
    key = _env(
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_KEY",
        "SUPABASE_SECRET_KEY",
    )
    if not url or not key:
        print(
            "Non trovo le credenziali Supabase.\n"
            "Cerco (in questo ordine): SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL\n"
            "                          SUPABASE_SERVICE_KEY / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY\n"
            "Mettile nel .env del backend, oppure:\n"
            "  export SUPABASE_URL=https://twbhmimqeyaiicazohnw.supabase.co\n"
            "  export SUPABASE_SERVICE_ROLE_KEY=eyJ...",
            file=sys.stderr,
        )
        return 1

    sb = create_client(url, key)

    city = sb.table("cities").select(
        "id, name, center_lat, center_lng, lat, lng"
    ).eq("slug", args.city).single().execute().data
    if not city:
        print(f"Città «{args.city}» non trovata", file=sys.stderr)
        return 1

    clat = city.get("center_lat") or city.get("lat")
    clng = city.get("center_lng") or city.get("lng")
    center = (float(clat), float(clng)) if clat and clng else None

    rows = (
        sb.table("places")
        .select("id, name, address, lat, lng")
        .eq("city_id", city["id"])
        .is_("lat", "null")
        .limit(args.limit)
        .execute()
        .data
        or []
    )

    print(f"{city['name']}: {len(rows)} place senza coordinate\n")
    if not rows:
        return 0

    ok = low = miss = 0
    for i, p in enumerate(rows, 1):
        hit = resolve_place(
            name=p["name"],
            city_name=city["name"],
            address_hint=p.get("address"),
            city_center=center,
        )

        if not hit:
            miss += 1
            print(f"[{i:>3}/{len(rows)}] ✗ {p['name']:<42} nessun match")
            continue

        if hit.match_score < args.min_score:
            low += 1
            print(f"[{i:>3}/{len(rows)}] ~ {p['name']:<42} match {hit.match_score:.2f} "
                  f"-> «{hit.display_name[:50]}» SALTATO")
            continue

        ok += 1
        print(f"[{i:>3}/{len(rows)}] ✓ {p['name']:<42} {hit.lat:.5f},{hit.lng:.5f} "
              f"({hit.match_score:.2f})")

        if args.dry_run:
            continue

        patch = {
            "lat": hit.lat,
            "lng": hit.lng,
            "osm_id": hit.osm_id,
            "osm_type": hit.osm_type,
            "geocode_source": "osm_nominatim",
            "geocode_confidence": hit.match_score,
            "geocoded_at": datetime.now(timezone.utc).isoformat(),
        }
        if not p.get("address") and hit.address:
            patch["address"] = hit.address
        sb.table("places").update(patch).eq("id", p["id"]).execute()

    print(f"\nscritti {ok} · match debole saltati {low} · nessun match {miss}")
    if low or miss:
        print("I saltati vanno guardati a mano: sono i posti con nomi generici "
              "o chiusi da tempo. Spesso il nome nostro non coincide con l'insegna.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())