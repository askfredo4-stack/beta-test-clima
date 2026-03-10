import threading
import logging
from datetime import datetime, timezone, timedelta

from app.config import (
    PRICE_HISTORY_TTL,
    SCORE_VOLUME_HIGH, SCORE_VOLUME_MID, SCORE_VOLUME_LOW,
    CITY_UTC_OFFSET, CITY_WINDOWS, OBSERVER_UTC_OFFSET,
)

log = logging.getLogger(__name__)

MAX_HISTORY_PER_MARKET = 50


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _now_utc():
    return datetime.now(timezone.utc)


class MarketScorer:
    def __init__(self):
        # {condition_id: [(timestamp, yes_price, volume), ...]}
        self._history: dict[str, list[tuple[float, float, float]]] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def record(self, condition_id: str, yes_price: float, volume: float, city: str):
        """Append one CLOB YES price observation."""
        ts = _now_ts()
        with self._lock:
            if condition_id not in self._history:
                self._history[condition_id] = []
            hist = self._history[condition_id]
            hist.append((ts, yes_price, volume))
            if len(hist) > MAX_HISTORY_PER_MARKET:
                self._history[condition_id] = hist[-MAX_HISTORY_PER_MARKET:]

    def score(self, condition_id: str, city: str) -> dict:
        """Return full score breakdown for a YES market.

        Returns:
            {
              'total': int,        # 0-100
              'price': int,        # price zone sub-score
              'trajectory': int,   # trajectory sub-score
              'volume': int,       # volume sub-score
              'time': int,         # time-of-day sub-score
              'observations': int,
              'zone': str,         # 'A' / 'B' / '-'
            }
        """
        with self._lock:
            hist = list(self._history.get(condition_id, []))

        if not hist:
            return {"total": 0, "price": 0, "trajectory": 0,
                    "volume": 0, "time": 0, "observations": 0, "zone": "-"}

        last_ts, last_yes, last_vol = hist[-1]

        price_pts, zone = self._price_score(last_yes)
        traj_pts        = self._trajectory_score(hist)
        vol_pts         = self._volume_score(last_vol)
        time_pts        = self._time_score(city)

        total = price_pts + traj_pts + vol_pts + time_pts

        return {
            "total":        total,
            "price":        price_pts,
            "trajectory":   traj_pts,
            "volume":       vol_pts,
            "time":         time_pts,
            "observations": len(hist),
            "zone":         zone,
        }

    def get_all_scores(self) -> dict:
        result = {}
        with self._lock:
            cids = list(self._history.keys())
        for cid in cids:
            result[cid] = self.score(cid, "")
        return result

    def purge_old(self):
        cutoff = _now_ts() - PRICE_HISTORY_TTL
        with self._lock:
            to_delete = [
                cid for cid, hist in self._history.items()
                if hist and hist[-1][0] < cutoff
            ]
            for cid in to_delete:
                del self._history[cid]
        if to_delete:
            log.info("MarketScorer purged %d stale histories", len(to_delete))

    # ── Sub-scores ────────────────────────────────────────────────────────────

    def _price_score(self, yes_price: float) -> tuple[int, str]:
        """Zonas de precio YES — más barato = más upside = mejor zona.

        Zona A: YES 0.06–0.09  → 30 pts  (ratio TP/entrada 67-150%)
        Zona B: YES 0.09–0.12  → 20 pts  (ratio TP/entrada 25-67%)
        Fuera:  0 pts
        """
        if 0.06 <= yes_price < 0.09:
            return 30, "A"
        if 0.09 <= yes_price <= 0.12:
            return 20, "B"
        return 0, "-"

    def _trajectory_score(self, hist: list) -> int:
        """Score basado en las últimas observaciones de YES price.

        YES subiendo gradual (0.5–2¢ por paso avg): 30 pts  ← momentum positivo
        YES estable (variación < 1¢):                20 pts  ← anclado bajo, listo para spike
        YES subiendo rápido (>2¢ por paso avg):      10 pts  ← ya se movió, tarde
        YES cayendo / errático:                        0 pts

        Requiere al menos 2 observaciones.
        """
        if len(hist) < 2:
            return 0

        n = min(4, len(hist))
        prices = [p for _, p, _ in hist[-n:]]
        variation  = max(prices) - min(prices)
        avg_change = (prices[-1] - prices[0]) / (len(prices) - 1)

        if avg_change > 0.02:           # subiendo rápido (>2¢/obs)
            return 10
        if avg_change >= 0.005:         # subiendo gradual (0.5–2¢/obs) ← señal de momentum
            return 30
        if variation < 0.01:            # estable (<1¢ variación total)
            return 20
        return 0                        # cayendo o errático

    def _volume_score(self, volume: float) -> int:
        if volume >= SCORE_VOLUME_HIGH:
            return 20
        if volume >= SCORE_VOLUME_MID:
            return 15
        if volume >= SCORE_VOLUME_LOW:
            return 10
        return 0

    def _time_score(self, city: str) -> int:
        """Score por progreso dentro de la ventana horaria de la ciudad.

        Evalúa qué fracción de la ventana ya transcurrió (hora Chile),
        en lugar de la hora local absoluta. Así todas las ciudades compiten
        en igualdad sin importar en qué hora del día opera cada una.

        ≥ 75% de la ventana → 20 pts
        ≥ 50%               → 15 pts
        ≥ 25%               → 10 pts
        <  25%              →  5 pts
        fuera de ventana    →  0 pts
        """
        win = CITY_WINDOWS.get(city)
        if win is None:
            return 0

        # Hora actual en Chile (zona del observador)
        chile_now = _now_utc() + timedelta(hours=OBSERVER_UTC_OFFSET)
        now_mins = chile_now.hour * 60 + chile_now.minute

        open_h, open_m, close_h, close_m = win
        open_mins  = open_h  * 60 + open_m
        close_mins = close_h * 60 + close_m

        # Ventana que cruza medianoche (ej. Seoul 00–01h → close < open)
        if close_mins <= open_mins:
            close_mins += 24 * 60
            if now_mins < open_mins:
                now_mins += 24 * 60

        if now_mins < open_mins or now_mins >= close_mins:
            return 0

        duration = close_mins - open_mins
        elapsed  = now_mins - open_mins
        pct = elapsed / duration

        if pct >= 0.75:
            return 20
        if pct >= 0.50:
            return 15
        if pct >= 0.25:
            return 10
        return 5
