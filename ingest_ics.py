"""
Ingestion eventi da un calendario .ics.

Molti locali espongono la programmazione in formato iCalendar senza dirlo:
cercalo come "abbonati al calendario", "iCal" o un link .ics nel piede della
pagina. Quando c'e', e' la sorgente migliore in assoluto.

Niente LLM qui, e non e' una scorciatoia: un .ics ha campi definiti
(SUMMARY, DTSTART, DURATION, LOCATION, URL), quindi si legge con un parser
deterministico. Zero allucinazioni possibili, zero costo, e non si rompe
quando il sito cambia grafica.

    python ingest_ics.py --url http://bandhi.it/.../beltrade.ics \\
        --category cinema --dry-run

    python ingest_ics.py --file beltrade.ics --category cinema
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from supabase import create_client  # type: ignore

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))
except ImportError:
    pass


UA = "ESCO/1.0 (city guide; miutifin.ask@gmail.com)"


# ------------------------------------------------------------ parser .ics ---

def unfold(raw: str) -> str:
    """Nell'iCalendar le righe lunghe vanno a capo con uno spazio davanti."""
    return raw.replace("\r\n ", "").replace("\r\n\t", "").replace("\r\n", "\n") \
              .replace("\n ", "").replace("\n\t", "")


def unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\").strip())


def parse_ics(raw: str) -> list[dict]:
    """Ogni VEVENT diventa un dizionario campo -> (parametri, valore)."""
    testo = unfold(raw)
    out: list[dict] = []
    for blocco in re.findall(r"BEGIN:VEVENT\n(.*?)END:VEVENT", testo, re.S):
        ev: dict[str, tuple[str, str]] = {}
        for riga in blocco.split("\n"):
            if ":" not in riga:
                continue
            testa, valore = riga.split(":", 1)
            nome, _, params = testa.partition(";")
            ev[nome.strip().upper()] = (params, unescape(valore))
        if ev:
            out.append(ev)
    return out


def campo(ev: dict, nome: str) -> str | None:
    v = ev.get(nome)
    return v[1] if v else None


def parse_dt(ev: dict, nome: str) -> datetime | None:
    """DTSTART;TZID=Europe/Rome:20260829T175000 -> datetime consapevole del fuso."""
    v = ev.get(nome)
    if not v:
        return None
    params, valore = v
    m = re.search(r"TZID=([^;:]+)", params)
    try:
        if valore.endswith("Z"):
            return datetime.strptime(valore, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        naive = datetime.strptime(valore[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        try:
            naive = datetime.strptime(valore[:8], "%Y%m%d")   # evento tutto il giorno
        except ValueError:
            return None
    tz = ZoneInfo(m.group(1)) if m else ZoneInfo("Europe/Rome")
    return naive.replace(tzinfo=tz)


def parse_duration(v: str | None) -> int | None:
    """PT1H30M0S -> 90 minuti."""
    if not v:
        return None
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", v)
    if not m:
        return None
    g, o, mi, s = (int(x) if x else 0 for x in m.groups())
    tot = g * 1440 + o * 60 + mi + (1 if s >= 30 else 0)
    return tot or None


# ---------------------------------------------------------------- match ---

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def simile(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s)).strip("-")[:70]


# ----------------------------------------------------------------- main ---

def _env(*n: str) -> str | None:
    for x in n:
        if os.environ.get(x):
            return os.environ[x]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="indirizzo del file .ics")
    g.add_argument("--file", help="file .ics gia' scaricato")
    ap.add_argument("--city", default="milano")
    ap.add_argument("--category", default="cinema", help="slug della categoria evento")
    ap.add_argument("--place", help="slug del posto a cui agganciare tutto, "
                                    "se il LOCATION del file non basta")
    ap.add_argument("--days", type=int, default=120, help="quanto avanti guardare")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", default=None)
    args = ap.parse_args()

    if args.url:
        with httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            r = c.get(args.url)
        r.raise_for_status()
        raw = r.text
    else:
        raw = open(args.file, encoding="utf-8", errors="replace").read()

    eventi_ics = parse_ics(raw)
    if not eventi_ics:
        print("Nessun VEVENT nel file: e' davvero un .ics?", file=sys.stderr)
        return 1
    print(f"{len(eventi_ics)} eventi nel calendario\n")

    url_sb = _env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    if not url_sb or not key:
        print("Credenziali Supabase mancanti", file=sys.stderr)
        return 1
    sb = create_client(url_sb, key)

    city = sb.table("cities").select("id, name").eq("slug", args.city).single().execute().data
    try:
        cat = (sb.table("categories").select("id, name")
               .eq("slug", args.category).eq("type", "event").single().execute().data)
    except Exception:  # noqa: BLE001
        disp = sb.table("categories").select("slug").eq("type", "event").execute().data or []
        print(f"Categoria «{args.category}» non trovata. Disponibili: "
              + ", ".join(sorted(c["slug"] for c in disp)), file=sys.stderr)
        return 1

    posti = sb.table("places").select("id, name, slug").eq("city_id", city["id"]).execute().data or []
    fisso = next((p for p in posti if p["slug"] == args.place), None) if args.place else None
    if args.place and not fisso:
        print(f"Posto «{args.place}» non trovato in {city['name']}", file=sys.stderr)
        return 1

    batch = args.batch or f"ics-{slugify((args.url or args.file).split('/')[-1])}-{datetime.now():%Y%m%d-%H%M}"
    limite = datetime.now(ZoneInfo("Europe/Rome")) + timedelta(days=args.days)
    adesso = datetime.now(ZoneInfo("Europe/Rome"))

    righe, saltati = [], 0
    for ev in eventi_ics:
        titolo = campo(ev, "SUMMARY")
        inizio = parse_dt(ev, "DTSTART")
        if not titolo or not inizio:
            saltati += 1
            continue
        if inizio < adesso or inizio > limite:
            saltati += 1
            continue

        durata = parse_duration(campo(ev, "DURATION"))
        fine = parse_dt(ev, "DTEND") or (inizio + timedelta(minutes=durata) if durata else None)

        loc = campo(ev, "LOCATION") or ""
        sala = loc.split("\n")[0].strip() if loc else None

        place = fisso
        if place is None and sala:
            cand = max(posti, key=lambda p: simile(sala, p["name"]), default=None)
            if cand and simile(sala, cand["name"]) >= 0.80:
                place = cand

        righe.append({
            "city_id": city["id"],
            "place_id": place["id"] if place else None,
            "category_id": cat["id"],
            "title": titolo,
            "slug": f"{slugify(titolo)}-{inizio:%Y%m%d-%H%M}",
            "start_at": inizio.isoformat(),
            "end_at": fine.isoformat() if fine else None,
            "venue_name": sala,
            "source_url": campo(ev, "URL"),
            "time_confirmed": True,          # l'ora viene dal calendario, e' vera
            "ingest_batch": batch,
        })

    per_giorno: dict[str, int] = {}
    for r in righe:
        g = r["start_at"][:10]
        per_giorno[g] = per_giorno.get(g, 0) + 1

    print(f"categoria: {cat['name']} · posto: "
          f"{fisso['name'] if fisso else 'dal campo LOCATION'}\n")
    for g in sorted(per_giorno):
        print(f"  {g}   {per_giorno[g]:>2} eventi")
    agganciati = sum(1 for r in righe if r["place_id"])
    print(f"\n{len(righe)} da scrivere · {agganciati} agganciati a un posto "
          f"· {saltati} fuori periodo o incompleti")
    print(f"batch: {batch}")

    if args.dry_run:
        print("\ndry-run: niente scritto.")
        return 0

    scritti = doppi = falliti = 0
    for r in righe:
        try:
            sb.table("events").insert(r).execute()
            scritti += 1
        except Exception as ex:  # noqa: BLE001
            if "23505" in str(ex) or "duplicate" in str(ex).lower():
                doppi += 1
                continue
            falliti += 1
            if falliti <= 3:
                print(f"    ! {r['title'][:40]}: {str(ex)[:120]}")

    print(f"\nscritti {scritti} · gia' presenti {doppi} · errori {falliti}")
    print(f"Per annullare:  delete from events where ingest_batch = '{batch}';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())