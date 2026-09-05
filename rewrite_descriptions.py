"""
Riscrittura delle descrizioni dei place nella voce ESCO.

Le descrizioni attuali sono scritte in lingua da comunicato stampa:
"seafood restaurant iconico della luxury dining scene", "nel cuore di
Brera", "crowd internazionale". Sono la prima cosa che un utente legge
aprendo una scheda, e dicono che ESCO e' un aggregatore come gli altri.

Il modello NON aggiunge fatti: riscrive quelli che ci sono gia'. Se la
descrizione attuale non dice niente di concreto, la nuova sara' corta.
Meglio corta che gonfia.

    python rewrite_descriptions.py --city milano --dry-run --limit 5
    python rewrite_descriptions.py --city milano --model gemini-2.5-flash

Scrive in description_draft: la descrizione attuale non viene toccata.
Il confronto si guarda con la vista v_descrizioni_da_rivedere.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from supabase import create_client  # type: ignore

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))
except ImportError:
    pass


# Le stesse formule del compositore, piu' quelle viste nelle descrizioni reali.
BANDITE = [
    "iconic", "imperdibil", "must", "esperienza unica", "gioiello nascosto",
    "nel cuore di", "tra le vie di", "alla scoperta", "mix perfetto",
    "suggestiv", "incantevol", "magic", "senza fine", "indimenticabil",
    "che non ti aspetti", "per tutti i gusti", "elegante", "raffinato",
    "crowd", "scene milanese", "luxury", "vibe", "mood",
    "atmosfera unica", "punto di riferimento", "un'istituzione",
    "molto amato", "cult", "drink list", "seafood",
]

SYSTEM = """Sei l'editor di ESCO, city companion editoriale italiana.

Riscrivi la descrizione di un posto. Non e' traduzione: e' pulizia. La
descrizione attuale e' scritta in lingua da comunicato stampa e va portata
alla voce ESCO, che e' neutrale ma non spenta.

═══════════════════════════════════════════════════════════
1. NON AGGIUNGERE NIENTE
═══════════════════════════════════════════════════════════
Esiste solo cio' che sta scritto nei dati che ti do. Niente piatti, prezzi,
orari, arredi, anni di fondazione, premi, dettagli di ambiente.

E soprattutto: SE CONOSCI QUESTO POSTO, DIMENTICALO. Non attingere a quello
che sai per tua conoscenza. Un dettaglio vero che non e' nei dati e'
comunque un errore, perche' nessuno lo ha verificato.

Se la descrizione attuale dice poco, la tua dira' poco. Una riga vera vale
piu' di tre inventate.

═══════════════════════════════════════════════════════════
2. TOGLI IL VAGO, TIENI IL CONCRETO
═══════════════════════════════════════════════════════════
Il problema non sono gli aggettivi: e' il vuoto.

VIA, perche' non dicono niente: iconico, imperdibile, must, unico,
suggestivo, incantevole, elegante, raffinato, magico, cult, contemporaneo,
un'istituzione, punto di riferimento, molto amato, atmosfera unica,
esperienza, nel cuore di, tra le vie di, alla scoperta di.

VIA l'itanglese quando l'italiano esiste: crowd -> pubblico o gente,
spot -> posto, location -> luogo, vibe / mood -> come si sta, luxury -> di
lusso, drink list -> carta dei cocktail, seafood restaurant -> ristorante
di pesce, viral -> molto passato di bocca in bocca (o si taglia).

RESTA quello che descrive davvero: "arredo industriale", "pasta romana",
"si mangia in piedi", "prezzi accessibili", "pubblico giovane", "pizza
napoletana". Sono informazioni, non fuffa: non toglierle.

═══════════════════════════════════════════════════════════
3. IL RITMO
═══════════════════════════════════════════════════════════
Tre frasi brevi di fila, tutte soggetto-verbo-complemento, sono un elenco,
non una descrizione. Alterna: una lunga e una corta, oppure una sola frase
ben fatta. Non chiudere sempre con la stessa cadenza.
Non ripetere in una frase quello che hai gia' detto in quella prima.

Registro neutro: descrivi, non giudicare. Ma neutro non vuol dire piatto —
si puo' dire com'e' un posto senza dire se e' bello.
2-3 frasi. Massimo 350 caratteri.

═══════════════════════════════════════════════════════════
ESEMPI
═══════════════════════════════════════════════════════════
PRIMA: "Seafood restaurant iconico della luxury dining scene milanese.
        Ambiente elegante e internazionale, cucina di mare premium e
        clientela fashion, entertainment e business."
DOPO:  "Ristorante di pesce in via Savona, di fascia alta. Sala
        internazionale, molta gente che ci viene per lavoro."
MALE:  "Ristorante di pesce. È di lusso. La clientela è internazionale."
       (stesso contenuto, ritmo da elenco)

PRIMA: "Storica enoteca e wine cellar nel cuore di Brera. Atmosfera
        autentica e romantica tra vini italiani, piccoli piatti e crowd
        internazionale."
DOPO:  "Enoteca storica in via San Marco: vini italiani e piccoli piatti,
        anche solo al bancone. Pubblico internazionale."

PRIMA: "Cocktail bar e restaurant dal mood industrial chic molto amato per
        aperitivi lunghi, musica e crowd giovane."
DOPO:  "Cocktail bar e ristorante dall'arredo industriale, fatto per gli
        aperitivi che si allungano. Musica e pubblico giovane."
MALE:  "Bar e ristorante. Frequentato per aperitivi lunghi e musica. La
        sera si riempie di gente giovane."
       (ha buttato via l'arredo industriale, che era un'informazione)

═══════════════════════════════════════════════════════════
OUTPUT — solo JSON, senza fence
═══════════════════════════════════════════════════════════
{
  "description": "...",
  "mancante": "cosa avresti voluto dire ma non c'era nei dati — una riga, o stringa vuota"
}
"""


def _env(*names: str) -> str | None:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def rewrite_one(place: dict, city: str) -> tuple[str, str] | None:
    from app.services.llm import get_llm  # type: ignore

    lines = [
        f"NOME: {place['name']}",
        f"CATEGORIA: {place.get('categoria') or '—'}",
        f"CITTÀ: {city}",
    ]
    if place.get("quartiere"):
        lines.append(f"QUARTIERE: {place['quartiere']}")
    if place.get("address"):
        lines.append(f"INDIRIZZO: {place['address']}")
    if place.get("price_level"):
        lines.append(f"FASCIA DI PREZZO: {'€' * place['price_level']}")
    lines.append("")
    lines.append("DESCRIZIONE ATTUALE (da riscrivere, non da ampliare):")
    lines.append(place.get("description") or "(vuota)")

    try:
        out = get_llm().generate_json(SYSTEM, "\n".join(lines), temperature=0.65)
    except Exception as e:  # noqa: BLE001
        print(f"    ! LLM fallito: {e}")
        return None

    text = (out.get("description") or "").strip()
    if not text:
        print("    ! descrizione vuota, scarto")
        return None
    if len(text) > 480:
        print(f"    ! troppo lunga ({len(text)} car.), scarto")
        return None

    low = text.lower()
    trovate = [w for w in BANDITE if w in low]
    if trovate:
        print(f"    ! formule bandite ({', '.join(trovate)}), scarto")
        return None

    return text, (out.get("mancante") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="milano")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", help="es. gemini-2.5-flash: la voce editoriale "
                                    "non e' un lavoro da modello piccolo")
    ap.add_argument("--only-bad", action="store_true",
                    help="solo le descrizioni che contengono formule bandite")
    ap.add_argument("--redo", action="store_true",
                    help="rigenera anche dove esiste gia' una proposta")
    args = ap.parse_args()

    url = _env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    if not url or not key:
        print("Credenziali Supabase mancanti", file=sys.stderr)
        return 1

    if args.model:
        from app.core.config import settings  # type: ignore
        from app.services.llm import reset_llm_cache  # type: ignore
        settings.LLM_MODEL_GEMINI = args.model
        settings.LLM_MODEL_CLAUDE = args.model
        reset_llm_cache()
        print(f"modello: {args.model}\n")

    sb = create_client(url, key)
    city = sb.table("cities").select("id, name").eq("slug", args.city).single().execute().data

    q = (sb.table("places")
         .select("id, name, description, description_draft, address, price_level,"
                 " categories(name), place_neighborhoods(neighborhoods(name))")
         .eq("city_id", city["id"]))
    rows = q.limit(args.limit).execute().data or []

    places = []
    for r in rows:
        if r.get("description_draft") and not args.redo:
            continue
        pn = r.get("place_neighborhoods") or []
        places.append({
            "id": r["id"], "name": r["name"], "description": r.get("description"),
            "address": r.get("address"), "price_level": r.get("price_level"),
            "categoria": (r.get("categories") or {}).get("name"),
            "quartiere": pn[0]["neighborhoods"]["name"] if pn and pn[0].get("neighborhoods") else None,
        })

    if args.only_bad:
        pat = re.compile("|".join(BANDITE), re.IGNORECASE)
        places = [p for p in places if p.get("description") and pat.search(p["description"])]

    print(f"{city['name']}: {len(places)} descrizioni da riscrivere\n" + "─" * 64)

    ok = skip = 0
    for i, p in enumerate(places, 1):
        print(f"\n[{i}/{len(places)}] {p['name']}  ({p.get('quartiere') or '—'})")
        # niente troncamento: se non vedi l'originale per intero non puoi
        # giudicare se la riscrittura ha perso qualcosa
        print(f"   PRIMA:  {p.get('description') or '(vuota)'}")

        res = rewrite_one(p, city["name"])
        if not res:
            skip += 1
            continue
        text, note = res
        print(f"   DOPO:   {text}")
        if note:
            print(f"   manca nei dati: {note}")
        ok += 1

        if args.dry_run:
            continue
        sb.table("places").update({
            "description_draft": text,
            "rewrite_status": "pending",
            "rewritten_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", p["id"]).execute()

    print("\n" + "─" * 64)
    print(f"riscritte {ok} · scartate {skip}")
    if args.dry_run:
        print("dry-run: niente scritto.")
    else:
        print("Sono in description_draft, la descrizione attuale non e' stata toccata.\n"
              "Guardale affiancate:  select * from v_descrizioni_da_rivedere;\n"
              "Poi pubblica quelle buone con l'update in fondo a 06_descrizioni_draft.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())