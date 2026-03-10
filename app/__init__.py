import logging
from flask import Flask

from app.config import INITIAL_CAPITAL, AUTO_START
from app import db
from app.portfolio import AutoPortfolio
from app.market_scorer import MarketScorer
from app.bot import BotRunner
from app.routes import bp, init_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def create_app():
    app = Flask(__name__)

    db.init_db()
    portfolio = AutoPortfolio(INITIAL_CAPITAL)
    portfolio.load_state()
    scorer    = MarketScorer()
    bot       = BotRunner(portfolio, scorer)

    init_routes(bot, portfolio, scorer)
    app.register_blueprint(bp)

    if AUTO_START:
        bot.start()

    return app
