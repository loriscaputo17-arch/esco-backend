"""
Ingestion eventi da una pagina di calendario.

Due passaggi, perche' le liste dei teatri danno la data ma quasi mai l'ora:
  1. la pagina lista  -> titolo, sala, intervallo di date, prezzo, link
  2. la pagina di dettaglio di ogni spettacolo -> le date esatte con l'ora

Uno spettacolo che va in scena dal 10 al 13 diventa quattro eventi: un'app
che dice cosa fare stasera ha bisogno di sapere cosa c'e' stasera, non che
qualcosa "e' in cartellone questa settimana".

    python ingest_events.py --url https://www.piccoloteatro.org/it/pages/prossimi-spettacoli \\
        --pages 3 --dry-run --model gemini-2.5-flash

    python ingest_events.py --url ... --pages 5 --model gemini-2.5-flash

Gli eventi entrano con time_confirmed=true solo se l'ora viene dalla pagina
di dettaglio. Con --no-detail si usa un orario di convenzione e il campo
resta false: quelli non vanno pubblicati senza controllo.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

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
PAUSA_S = 1.0          # cortesia verso il sito: una richiesta al secondo
ORA_CONVENZIONE = "20:30"      # prosa: si alza il sipario. Per i club usa --default-time
MAX_REPLICHE = 12      # uno spettacolo che dura un mese non diventa 30 eventi

# Fuso: da fine marzo a fine ottobre l'Italia e' su +02:00
def _tz(d: datetime) -> str:
    return "+02:00" if 3 <= d.month <= 10 else "+01:00"


# ------------------------------------------------------------------ utili ---

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def simile(a: str, b: str) -> float:
    """
    Somiglianza fra due nomi.

    Il confronto e' per PAROLE INTERE, non per sottostringhe: "Onda" e'
    contenuto dentro "fondazione", e con il vecchio criterio la Fondazione
    Memoriale della Shoah agganciava un cocktail bar con punteggio 1.00.
    """
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    # uno e' il sottoinsieme dell'altro: "Teatro Grassi" dentro
    # "Piccolo Teatro Grassi", oppure "Teatro Elfo Puccini" dentro
    # "Teatro dell'Elfo Puccini"
    if ta <= tb or tb <= ta:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s)).strip("-")[:70]


def html_to_text(raw: str) -> str:
    """Testo leggibile dall'LLM: via script e stile, immagini come marcatori."""
    raw = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r'(?i)<img[^>]*src=["\']([^"\']+)["\'][^>]*>', r" IMG:\1 ", raw)
    raw = re.sub(r'(?i)<a[^>]*href=["\']([^"\']+)["\'][^>]*>', r" LINK:\1 ", raw)
    raw = re.sub(r"(?i)</?(p|div|li|tr|h[1-6]|br)[^>]*>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def assoluto(href: str | None, base: str) -> str | None:
    """I siti scrivono /it/spettacolo: senza dominio httpx non sa dove andare."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(base, href)


def fetch(url: str) -> str | None:
    try:
        time.sleep(PAUSA_S)
        with httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url)
        if r.status_code != 200:
            print(f"    ! {url} -> HTTP {r.status_code}")
            return None
        return r.text
    except httpx.HTTPError as e:
        print(f"    ! {url} -> {e}")
        return None


# -------------------------------------------------------------- prompt 1 ---

SYS_LISTA = """Estrai gli spettacoli da una pagina di calendario di un teatro.

REGOLE
1. SOLO quello che c'e' scritto. Se un dato manca, null. Non dedurre, non completare.
2. Le date vanno in formato ISO YYYY-MM-DD, qualunque sia la lingua della
   pagina. "10 - 13 settembre 2026" ha date_start 2026-09-10 e date_end
   2026-09-13. "FRIDAY, SEPTEMBER 4, 2026" ha date_start 2026-09-04 e
   date_end null. Una data sola ha sempre date_end null.
3. venue e' il nome della sala COSI' COME SCRITTO (es. "Teatro Grassi").
4. price_min e' un numero, senza simbolo. "Biglietti da € 12" -> 12. Se non
   c'e' prezzo, null. Se dice gratuito o ingresso libero, 0.
5. detail_url e' il link alla pagina dello spettacolo (LINK:...), ticket_url
   quello dell'acquisto, image_url l'immagine (IMG:...). Solo se presenti.
   Un link a wa.me o a WhatsApp NON e' un ticket_url: e' una prenotazione,
   mettilo in booking_url. Se il locale non vende online, ticket_url null.
6. Ignora menu, footer, elenchi di filtri, banner e link di navigazione:
   sono spettacoli solo i blocchi con un titolo E una data.

OUTPUT: solo JSON, senza fence.
{"shows": [{
  "title": str, "subtitle": str|null, "venue": str|null,
  "date_start": "YYYY-MM-DD", "date_end": "YYYY-MM-DD"|null,
  "description": str|null, "price_min": number|null,
  "detail_url": str|null, "ticket_url": str|null, "booking_url": str|null,
  "image_url": str|null
}]}
Se non trovi spettacoli, {"shows": []}."""


SYS_DETTAGLIO = """Estrai le repliche di UNO spettacolo dalla sua pagina.

Cerca il calendario delle date: ogni replica ha una data e quasi sempre
un'ora (es. "gio 11 settembre h 19.30", "domenica ore 16:00").

REGOLE
1. SOLO quello che c'e' scritto. Se l'ora non compare, time null: non
   inventarla e non dedurla dalle abitudini dei teatri.
2. Formato: date "YYYY-MM-DD", time "HH:MM" 24 ore.
3. Includi solo le repliche di QUESTO spettacolo, non altri in cartellone.
4. duration_min se dichiarata, altrimenti null.

OUTPUT: solo JSON, senza fence.
{"dates": [{"date": "YYYY-MM-DD", "time": "HH:MM"|null}], "duration_min": int|null}"""


def estrai_lista(llm, testo: str, profondita: int = 0) -> list[dict]:
    """
    Una pagina densa produce un JSON piu' lungo del tetto di token in uscita
    e torna troncato. In quel caso la spezziamo a meta' e riproviamo: due
    risposte corte passano dove una lunga non passa.
    """
    try:
        out = llm.generate_json(SYS_LISTA, testo, temperature=0.1)
        return out.get("shows") or []
    except Exception as e:  # noqa: BLE001
        if profondita >= 2 or len(testo) < 4000:
            print(f"    ! estrazione fallita: {str(e)[:120]}")
            return []
        print(f"    ~ risposta troncata, spezzo il testo (giro {profondita + 1})")
        meta = len(testo) // 2
        taglio = testo.find("\n", meta) or meta
        return (estrai_lista(llm, testo[:taglio], profondita + 1)
                + estrai_lista(llm, testo[taglio:], profondita + 1))


# ------------------------------------------------------------------ main ---

def _env(*n: str) -> str | None:
    for x in n:
        if os.environ.get(x):
            return os.environ[x]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="pagina lista del calendario")
    ap.add_argument("--city", default="milano")
    ap.add_argument("--pages", type=int, default=1, help="quante pagine (?page=N)")
    ap.add_argument("--limit", type=int, default=200, help="max eventi scritti")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-detail", action="store_true",
                    help="salta le pagine di dettaglio: piu' veloce, orario di convenzione")
    ap.add_argument("--model", help="es. gemini-2.5-flash")
    ap.add_argument("--category", default="theatre",
                    help="slug della categoria EVENTO: theatre, serata-club, cinema, "
                         "art, concerto, live-music, festival, food-drink, sport. "
                         "Un dj set che entra come 'teatro' e' inutilizzabile nei filtri.")
    ap.add_argument("--default-time", default=ORA_CONVENZIONE,
                    help="orario da usare quando la pagina non lo dice. "
                         "Teatro 20:30 (predefinito), club 23:30, mostra 10:00. "
                         "Gli eventi cosi' creati restano time_confirmed=false.")
    ap.add_argument("--only-matched", action="store_true",
                    help="scarta gli eventi la cui sala non e' in catalogo "
                         "(utile per escludere Pavia, Bergamo e simili)")
    ap.add_argument("--batch", default=None, help="etichetta per poter annullare l'import")
    args = ap.parse_args()

    url_sb = _env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    if not url_sb or not key:
        print("Credenziali Supabase mancanti", file=sys.stderr)
        return 1

    if args.model:
        from app.core.config import settings  # type: ignore
        from app.services.llm import reset_llm_cache  # type: ignore
        settings.LLM_MODEL_GEMINI = args.model
        settings.LLM_MODEL_CLAUDE = args.model
        reset_llm_cache()
    from app.services.llm import get_llm  # type: ignore
    llm = get_llm()

    sb = create_client(url_sb, key)
    city = sb.table("cities").select("id, name").eq("slug", args.city).single().execute().data
    try:
        cat = (sb.table("categories").select("id, name")
               .eq("slug", args.category).eq("type", "event").single().execute().data)
    except Exception:  # noqa: BLE001
        disp = (sb.table("categories").select("slug, name")
                .eq("type", "event").execute().data or [])
        print(f"Categoria evento «{args.category}» non trovata.\nDisponibili: "
              + ", ".join(sorted(c["slug"] for c in disp)), file=sys.stderr)
        return 1

    # i posti su cui agganciare le sale
    posti = sb.table("places").select("id, name, slug").eq("city_id", city["id"]).execute().data or []
    batch = args.batch or f"{slugify(args.url.split('/')[2])}-{datetime.now():%Y%m%d-%H%M}"
    print(f"categoria: {cat.get('name', args.category)} · orario di ripiego: {args.default_time}\n")

    # ── 1. le pagine lista ────────────────────────────────────────────────
    shows: list[dict] = []
    for page in range(args.pages):
        u = args.url if page == 0 else f"{args.url}?page={page}"
        print(f"lista: {u}")
        raw = fetch(u)
        if not raw:
            continue
        testo = html_to_text(raw)[:60000]
        trovati = estrai_lista(llm, testo)
        print(f"    {len(trovati)} spettacoli")
        shows += trovati
        if not trovati:
            break

    if not shows:
        print("Nessuno spettacolo trovato: controlla l'URL o alza --pages.")
        return 0

    # ── 2. dettaglio e repliche ───────────────────────────────────────────
    eventi: list[dict] = []
    for i, s in enumerate(shows, 1):
        titolo = (s.get("title") or "").strip()
        if not titolo or not s.get("date_start"):
            continue

        for k in ("detail_url", "ticket_url", "booking_url", "image_url"):
            s[k] = assoluto(s.get(k), args.url)

        sala = (s.get("venue") or "").strip()
        place = None
        if sala:
            # "Teatro Strehler - Scatola Magica" e' una sala dentro lo Strehler:
            # proviamo anche la parte prima del trattino.
            varianti = [sala]
            for sep in (" - ", " – ", " | ", ","):
                if sep in sala:
                    varianti.append(sala.split(sep)[0].strip())
            punteggio = lambda p: max(simile(v, p["name"]) for v in varianti)
            cand = max(posti, key=punteggio, default=None)
            if cand and punteggio(cand) >= 0.80:   # 0.70 agganciava
                                                   # "Memoriale della Shoah" a "Onda"
                place = cand

        repliche: list[tuple[str, str | None]] = []
        durata = None

        if not args.no_detail and s.get("detail_url"):
            raw = fetch(s["detail_url"])
            if raw:
                try:
                    d = llm.generate_json(SYS_DETTAGLIO, html_to_text(raw)[:40000], temperature=0.1)
                    repliche = [(x["date"], x.get("time")) for x in (d.get("dates") or []) if x.get("date")]
                    durata = d.get("duration_min")
                except Exception as e:  # noqa: BLE001
                    print(f"    ! dettaglio fallito su «{titolo}»: {e}")

        if not repliche:                      # ripiego: espandi l'intervallo
            d0 = datetime.fromisoformat(s["date_start"])
            d1 = datetime.fromisoformat(s["date_end"]) if s.get("date_end") else d0
            giorni = min((d1 - d0).days + 1, MAX_REPLICHE)
            repliche = [((d0 + timedelta(days=k)).strftime("%Y-%m-%d"), None) for k in range(giorni)]

        if args.only_matched and not place:
            print(f"[{i}/{len(shows)}] {titolo[:44]:<44} saltato: «{sala[:30]}» non e' in catalogo")
            continue

        for data, ora in repliche[:MAX_REPLICHE]:
            try:
                d = datetime.fromisoformat(data)
            except ValueError:
                continue
            if d.date() < datetime.now().date():
                continue                      # niente eventi passati
            confermato = ora is not None
            orario = ora or args.default_time
            start = f"{data}T{orario}:00{_tz(d)}"
            eventi.append({
                "city_id": city["id"],
                "place_id": place["id"] if place else None,
                "category_id": cat["id"],
                "title": titolo,
                "slug": f"{slugify(titolo)}-{data.replace('-','')}",
                "description": s.get("description"),
                "start_at": start,
                "end_at": (
                    (d.replace(hour=int(orario[:2]), minute=int(orario[3:5]))
                     + timedelta(minutes=durata)).isoformat() + _tz(d)
                    if durata else None
                ),
                "price_min": s.get("price_min"),
                "cover_image": s.get("image_url"),
                "ticket_url": s.get("ticket_url"),
                "source_url": s.get("detail_url") or args.url,
                "venue_name": sala or None,
                "time_confirmed": confermato,
                "ingest_batch": batch,
            })

        print(f"[{i}/{len(shows)}] {titolo[:44]:<44} "
              f"{len(repliche)} repl. · {sala[:22]:<22} "
              f"{'→ ' + place['name'][:20] if place else '(nessun posto)'}"
              f"{'' if any(r[1] for r in repliche) else '  ORARIO DA CONFERMARE'}")

    # ── 3. scrittura ──────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    con_ora = sum(1 for e in eventi if e["time_confirmed"])
    con_posto = sum(1 for e in eventi if e["place_id"])
    print(f"{len(eventi)} eventi · {con_ora} con orario verificato · {con_posto} agganciati a un posto")
    print(f"batch: {batch}")

    if args.dry_run:
        print("\ndry-run: niente scritto.")
        return 0

    scritti = falliti = 0
    for e in eventi[: args.limit]:
        try:
            sb.table("events").insert(e).execute()
            scritti += 1
        except Exception as ex:  # noqa: BLE001
            if "23505" in str(ex) or "duplicate" in str(ex).lower():
                continue                      # gia' presente: va bene cosi'
            falliti += 1
            if falliti <= 3:
                print(f"    ! {e['title'][:40]}: {str(ex)[:120]}")

    print(f"\nscritti {scritti} · gia' presenti {len(eventi) - scritti - falliti} · errori {falliti}")
    print(f"Per annullare tutto:  delete from events where ingest_batch = '{batch}';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())