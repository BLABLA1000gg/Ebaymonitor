import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from profiles import SearchProfile
from storage import MonitorStore


def create_app(database_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(database_path or os.getenv("DATABASE_PATH", "ebay_monitor.db"))

    def store():
        return MonitorStore(app.config["DATABASE_PATH"])

    @app.get("/")
    def index():
        profile_id = request.args.get("profile", type=int)
        with store() as database:
            data = database.dashboard(profile_id)
        return render_template("dashboard.html", **data)

    @app.route("/profiles/new", methods=["GET", "POST"])
    @app.route("/profiles/<int:profile_id>", methods=["GET", "POST"])
    def profile_form(profile_id=None):
        with store() as database:
            current = database.profile(profile_id) if profile_id else None
            if request.method == "POST":
                profile = SearchProfile(
                    id=profile_id,
                    name=request.form["name"].strip(),
                    ebay_url=request.form["ebay_url"].strip(),
                    include_keywords=request.form.get("include_keywords", "").strip(),
                    exclude_keywords=request.form.get("exclude_keywords", "").strip(),
                    min_price=_decimal(request.form.get("min_price")),
                    max_price=_decimal(request.form.get("max_price")),
                    currency=request.form.get("currency") or None,
                    sold_window_days=max(1, int(request.form.get("sold_window_days", "90"))),
                    enabled=request.form.get("enabled") == "on",
                )
                saved_id = database.save_profile(profile)
                return redirect(url_for("index", profile=saved_id))
        return render_template("profile.html", profile=current)

    @app.post("/profiles/<int:profile_id>/delete")
    def delete_profile(profile_id):
        with store() as database:
            database.delete_profile(profile_id)
        return redirect(url_for("index"))

    @app.get("/api/profiles/<int:profile_id>/trend")
    def trend(profile_id):
        with store() as database:
            data = database.dashboard(profile_id)["trend"]
        return jsonify(data)

    @app.get("/api/price-history")
    def price_history():
        with store() as database:
            data = database.price_history(request.args["link"])
        return jsonify(data)

    return app


def _decimal(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"Invalid decimal value: {value}") from error


if __name__ == "__main__":
    create_app().run(host=os.getenv("DASHBOARD_HOST", "127.0.0.1"), port=int(os.getenv("DASHBOARD_PORT", "5000")), debug=False)
