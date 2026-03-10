from flask import Blueprint, render_template, jsonify

from app.bot import is_weekend, get_entry_thresholds
from app.config import WEEKEND_ENABLED

bp = Blueprint("main", __name__)

bot = None
portfolio = None
scorer = None


def init_routes(bot_instance, portfolio_instance, scorer_instance):
    global bot, portfolio, scorer
    bot = bot_instance
    portfolio = portfolio_instance
    scorer = scorer_instance


@bp.route("/")
def dashboard():
    return render_template("dashboard.html")


@bp.route("/api/status")
def api_status():
    with portfolio.lock:
        snap = portfolio.snapshot()
    snap["bot_status"]   = bot.status if bot else "unknown"
    snap["scan_count"]   = bot.scan_count if bot else 0
    snap["last_opportunities"] = bot.last_opportunities if bot else []
    lpu = bot.last_price_update if bot else None
    snap["last_price_update"] = lpu.isoformat() if lpu else None
    snap["price_thread_alive"] = (
        bot._price_thread is not None and bot._price_thread.is_alive()
    ) if bot else False

    yes_min, yes_max, min_score, regime = get_entry_thresholds()
    snap["regime"]           = bot.active_regime if bot else regime
    snap["regime_blocked"]   = (yes_min is None)
    snap["weekend_enabled"]  = WEEKEND_ENABLED
    snap["active_yes_min"]   = yes_min if yes_min is not None else 0.06
    snap["active_yes_max"]   = yes_max if yes_max is not None else 0.12
    snap["active_min_score"] = min_score if min_score is not None else 60

    if scorer:
        all_scores = scorer.get_all_scores()
        snap["tracked_markets"]  = len(all_scores)
        snap["high_score_count"] = sum(1 for s in all_scores.values() if s["total"] >= 60)
    else:
        snap["tracked_markets"]  = 0
        snap["high_score_count"] = 0

    return jsonify(snap)


@bp.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    bot.start()
    return jsonify({"status": "running"})


@bp.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    bot.stop()
    return jsonify({"status": "stopped"})


@bp.route("/api/scores")
def api_scores():
    if not scorer:
        return jsonify({})
    return jsonify(scorer.get_all_scores())
