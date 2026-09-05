"""
Compositore di journey.

Prende i place reali di una città, li incatena in sequenze percorribili a
piedi, e fa scrivere all'LLM solo il testo di raccordo. Nomi, indirizzi,
coordinate e tempi vengono dal database: non c'è niente da allucinare
perché non c'è niente da inventare.

    python compose_journeys.py --city milano --dry-run
    python compose_journeys.py --city milano --limit 12

Scrive journey con visibility='private' e author_kind='ai': non si vedono
nell'app finché non li pubblichi dall'admin.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from supabase import create_client  # type: ignore

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))
except ImportError:
    pass


# ------------------------------------------------------------------ regole ---

BANDITE = [
    "iconic", "imperdibil", "must", "esperienza unica", "gioiello nascosto",
    "nel cuore di", "tra le vie di", "alla scoperta", "mix perfetto",
    "suggestiv", "incantevol", "magic", "senza fine", "indimenticabil",
    "che non ti aspetti", "per tutti i gusti", "elegante",
]

WALK_M_PER_MIN = 80          # 4,8 km/h: passo urbano reale, non da atleta
DETOUR = 1.35                # le strade non sono linee rette
MAX_LEG_M = 950              # oltre ~12 minuti a piedi la tappa non regge
MIN_LEG_M = 40               # sotto questa soglia sono lo stesso posto

# Quante volte lo stesso posto puo' comparire in tutta la selezione.
# Senza questo limite il quartiere piu' denso si prende meta' dei journey:
# Brera ha il cluster piu' fitto e uscirebbe sei volte su dodici.
MAX_USES_PER_PLACE = 2
MAX_PER_NEIGHBORHOOD = 3

# Ogni template è una sequenza di ruoli. Ogni ruolo elenca le categorie
# ammesse in ordine di preferenza. Aggiungerne uno = aggiungere una riga.
TEMPLATES: list[dict] = [
    {
        "key": "aperitivo-cena-dopo",
        "label": "Aperitivo, cena, e poi si vede",
        "when": "sera",
        "roles": [("Bar", "Caffè"), ("Restaurant",), ("Club", "Pub", "Bar")],
        "times": ["19:00", "20:45", "23:00"],
    },
    {
        "key": "cultura-cena",
        "label": "Prima la mostra, poi il tavolo",
        "when": "pomeriggio",
        "roles": [("Museo & Galleria", "Monumento"), ("Caffè", "Bar"), ("Restaurant",)],
        "times": ["16:30", "18:30", "20:30"],
    },
    {
        "key": "teatro-sera",
        "label": "Una sera a teatro, fatta bene",
        "when": "sera",
        "roles": [("Bar", "Caffè"), ("Teatro",), ("Restaurant", "Pub")],   # solo teatri veri
        "times": ["18:45", "20:30", "22:45"],
    },
    {
        "key": "notte-lunga",
        "label": "Notte lunga",
        "when": "notte",
        "roles": [("Restaurant",), ("Bar", "Pub"), ("Club",)],
        "times": ["20:30", "22:30", "00:30"],
    },
    {
        "key": "giorno-lento",
        "label": "Giornata lenta",
        "when": "giorno",
        "roles": [("Caffè",), ("Museo & Galleria", "Monumento", "Piazza", "Parco"), ("Restaurant", "Bar")],
        "times": ["10:30", "12:00", "13:30"],
    },
]


# ------------------------------------------------------------------ modello ---

@dataclass
class Place:
    id: str
    name: str
    slug: str
    lat: float
    lng: float
    category: str
    neighborhood: str | None
    description: str | None
    price_level: int | None
    address: str | None


def meters(a: Place, b: Place) -> float:
    r = 6371000
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lng - a.lng)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x)) * DETOUR


def walk_min(m: float) -> int:
    return max(1, round(m / WALK_M_PER_MIN))


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s)).strip("-")[:60]


# ------------------------------------------------------------ composizione ---

def build_routes(places: list[Place], template: dict, max_per_template: int) -> list[list[Place]]:
    """Tutte le sequenze che rispettano ruoli e distanza, le migliori prima."""
    buckets = [
        [p for p in places if p.category in roles]
        for roles in template["roles"]
    ]
    if any(not b for b in buckets):
        return []

    routes: list[tuple[float, list[Place]]] = []
    for combo in itertools.product(*buckets):
        if len({p.id for p in combo}) != len(combo):
            continue
        legs = [meters(combo[i], combo[i + 1]) for i in range(len(combo) - 1)]
        if any(d > MAX_LEG_M or d < MIN_LEG_M for d in legs):
            continue
        # più corto è, meglio è: un percorso è credibile se si fa a piedi
        routes.append((sum(legs), list(combo)))

    routes.sort(key=lambda r: r[0])

    # evita di proporre dieci varianti che condividono due tappe su tre
    chosen: list[list[Place]] = []
    used_pairs: set[frozenset] = set()
    for _, route in routes:
        pairs = {frozenset((route[i].id, route[i + 1].id)) for i in range(len(route) - 1)}
        if pairs & used_pairs:
            continue
        chosen.append(route)
        used_pairs |= pairs
        if len(chosen) >= max_per_template:
            break
    return chosen


def route_neighborhood(route: list[Place]) -> str | None:
    names = [p.neighborhood for p in route if p.neighborhood]
    if not names:
        return None
    return max(set(names), key=names.count)


# -------------------------------------------------------------------- LLM ---

SYSTEM = """Sei l'editor di ESCO, city companion editoriale italiana.

Ti do un percorso GIÀ COMPOSTO: posti reali, ordine deciso, distanze e tempi
calcolati. Il tuo lavoro è SOLO scrivere il testo.

═══════════════════════════════════════════════════════════
COSA NON PUOI FARE
═══════════════════════════════════════════════════════════
1. Non inventare niente: nessun piatto, prezzo, orario di apertura, arredo,
   atmosfera o dettaglio che non sia nei dati. Se di un posto sai solo nome e
   categoria, scrivi qualcosa che regge sapendo solo quello.
2. Non aggiungere, togliere o riordinare tappe.
3. Mai iniziare il titolo con il nome della città. Chi legge è già a Milano,
   nell'app di Milano: dirglielo è spazio sprecato.
4. Parole e formule BANDITE — se ne usi una, il testo è da rifare:
   iconico, imperdibile, must, esperienza unica, gioiello nascosto,
   nel cuore di, tra le vie di, alla scoperta di, un mix perfetto,
   suggestivo, incantevole, magico, senza fine, indimenticabile,
   la Milano che non ti aspetti, per tutti i gusti, elegante.
   Niente emoji, niente esclamativi, niente due punti nel titolo.
5. Il sottotitolo NON elenca le tappe. "Mostra, caffè e osteria" descrive
   la struttura che il lettore vede già sotto: è una riga buttata.

═══════════════════════════════════════════════════════════
COSA DEVI FARE
═══════════════════════════════════════════════════════════
Il TITOLO nomina una cosa concreta: un'ora, un gesto, una condizione, un
posto specifico del percorso. Deve poter essere solo di QUESTO percorso.
Il SOTTOTITOLO dice a chi serve e quando: la situazione, non il contenuto.
La DESCRIZIONE tiene il filo — perché queste tre tappe in quest'ordine.
Due o tre frasi, almeno un dato vero (i minuti a piedi, il quartiere, l'ora).
La NOTA di ogni tappa dice perché è lì in quel punto, non cos'è il posto.
Una frase, due al massimo.

Voce: asciutta, seconda persona singolare, frasi brevi. Italiano, apostrofi
curly. Se non hai niente da dire su un posto, dì poco: meglio corto che gonfio.

═══════════════════════════════════════════════════════════
ESEMPI
═══════════════════════════════════════════════════════════
MALE:  "Milano: Aperitivo e Cena a Brera" / "Serata tra cocktail, piatti
       ricercati e vini"
       (comincia con la città, elenca le tappe, aggettivi vuoti)
BENE:  "Si comincia alle sette da Dry" / "Quando la cena è alle nove e
       prima bisogna passare il tempo"

MALE:  "Milano Notte Lunga" / "Una notte milanese senza fine"
BENE:  "Da tavola a pista in nove minuti" / "Per quando non avete voglia di
       spostarvi in taxi tra un posto e l'altro"

MALE:  "Arte e sapori a Brera" / "Mostra e cena nel cuore di Milano"
BENE:  "Un'ora di Pinacoteca, poi si mangia" / "Il pomeriggio in cui hai
       tempo ma non tutto il giorno"

═══════════════════════════════════════════════════════════
OUTPUT — solo JSON valido, senza fence
═══════════════════════════════════════════════════════════
{
  "title": "max 45 caratteri, senza due punti, senza il nome della città",
  "headline": "una riga, max 60 caratteri",
  "subtitle": "la situazione a cui serve, max 80 caratteri",
  "description": "2-3 frasi, con almeno un dato concreto dai dati che ti do",
  "vibe_tags": ["3-5 tag minuscoli, una parola ciascuno"],
  "steps": [{"note": "perché questa tappa qui"}]
}
L'array steps deve avere ESATTAMENTE lo stesso numero di tappe che ti ho dato.
"""


def write_copy(route: list[Place], template: dict, legs: list[int], city: str) -> dict | None:
    from app.services.llm import get_llm  # type: ignore

    lines = [f"CITTÀ: {city}", f"MOMENTO: {template['when']}", f"SPUNTO: {template['label']}", "", "TAPPE:"]
    for i, p in enumerate(route):
        lines.append(f"{i+1}. {p.name} — {p.category}" + (f" — {p.neighborhood}" if p.neighborhood else ""))
        if p.address:
            lines.append(f"   indirizzo: {p.address}")
        if p.price_level:
            lines.append(f"   fascia di prezzo: {'€' * p.price_level}")
        if p.description:
            lines.append(f"   descrizione esistente: {p.description[:400]}")
        if i < len(legs):
            lines.append(f"   → {legs[i]} minuti a piedi fino alla tappa successiva")
    try:
        out = get_llm().generate_json(SYSTEM, "\n".join(lines), temperature=0.7)
    except Exception as e:  # noqa: BLE001
        print(f"    ! LLM fallito: {e}")
        return None
    if not isinstance(out.get("steps"), list) or len(out["steps"]) != len(route):
        print("    ! l'LLM ha restituito un numero di tappe sbagliato, scarto")
        return None

    # Il divieto nel prompt non basta: va verificato. Meglio rigenerare che
    # pubblicare un titolo da brochure.
    testo = " ".join([
        str(out.get("title") or ""), str(out.get("headline") or ""),
        str(out.get("subtitle") or ""), str(out.get("description") or ""),
    ]).lower()
    trovate = [w for w in BANDITE if w in testo]
    if trovate:
        print(f"    ! formule bandite nel testo ({', '.join(trovate)}), scarto")
        return None
    if str(out.get("title", "")).lower().startswith(city.lower()):
        print("    ! il titolo comincia col nome della città, scarto")
        return None
    return out


# ------------------------------------------------------------------- main ---

def _env(*names: str) -> str | None:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="milano")
    ap.add_argument("--limit", type=int, default=12, help="quanti journey al massimo")
    ap.add_argument("--per-template", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="solo struttura, senza testi")
    ap.add_argument("--model", help="modello da usare SOLO qui, es. gemini-2.5-flash. "
                                    "L'estrazione dai flyer puo' restare su flash-lite: "
                                    "la voce editoriale no.")
    args = ap.parse_args()

    url = _env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    if not url or not key:
        print("Credenziali Supabase mancanti (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)", file=sys.stderr)
        return 1
    if args.model:
        from app.core.config import settings  # type: ignore
        from app.services.llm import reset_llm_cache  # type: ignore
        settings.LLM_MODEL_GEMINI = args.model
        settings.LLM_MODEL_CLAUDE = args.model
        reset_llm_cache()
        print(f"modello per i testi: {args.model}\n")

    sb = create_client(url, key)

    city = sb.table("cities").select("id, name").eq("slug", args.city).single().execute().data
    rows = (
        sb.table("places")
        .select("id, name, slug, lat, lng, description, price_level, address,"
                " categories(name), place_neighborhoods(neighborhoods(name))")
        .eq("city_id", city["id"])
        .not_.is_("lat", "null")
        .execute()
        .data or []
    )

    places: list[Place] = []
    for r in rows:
        pn = r.get("place_neighborhoods") or []
        hood = pn[0]["neighborhoods"]["name"] if pn and pn[0].get("neighborhoods") else None
        places.append(Place(
            id=r["id"], name=r["name"], slug=r["slug"],
            lat=float(r["lat"]), lng=float(r["lng"]),
            category=(r.get("categories") or {}).get("name") or "?",
            neighborhood=hood, description=r.get("description"),
            price_level=r.get("price_level"), address=r.get("address"),
        ))

    print(f"{city['name']}: {len(places)} posti con coordinate\n")

    # quanto è componibile il catalogo, template per template
    all_routes: list[tuple[dict, list[Place]]] = []
    for tpl in TEMPLATES:
        routes = build_routes(places, tpl, args.per_template)
        print(f"  {tpl['label']:<38} {len(routes)} percorso/i")
        all_routes += [(tpl, r) for r in routes]

    if not all_routes:
        print("\nNessun percorso possibile: le tappe candidate sono troppo lontane "
              f"fra loro (limite {MAX_LEG_M} m). Serve più densità in un quartiere.")
        return 0

    # Selezione con quote: nessun posto piu' di MAX_USES_PER_PLACE volte,
    # nessun quartiere piu' di MAX_PER_NEIGHBORHOOD. Meglio dodici journey
    # diversi che venti che girano intorno agli stessi quattro locali.
    random.shuffle(all_routes)
    uses: dict[str, int] = {}
    hoods: dict[str, int] = {}
    picked_sets: list[set[str]] = []
    selected: list[tuple[dict, list[Place]]] = []
    for tpl, route in all_routes:
        if any(uses.get(p.id, 0) >= MAX_USES_PER_PLACE for p in route):
            continue
        h = route_neighborhood(route) or "—"
        if hoods.get(h, 0) >= MAX_PER_NEIGHBORHOOD:
            continue
        # Due percorsi che condividono 2 tappe su 3 sono lo stesso percorso
        # con una variante: per chi scorre l'app sono un doppione.
        ids = {p.id for p in route}
        if any(len(ids & prev) >= 2 for prev in picked_sets):
            continue
        picked_sets.append(ids)
        selected.append((tpl, route))
        for p in route:
            uses[p.id] = uses.get(p.id, 0) + 1
        hoods[h] = hoods.get(h, 0) + 1
        if len(selected) >= args.limit:
            break
    all_routes = selected
    print("\n  quartieri coperti: " + ", ".join(f"{k} ({v})" for k, v in sorted(hoods.items(), key=lambda x: -x[1])))
    print(f"\n{len(all_routes)} journey da comporre\n" + "─" * 60)

    written = 0
    for tpl, route in all_routes:
        legs = [walk_min(meters(route[i], route[i + 1])) for i in range(len(route) - 1)]
        total_m = sum(meters(route[i], route[i + 1]) for i in range(len(route) - 1))
        hood = route_neighborhood(route)

        print(f"\n[{tpl['key']}] {hood or '—'}")
        for i, p in enumerate(route):
            print(f"   {tpl['times'][i]}  {p.name} ({p.category})"
                  + (f"   →{legs[i]}′ a piedi" if i < len(legs) else ""))

        copy = None if args.no_llm else write_copy(route, tpl, legs, city["name"])
        if copy:
            print(f"   « {copy.get('title')} » — {copy.get('headline')}")

        if args.dry_run:
            continue

        # Un journey senza testo non e' una bozza da rivedere, e' spazzatura
        # in tabella: se l'LLM ha fallito, saltiamo e basta.
        if copy is None and not args.no_llm:
            print("   ✗ saltato: senza testo non lo salviamo")
            continue

        title = (copy or {}).get("title") or f"{tpl['label']} · {hood or city['name']}"
        j = sb.table("journeys").insert({
            "city_id": city["id"],
            "title": title,
            # suffisso corto: due percorsi diversi possono partire dallo
            # stesso posto e avere lo stesso titolo
            "slug": f"{slugify(title)}-{uuid.uuid4().hex[:6]}",
            "headline": (copy or {}).get("headline"),
            "subtitle": (copy or {}).get("subtitle"),
            "description": (copy or {}).get("description"),
            "cover_image": None,
            "visibility": "private",          # invisibile finché non lo pubblichi
            "author_kind": "ai",
            "duration_min": sum(legs) + 60 * len(route),
            "distance_m": int(total_m),
            "vibe_tags": (copy or {}).get("vibe_tags") or [tpl["when"]],
        }).execute().data[0]

        steps = []
        for i, p in enumerate(route):
            steps.append({
                "journey_id": j["id"],
                "step_order": i + 1,
                "entity_type": "place",
                "entity_id": p.id,
                "suggested_time": tpl["times"][i],
                "note": ((copy or {}).get("steps") or [{}] * len(route))[i].get("note"),
                "next_transit_mode": "walk" if i < len(legs) else None,
                "next_duration_min": legs[i] if i < len(legs) else None,
            })
        sb.table("journey_steps").insert(steps).execute()
        written += 1
        print(f"   ✓ salvato come bozza privata")

    print("\n" + "─" * 60)
    if args.dry_run:
        print("dry-run: niente scritto. Togli --dry-run per salvare.")
    else:
        print(f"{written} journey salvati con visibility='private'.\n"
              "Aprili nell'admin, scarta quelli che non reggono, sistema il tono "
              "dei buoni e metti visibility='public'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())