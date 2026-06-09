from __future__ import annotations
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from controller import MonitorController
from profiles import SearchProfile
from proxy import ProfileProxyStore, redact_proxy_url
from settings import AppSettings, SettingsStore
from storage import MonitorStore


def create_app(database_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(database_path or os.getenv("DATABASE_PATH", "ebay_monitor.db"))

    with SettingsStore(app.config["DATABASE_PATH"]) as ss:
        initial = ss.load()

    app.extensions["monitor_controller"] = MonitorController(
        app.config["DATABASE_PATH"],
        initial.check_interval_seconds,
    )

    def store():
        return MonitorStore(app.config["DATABASE_PATH"])

    def settings_store():
        return SettingsStore(app.config["DATABASE_PATH"])

    @app.get("/")
    def index():
        with store() as database, settings_store() as ss:
            s = ss.load()
            data = database.dashboard(
                request.args.get("profile", type=int),
                shipping_cost=Decimal(str(s.shipping_cost_eur)),
                fee_rate=Decimal(str(s.ebay_fee_rate)),
            )
        return render_template(
            "dashboard.html",
            monitor_status=app.extensions["monitor_controller"].status(),
            **data,
        )

    @app.post("/monitor/scan")
    def monitor_scan():
        app.extensions["monitor_controller"].scan_once()
        return redirect(request.referrer or url_for("index"))

    @app.post("/monitor/start")
    def monitor_start():
        app.extensions["monitor_controller"].start()
        return redirect(request.referrer or url_for("index"))

    @app.post("/monitor/stop")
    def monitor_stop():
        app.extensions["monitor_controller"].stop()
        return redirect(request.referrer or url_for("index"))

    @app.get("/api/monitor/status")
    def monitor_status():
        return jsonify(app.extensions["monitor_controller"].status())

    @app.route("/settings", methods=["GET", "POST"])
    def app_settings():
        saved = False
        with settings_store() as ss:
            if request.method == "POST":
                new_settings = AppSettings(
                    discord_webhook_url=request.form.get("discord_webhook_url", "").strip(),
                    check_interval_seconds=max(30, int(request.form.get("check_interval_seconds", "300") or 300)),
                    notify_existing=request.form.get("notify_existing") == "on",
                    notify_price_increases=request.form.get("notify_price_increases") == "on",
                    notify_statistics=request.form.get("notify_statistics") == "on",
                    browser_fetch=request.form.get("browser_fetch") == "on",
                    shipping_cost_eur=max(0.0, float(request.form.get("shipping_cost_eur") or 5.0)),
                    ebay_fee_rate=max(0.0, min(1.0, float(request.form.get("ebay_fee_rate") or 0.1235))),
                    deepseek_api_key=request.form.get("deepseek_api_key", "").strip(),
                    nvidia_api_key=request.form.get("nvidia_api_key", "").strip(),
                    ai_provider=request.form.get("ai_provider", "none").strip(),
                )
                ss.save(new_settings)
                # Apply new interval to running controller
                app.extensions["monitor_controller"].interval_seconds = new_settings.check_interval_seconds
                saved = True
            current = ss.load()
        return render_template("settings.html", s=current, saved=saved)

    @app.route("/profiles/new", methods=["GET", "POST"])
    @app.route("/profiles/<int:profile_id>", methods=["GET", "POST"])
    def profile_form(profile_id=None):
        with store() as database, ProfileProxyStore(app.config["DATABASE_PATH"]) as proxies:
            current = database.profile(profile_id) if profile_id else None
            if request.method == "POST":
                ref_url = request.form.get("ebay_reference_url", "").strip() or None
                ct_url = request.form.get("clevertronic_url", "").strip() or None
                zoxs_url_val = request.form.get("zoxs_url", "").strip() or None
                wkfs_url_val = request.form.get("wirkaufens_url", "").strip() or None
                buyback_platforms = request.form.getlist("buyback_platforms")
                profile = SearchProfile(
                    id=profile_id, name=request.form["name"].strip(),
                    ebay_url=request.form["ebay_url"].strip(),
                    include_keywords=request.form.get("include_keywords", "").strip(),
                    exclude_keywords=request.form.get("exclude_keywords", "").strip(),
                    min_price=_decimal(request.form.get("min_price")),
                    max_price=_decimal(request.form.get("max_price")),
                    currency=request.form.get("currency") or None,
                    sold_window_days=max(1, int(request.form.get("sold_window_days", "90"))),
                    enabled=request.form.get("enabled") == "on",
                    ebay_reference_url=ref_url,
                    clevertronic_url=ct_url,
                    zoxs_url=zoxs_url_val,
                    wirkaufens_url=wkfs_url_val,
                    buyback_platforms=buyback_platforms,
                )
                saved_id = database.save_profile(profile)
                new_proxy = request.form.get("proxy_url", "").strip()
                if new_proxy:
                    proxies.set(saved_id, new_proxy)
                elif request.form.get("clear_proxy") == "on":
                    proxies.delete(saved_id)
                return redirect(url_for("index", profile=saved_id))
            existing_proxy = proxies.get(profile_id) if profile_id else None
        return render_template(
            "profile.html", profile=current, proxy_configured=bool(existing_proxy),
            proxy_display=redact_proxy_url(existing_proxy),
        )

    @app.post("/profiles/<int:profile_id>/delete")
    def delete_profile(profile_id):
        with store() as database, ProfileProxyStore(app.config["DATABASE_PATH"]) as proxies:
            proxies.delete(profile_id)
            database.delete_profile(profile_id)
        return redirect(url_for("index"))

    @app.get("/api/profiles/<int:profile_id>/trend")
    def trend(profile_id):
        with store() as database:
            return jsonify(database.dashboard(profile_id)["trend"])

    @app.get("/api/price-history")
    def price_history():
        with store() as database:
            return jsonify(database.price_history(request.args["link"]))

    @app.get("/api/buyback/search")
    def buyback_search():
        """Search all buyback/refurbished sites by keyword. Returns grouped results."""
        from buyback_search import search_wirkaufens, search_zoxs, search_clevertronic
        import concurrent.futures

        q = request.args.get("q", "").strip()
        if len(q) < 3:
            return jsonify({"wirkaufens": [], "zoxs": [], "clevertronic": []})

        # Run all three searches in parallel threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            wkfs_f = ex.submit(search_wirkaufens, q)
            zoxs_f = ex.submit(search_zoxs, q)
            ct_f   = ex.submit(search_clevertronic, q)

        return jsonify({
            "wirkaufens":   wkfs_f.result(),
            "zoxs":         zoxs_f.result(),
            "clevertronic": ct_f.result(),
        })

    return app


def _decimal(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"Invalid decimal value: {value}") from error


if __name__ == "__main__":
    create_app().run(
        host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("DASHBOARD_PORT", "5000")),
        debug=False,
    )
