from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import os


def create_app(load_config, save_config, load_state, log_path,
               trigger_manual_search, reschedule_cb):
    app = Flask(__name__)
    app.secret_key = "nytarr-secret-key-change-me"

    @app.route("/")
    def index():
        state = load_state()
        downloads = state.get("downloads", {})
        sorted_items = sorted(downloads.items(), key=lambda x: x[0], reverse=True)
        return render_template("index.html", downloads=sorted_items)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = load_config()
        if request.method == "POST":
            try:
                cfg["prowlarr"]["url"] = request.form["prowlarr_url"].strip()
                cfg["prowlarr"]["api_key"] = request.form["prowlarr_api_key"].strip()
                cfg["prowlarr"]["indexer_name"] = request.form["prowlarr_indexer"].strip()

                cfg["sabnzbd"]["url"] = request.form["sab_url"].strip()
                cfg["sabnzbd"]["api_key"] = request.form["sab_api_key"].strip()
                cfg["sabnzbd"]["category"] = request.form["sab_category"].strip()

                cfg["schedule"]["search_start_hour"] = int(request.form["start_hour"])
                cfg["schedule"]["search_interval_hours"] = int(request.form["interval_hours"])

                cfg["paths"]["download_folder"] = request.form["download_folder"].strip()
                cfg["paths"]["media_folder"] = request.form["media_folder"].strip()

                cfg["backfill"]["days"] = int(request.form["backfill_days"])

                save_config(cfg)
                reschedule_cb()
                flash("Settings saved.", "success")
                return redirect(url_for("settings"))
            except Exception as e:
                flash(f"Error saving settings: {e}", "error")
        return render_template("settings.html", cfg=cfg)

    @app.route("/logs")
    def logs():
        lines = []
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()[-500:]
        except Exception as e:
            lines = [f"Could not read log: {e}"]
        return render_template("logs.html", lines="".join(lines))

    @app.route("/search-now", methods=["POST"])
    def search_now():
        trigger_manual_search()
        flash("Manual search triggered.", "success")
        return redirect(url_for("index"))

    @app.route("/api/state")
    def api_state():
        return jsonify(load_state())

    return app
