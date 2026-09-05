"""
Entity resolver — OpenStreetMap / Nominatim.

L'LLM produce il candidato ("Gattopardo, Milano"); qui lo trasformiamo in
coordinate reali. Le due responsabilità restano separate apposta: il modello
non inventa mai lat/lng (regola 3 del prompt), il geocoder non inventa mai
descrizioni.

Licenza: la linea guida OSMF sul geocoding considera i singoli risultati
"insubstantial extracts", quindi si possono archiviare accanto a dati
proprietari senza share-alike. Serve però l'attribuzione a OpenStreetMap
ovunque mostriamo quei dati. Non superare questa linea: interroghiamo un
candidato alla volta, non scarichiamo l'estratto POI della città.

Vincoli Nominatim pubblico: 1 richiesta/secondo, User-Agent con contatto
reale. Se un giorno serve volume, si self-hosta (l'estratto Lombardia sta
in pochi GB) e si toglie il rate limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim richiede un contatto raggiungibile: senza, il servizio ti blocca.
USER_AGENT = "ESCO/1.0 (city guide; miutifin.ask@gmail.com)"

_MIN_INTERVAL_S = 1.1          # margine sopra il limite di 1 req/s
_TIMEOUT_S = 20.0
_MAX_RADIUS_KM = 25.0          # oltre questa distanza dal centro città, scarta
_ACCEPT_SCORE = 0.62           # ricerca per nome: sotto questa, non accettiamo
_ACCEPT_ADDRESS = 0.55         # ricerca per indirizzo: si confronta la via


# ------------------------------------------------------------------ modello ---


@dataclass
class ResolvedPlace:
    lat: float
    lng: float
    address: str | None
    osm_id: int | None
    osm_type: str | None
    display_name: str
    match_score: float          # 0..1, somiglianza del nome
    distance_km: float | None   # dal centro città, se noto
    website_url: str | None = None
    phone: str | None = None

    def as_notes(self) -> str:
        d = f", {self.distance_km:.1f} km dal centro" if self.distance_km is not None else ""
        return (
            f"geocodato via OSM: «{self.display_name}» "
            f"(match {self.match_score:.2f}{d}, osm {self.osm_type}/{self.osm_id})"
        )


# ------------------------------------------------------------- rate limiter ---


class _Throttle:
    """Un solo processo, un solo thread alla volta: rispetta 1 req/s."""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min:
                time.sleep(self._min - delta)
            self._last = time.monotonic()


_throttle = _Throttle(_MIN_INTERVAL_S)


# --------------------------------------------------------------- normalizza ---


_NOISE = re.compile(
    r"\b(ristorante|trattoria|osteria|pizzeria|bar|caffe|cafe|caffè|club|"
    r"discoteca|lounge|beach|hotel|the|il|lo|la|le|gli|i)\b",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    """Minuscole, senza accenti, senza parole di servizio: per confrontare nomi."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", " ").replace("’", " ")
    s = _NOISE.sub(" ", s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _similar(a: str, b: str) -> float:
    """
    Somiglianza fra due nomi.

    Il confronto e' per PAROLE INTERE, non per sottostringhe: "Onda" e'
    contenuto dentro "fondazione", e con il vecchio criterio la Fondazione
    Memoriale della Shoah agganciava un cocktail bar con punteggio 1.00.
    """
    na, nb = _norm(a), _norm(b)
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


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------------------------------------------- query ---


def _query_nominatim(params: dict) -> list[dict]:
    _throttle.wait()
    base = {
        "format": "jsonv2",
        "addressdetails": 1,
        "extratags": 1,
        "namedetails": 1,
        "limit": 5,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as c:
            resp = c.get(NOMINATIM_URL, params={**base, **params})
        if resp.status_code == 429:
            logger.warning("nominatim rate limit, salto questa risoluzione")
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.warning("nominatim fallita: %s", e)
        return []


def _pick_best(
    candidates: list[dict],
    name: str,
    city_center: tuple[float, float] | None,
    address_query: str | None = None,
) -> ResolvedPlace | None:
    """
    Se la ricerca era per INDIRIZZO, confrontare il nome non ha senso:
    cercando "Via Carmagnola 15" OSM risponde "15, Via Carmagnola, Isola",
    che col nome del locale ("Dexter") non c'entra niente. In quel caso il
    punteggio si calcola sull'indirizzo, che e' cio' che abbiamo chiesto.
    """
    best: ResolvedPlace | None = None
    soglia = _ACCEPT_ADDRESS if address_query else _ACCEPT_SCORE

    for c in candidates:
        try:
            lat, lng = float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            continue

        dist = _haversine_km(*city_center, lat, lng) if city_center else None
        if dist is not None and dist > _MAX_RADIUS_KM:
            continue  # stesso nome, città sbagliata

        names = c.get("namedetails") or {}
        display = c.get("display_name") or ""
        addr = c.get("address") or {}

        if address_query:
            # quanto l'indirizzo trovato somiglia a quello chiesto
            via_num = " ".join(str(x) for x in (addr.get("road"), addr.get("house_number")) if x)
            score = max(_similar(address_query, via_num),
                        _similar(address_query, display))
            # se il civico chiesto e quello trovato coincidono, e' quello
            chiesto = re.search(r"\b(\d+)\b", address_query)
            if chiesto and addr.get("house_number") == chiesto.group(1):
                score = max(score, 0.95)
        else:
            score = max(
                _similar(name, names.get("name", "")),
                _similar(name, display.split(",")[0]),
            )

        tags = c.get("extratags") or {}
        cand = ResolvedPlace(
            lat=lat,
            lng=lng,
            address=_format_address(addr) or display,
            osm_id=c.get("osm_id"),
            osm_type=c.get("osm_type"),
            display_name=display,
            match_score=round(score, 3),
            distance_km=round(dist, 2) if dist is not None else None,
            website_url=tags.get("website") or tags.get("contact:website"),
            phone=tags.get("phone") or tags.get("contact:phone"),
        )
        if best is None or cand.match_score > best.match_score:
            best = cand

    if best and best.match_score < soglia:
        logger.info("scarto «%s»: match troppo basso (%.2f, soglia %.2f)",
                    best.display_name, best.match_score, soglia)
        return None
    return best


def _format_address(a: dict) -> str | None:
    road = a.get("road")
    num = a.get("house_number")
    city = a.get("city") or a.get("town") or a.get("village")
    cap = a.get("postcode")
    street = f"{road} {num}".strip() if road else None
    parts = [p for p in (street, cap, city) if p]
    return ", ".join(parts) if parts else None


# ------------------------------------------------------------------ public ---


def resolve_place(
    name: str,
    city_name: str | None = None,
    address_hint: str | None = None,
    city_center: tuple[float, float] | None = None,
    country_code: str = "it",
) -> ResolvedPlace | None:
    """
    Due tentativi, dal più affidabile al meno.

    1. Se il content conteneva un indirizzo, cerchiamo quello: è il caso in cui
       OSM sbaglia di meno.
    2. Altrimenti nome + città come query libera.

    Restituisce None se nessun candidato supera la soglia di somiglianza:
    meglio un draft senza coordinate che un draft con le coordinate sbagliate.
    """
    if not name or not name.strip():
        return None

    attempts: list[tuple[dict, str | None]] = []
    if address_hint:
        q = f"{address_hint}, {city_name}" if city_name else address_hint
        attempts.append(({"q": q, "countrycodes": country_code}, address_hint))
    q2 = f"{name}, {city_name}" if city_name else name
    attempts.append(({"q": q2, "countrycodes": country_code}, None))

    for params, addr_q in attempts:
        results = _query_nominatim(params)
        if not results:
            continue
        best = _pick_best(results, name, city_center, address_query=addr_q)
        if best:
            return best

    logger.info("nessun match OSM per «%s» (%s)", name, city_name)
    return None


def enrich_payload(
    payload: dict,
    kind: str,
    city_name: str | None,
    city_center: tuple[float, float] | None = None,
) -> tuple[dict, str | None]:
    """
    Riempie SOLO i campi vuoti. Quello che l'LLM ha letto nel content vince
    sempre: il geocoder è un ripiego, non una correzione.

    Ritorna (payload aggiornato, nota per il revisore).
    """
    name = payload.get("name") if kind == "place" else payload.get("venue_name")
    if not name:
        return payload, None

    if payload.get("lat") is not None and payload.get("lng") is not None:
        return payload, None  # coordinate già presenti nel content

    hit = resolve_place(
        name=name,
        city_name=city_name,
        address_hint=payload.get("address"),
        city_center=city_center,
    )
    if not hit:
        return payload, "geocoding OSM: nessun match affidabile, lat/lng da inserire a mano"

    out = dict(payload)
    out["lat"] = hit.lat
    out["lng"] = hit.lng
    out.setdefault("address", None)
    if not out.get("address"):
        out["address"] = hit.address
    if not out.get("website_url") and hit.website_url:
        out["website_url"] = hit.website_url
    if not out.get("phone") and hit.phone:
        out["phone"] = hit.phone

    # tracciabilità: da dove vengono queste coordinate
    out["osm_id"] = hit.osm_id
    out["osm_type"] = hit.osm_type
    out["geocode_source"] = "osm_nominatim"
    out["geocode_confidence"] = hit.match_score

    return out, hit.as_notes()


def cache_key(name: str, city: str | None) -> str:
    return hashlib.md5(f"{_norm(name)}|{_norm(city or '')}".encode()).hexdigest()


def to_dict(r: ResolvedPlace) -> dict:
    return asdict(r)