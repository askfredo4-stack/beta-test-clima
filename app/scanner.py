import requests
import json
import logging
from datetime import datetime, timezone, timedelta

from app.config import (
    GAMMA, WEATHER_CITIES, MIN_YES_PRICE, MAX_YES_PRICE, TAKE_PROFIT_YES,
    MIN_VOLUME, SCAN_DAYS_AHEAD, CITY_UTC_OFFSET, OBSERVER_UTC_OFFSET,
    CITY_WINDOWS,
)

CLOB = "https://clob.polymarket.com"
log = logging.getLogger(__name__)


def now_utc():
    return datetime.now(timezone.utc)


def parse_price(val):
    try:
        return float(val)
    except Exception:
        return None


def parse_date(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def get_prices(m):
    raw = m.get("outcomePrices") or "[]"
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes = parse_price(prices[0]) if len(prices) > 0 else None
        no  = parse_price(prices[1]) if len(prices) > 1 else None
        if yes is not None and yes < 0:
            yes = None
        if no is not None and no < 0:
            no = None
        if yes == 0.0 and no is not None and no >= 0.99:
            yes = 0.001
        if no == 0.0 and yes is not None and yes >= 0.99:
            no = 0.001
        return yes, no
    except Exception:
        return None, None


def city_is_ready(city, scan_date, today):
    """V2: acepta entradas solo cuando la hora Chile está en la ventana de apertura de la ciudad.

    El check de fecha usa la hora local de la ciudad (para escanear el día correcto).
    El check de ventana horaria usa hora Chile (OBSERVER_UTC_OFFSET), referencia del operador.
    Soporta ventanas que cruzan medianoche (ej. Seoul 23:00–02:00).
    """
    city_offset = CITY_UTC_OFFSET.get(city)
    if city_offset is None:
        return False
    # ¿Es hoy el día correcto para esta ciudad?
    city_local = now_utc() + timedelta(hours=city_offset)
    if city_local.date() != scan_date:
        return False
    # ¿Estamos dentro de la ventana horaria de esta ciudad (hora Chile)?
    win = CITY_WINDOWS.get(city)
    if not win:
        return False
    open_h, open_m, close_h, close_m = win
    chile_now  = now_utc() + timedelta(hours=OBSERVER_UTC_OFFSET)
    c_mins     = chile_now.hour * 60 + chile_now.minute
    open_mins  = open_h  * 60 + open_m
    close_mins = close_h * 60 + close_m
    if open_mins < close_mins:
        return open_mins <= c_mins < close_mins
    else:  # cruza medianoche (ej. Seoul 23:00–02:00)
        return c_mins >= open_mins or c_mins < close_mins


def build_event_slug(city, date):
    months = {
        1: "january", 2: "february", 3: "march", 4: "april",
        5: "may", 6: "june", 7: "july", 8: "august",
        9: "september", 10: "october", 11: "november", 12: "december",
    }
    return f"highest-temperature-in-{city}-on-{months[date.month]}-{date.day}-{date.year}"


def fetch_event_by_slug(slug):
    try:
        r = requests.get(
            f"{GAMMA}/events", params={"slug": slug, "limit": 1},
            timeout=(5, 8),
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
    except Exception:
        pass
    return None


def fetch_market_live(slug):
    try:
        r = requests.get(
            f"{GAMMA}/markets", params={"slug": slug, "limit": 1},
            timeout=(5, 8),
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
    except Exception:
        pass
    return None


def fetch_live_prices(slug):
    """Fetch YES/NO prices via Gamma (~2 min cache). Used as fallback."""
    m = fetch_market_live(slug)
    if not m:
        return None, None
    return get_prices(m)


def fetch_yes_price_clob(yes_token_id):
    """Fetch real-time YES price from CLOB order book (no cache).

    Uses best ASK = "Buy Yes" price shown on Polymarket UI.
    Sanity check: discard if returned price > 0.50 (likely fetched NO token by mistake).
    """
    if not yes_token_id:
        return None, None
    try:
        r = requests.get(
            f"{CLOB}/book",
            params={"token_id": yes_token_id},
            timeout=(2, 3),
        )
        if r.status_code != 200:
            return None, None
        data = r.json()

        bids = data.get("bids") or []
        asks = data.get("asks") or []

        yes_price = None
        if asks:
            yes_price = min(float(a["price"]) for a in asks)
        elif bids:
            yes_price = max(float(b["price"]) for b in bids)
        # No fallback to last_trade_price: an empty book means the market
        # has resolved or gone illiquid — last_trade_price can be ~0 from a
        # resolution trade, which would make force-close look like a total loss.

        if yes_price is None or not (0.0 < yes_price < 1.0):
            return None, None

        no_price = round(1.0 - yes_price, 6)
        return yes_price, no_price

    except Exception:
        log.debug("CLOB book fetch failed for YES token %s", str(yes_token_id)[:20])
        return None, None


def scan_opportunities(existing_ids=None):
    """Scan for YES-side weather opportunities (cheap YES = high NO).

    V2: Only scans WEATHER_CITIES (5 cities) during local hours 12-17h.
    Gamma filter: NO 0.88-0.97 (= YES 0.03-0.12) for discovery.
    CLOB in bot.py is the real entry gate (YES 0.06-0.12).
    Returns all candidates sorted by YES price ascending (cheapest first).
    """
    if existing_ids is None:
        existing_ids = set()

    today = now_utc().date()
    scan_dates = [today + timedelta(days=d) for d in range(SCAN_DAYS_AHEAD + 1)]
    opportunities = []

    for scan_date in scan_dates:
        for city in WEATHER_CITIES:
            if not city_is_ready(city, scan_date, today):
                continue
            slug = build_event_slug(city, scan_date)
            event = fetch_event_by_slug(slug)
            if not event:
                continue

            for m in (event.get("markets") or []):
                condition_id = m.get("conditionId")
                if condition_id in existing_ids:
                    continue

                yes_price, no_price = get_prices(m)
                if yes_price is None or no_price is None:
                    continue

                volume = parse_price(m.get("volume") or 0) or 0
                if volume < MIN_VOLUME:
                    continue

                # Gamma discovery filter: NO 0.88-0.97 ≈ YES 0.03-0.12
                # Wide enough to catch any market that CLOB might confirm in YES 0.06-0.12
                if not (0.88 <= no_price <= 0.97):
                    continue

                profit_if_tp = (TAKE_PROFIT_YES - yes_price) * 100  # ¢ gain to take profit

                end_dt = parse_date(m.get("endDate"))
                if end_dt and end_dt.date() < today:
                    continue

                raw_ids = m.get("clobTokenIds") or "[]"
                clob_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
                yes_token_id = clob_ids[0] if len(clob_ids) > 0 else None
                no_token_id  = clob_ids[1] if len(clob_ids) > 1 else None

                opportunities.append({
                    "condition_id": condition_id,
                    "city": city,
                    "question": m.get("question", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": volume,
                    "end_date": end_dt.isoformat() if end_dt else None,
                    "slug": m.get("slug", ""),
                    "profit_cents": round(profit_if_tp, 1),
                    "yes_token_id": yes_token_id,
                    "no_token_id":  no_token_id,
                })

    # Cheapest YES first (highest leverage / most room to take profit)
    opportunities.sort(key=lambda x: x["yes_price"])
    return opportunities
