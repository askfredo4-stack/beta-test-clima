"""portfolio.py — V2: Buy cheap YES tokens (5 cities, 12-17h local).

Entry: YES 0.06–0.12 (score-filtered)
Take profit: YES >= TAKE_PROFIT_YES (0.15)
Resolution WON: YES resolves to 0.99+ (event happened)
Resolution LOST: NO resolves to 0.99+ (event didn't happen — we lose the YES investment)
"""

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from app.scanner import now_utc, fetch_market_live, get_prices
from app.config import (
    MAX_POSITIONS, TAKE_PROFIT_YES,
    REGION_MAP, MAX_REGION_EXPOSURE,
)
import app.db as db

log = logging.getLogger(__name__)


class AutoPortfolio:
    def __init__(self, initial_capital):
        self.lock = threading.Lock()
        self.capital_inicial    = initial_capital
        self.capital_total      = initial_capital
        self.capital_disponible = initial_capital
        self.positions          = {}
        self.closed_positions   = []
        self.session_start      = now_utc()
        self.capital_history    = [
            {"time": now_utc().isoformat(), "capital": initial_capital}
        ]
        self._cap_record_count = 0

    def can_open_position(self):
        return (len(self.positions) < MAX_POSITIONS and
                self.capital_disponible >= 0.50)

    def open_position(self, opp, amount):
        yes_price = opp["yes_price"]
        tokens = amount / yes_price

        pos = {
            **opp,
            "entry_time":  now_utc().isoformat(),
            "entry_yes":   yes_price,
            "current_yes": yes_price,
            "allocated":   amount,
            "tokens":      tokens,
            "take_profit": TAKE_PROFIT_YES,
            "status":      "OPEN",
            "pnl":         0.0,
        }
        cid = opp["condition_id"]
        self.positions[cid] = pos
        self.capital_disponible -= amount
        db.upsert_open_position(cid, pos)
        db.save_state(self.capital_inicial, self.capital_total,
                      self.capital_disponible, self.session_start)
        return True

    def get_position_slugs(self):
        return [
            (cid, pos["slug"], pos.get("yes_token_id"))
            for cid, pos in self.positions.items()
        ]

    def apply_price_updates(self, price_map):
        """Apply {cid: (yes_price, no_price)} and handle exits.
        Must be called with self.lock held."""
        to_close = []

        for cid, (yes_price, no_price) in price_map.items():
            if cid not in self.positions:
                continue
            pos = self.positions[cid]
            pos["current_yes"] = yes_price

            # 1. YES resolved (event happened) — jackpot
            if yes_price >= 0.99:
                sale_value   = pos["tokens"] * yes_price
                realized_pnl = sale_value - pos["allocated"]
                resolution   = (
                    f"YES resolvió — temperatura superó el umbral "
                    f"(YES={yes_price*100:.1f}¢)"
                )
                to_close.append((cid, "WON", realized_pnl, resolution))
                continue

            # 2. NO resolved (event didn't happen) — lose YES investment
            if no_price >= 0.99:
                resolution = (
                    f"NO resolvió — temperatura no superó el umbral "
                    f"(perdemos inversión YES)"
                )
                to_close.append((cid, "LOST", -pos["allocated"], resolution))
                continue

            # 3. Take profit: YES reached 0.15
            if yes_price >= TAKE_PROFIT_YES:
                sale_value   = pos["tokens"] * yes_price
                realized_pnl = sale_value - pos["allocated"]
                gain_pct     = realized_pnl / pos["allocated"] * 100
                resolution   = (
                    f"Take profit @ YES={yes_price*100:.1f}¢ "
                    f"(entrada {pos['entry_yes']*100:.1f}¢, +{gain_pct:.0f}%)"
                )
                to_close.append((cid, "TAKE_PROFIT", realized_pnl, resolution))
                continue

        for cid, status, pnl, resolution in to_close:
            self._close_position(cid, status, pnl, resolution)

    def _close_position(self, cid, status, pnl, resolution=""):
        if cid not in self.positions:
            return
        pos = self.positions[cid]
        pos["status"]     = status
        pos["pnl"]        = pnl
        pos["close_time"] = now_utc().isoformat()
        pos["resolution"] = resolution

        recovered = pos["allocated"] + pnl
        self.capital_disponible += recovered
        self.capital_total      += pnl

        closed_pos = pos.copy()
        self.closed_positions.append(closed_pos)
        del self.positions[cid]
        db.delete_open_position(cid)
        db.insert_closed_position(closed_pos)
        db.save_state(self.capital_inicial, self.capital_total,
                      self.capital_disponible, self.session_start)

    # ── Region exposure ────────────────────────────────────────────────────────

    def get_region_allocated(self, region):
        return sum(
            pos["allocated"]
            for pos in self.positions.values()
            if REGION_MAP.get(pos.get("city", ""), "other") == region
        )

    def region_has_capacity(self, city):
        region    = REGION_MAP.get(city, "other")
        allocated = self.get_region_allocated(region)
        return allocated < self.capital_total * MAX_REGION_EXPOSURE

    # ── Learning insights ─────────────────────────────────────────────────────

    def compute_insights(self):
        exclude = {"LIQUIDATED"}
        closed  = [p for p in self.closed_positions if p["status"] not in exclude]
        if len(closed) < 5:
            return None

        by_hour = defaultdict(lambda: {"won": 0, "total": 0})
        by_city = defaultdict(lambda: {"won": 0, "total": 0, "pnl": 0.0,
                                       "gross_win": 0.0, "gross_loss": 0.0})

        for pos in closed:
            try:
                hour = int(pos["entry_time"][11:13])
            except Exception:
                hour = -1
            city = pos.get("city", "unknown")
            pnl  = pos["pnl"]
            won  = pnl > 0

            if hour >= 0:
                by_hour[hour]["total"] += 1
                if won:
                    by_hour[hour]["won"] += 1

            by_city[city]["total"]      += 1
            by_city[city]["pnl"]        += pnl
            by_city[city]["gross_win"]  += max(pnl, 0.0)
            by_city[city]["gross_loss"] += abs(min(pnl, 0.0))
            if won:
                by_city[city]["won"] += 1

        total       = len(closed)
        won_total   = sum(1 for p in closed if p["pnl"] > 0)
        gross_wins  = sum(p["pnl"] for p in closed if p["pnl"] > 0)
        gross_loss  = abs(sum(p["pnl"] for p in closed if p["pnl"] <= 0))
        pf_global   = round(gross_wins / gross_loss, 2) if gross_loss > 0 else None

        hour_stats = sorted(
            [{"hour": h, "win_rate": round(v["won"] / v["total"], 2), "trades": v["total"]}
             for h, v in by_hour.items() if v["total"] >= 2],
            key=lambda x: x["win_rate"], reverse=True,
        )
        city_stats = sorted(
            [{"city": c,
              "win_rate":      round(v["won"] / v["total"], 2),
              "trades":        v["total"],
              "pnl":           round(v["pnl"], 2),
              "profit_factor": round(v["gross_win"] / v["gross_loss"], 2) if v["gross_loss"] > 0 else None,
             }
             for c, v in by_city.items() if v["total"] >= 2],
            key=lambda x: x["win_rate"], reverse=True,
        )

        return {
            "overall_win_rate": round(won_total / total, 2),
            "total_trades":     total,
            "profit_factor":    pf_global,
            "by_hour":          hour_stats[:6],
            "by_city":          city_stats[:6],
        }

    # ── State persistence ─────────────────────────────────────────────────────

    def save_state(self):
        db.save_state(self.capital_inicial, self.capital_total,
                      self.capital_disponible, self.session_start)

    def load_state(self):
        """Restaura estado desde DB al arrancar. Devuelve True si OK."""
        s = db.load_state()
        if not s:
            return False
        try:
            self.capital_inicial    = s["capital_inicial"]
            self.capital_total      = s["capital_total"]
            self.capital_disponible = s["capital_disponible"]
            self.positions          = db.load_open_positions()
            self.closed_positions   = db.load_closed_positions()
            hist = db.load_capital_history()
            if hist:
                self.capital_history = hist
            self.session_start = datetime.fromisoformat(s["session_start"])
            log.info(
                "Estado restaurado desde DB: capital=%.2f  abiertas=%d  cerradas=%d",
                self.capital_total, len(self.positions), len(self.closed_positions),
            )
            return True
        except Exception as e:
            log.warning("load_state error: %s", e)
            return False

    # ── Capital snapshot ──────────────────────────────────────────────────────

    def record_capital(self):
        ts = now_utc().isoformat()
        # Mark-to-market: available cash + current value of open positions
        open_value  = sum(pos["tokens"] * pos["current_yes"] for pos in self.positions.values())
        mtm_capital = round(self.capital_disponible + open_value, 2)
        point = {"time": ts, "capital": mtm_capital}
        self.capital_history.append(point)
        if len(self.capital_history) > 500:
            self.capital_history = self.capital_history[-500:]
        self._cap_record_count += 1
        if self._cap_record_count % 120 == 0:  # cada ~1h (120 ciclos × 30s)
            db.append_capital_point(ts, mtm_capital)

    def snapshot(self):
        pnl = self.capital_total - self.capital_inicial
        roi = (pnl / self.capital_inicial * 100) if self.capital_inicial else 0

        exclude    = {"LIQUIDATED"}
        won        = sum(1 for p in self.closed_positions if p["pnl"] > 0  and p["status"] not in exclude)
        lost       = sum(1 for p in self.closed_positions if p["pnl"] <= 0 and p["status"] not in exclude)
        take_profit_count = sum(1 for p in self.closed_positions if p["status"] == "TAKE_PROFIT")
        stopped    = sum(1 for p in self.closed_positions if p["status"] == "STOPPED")
        liquidated = sum(1 for p in self.closed_positions if p["status"] == "LIQUIDATED")

        open_positions = []
        for pos in list(self.positions.values()):
            float_pnl = pos["tokens"] * pos["current_yes"] - pos["allocated"]
            open_positions.append({
                "question":    pos["question"],
                "city":        pos.get("city", ""),
                "entry_yes":   pos["entry_yes"],
                "current_yes": pos["current_yes"],
                "take_profit": pos["take_profit"],
                "allocated":   round(pos["allocated"], 2),
                "pnl":         round(float_pnl, 2),
                "entry_time":  pos["entry_time"],
                "status":      pos["status"],
            })

        closed = []
        for pos in self.closed_positions:
            closed.append({
                "question":   pos["question"],
                "entry_yes":  pos["entry_yes"],
                "allocated":  round(pos["allocated"], 2),
                "pnl":        round(pos["pnl"], 2),
                "status":     pos["status"],
                "resolution": pos.get("resolution", ""),
                "entry_time": pos["entry_time"],
                "close_time": pos.get("close_time", ""),
            })

        return {
            "capital_inicial":    round(self.capital_inicial, 2),
            "capital_total":      round(self.capital_total, 2),
            "capital_disponible": round(self.capital_disponible, 2),
            "pnl":                round(pnl, 2),
            "roi":                round(roi, 2),
            "won":                won,
            "lost":               lost,
            "take_profit":        take_profit_count,
            "stopped":            stopped,
            "liquidated":         liquidated,
            "open_positions":     open_positions,
            "closed_positions":   closed,
            "capital_history":    self.capital_history,
            "session_start":      self.session_start.isoformat(),
            "insights":           self.compute_insights(),
        }
