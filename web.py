from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort, Response
import os
import secrets
import hmac
from config import ready
from collections import Counter, deque


def create_app(load_config, save_config, load_state, log_path,
               trigger_manual_search, reschedule_cb):
    app = Flask(__name__)
    app.secret_key = os.environ.get("NYTARR_SECRET_KEY") or secrets.token_hex(32)

    app.config.update(MAX_CONTENT_LENGTH=64 * 1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true")

    @app.before_request
    def protect():
        if request.endpoint == 'health':
            return None
        password = os.environ.get('WEB_PASSWORD', '')
        if password:
            auth = request.authorization
            if not auth or not hmac.compare_digest(auth.username or '', os.environ.get('WEB_USER', 'admin')) or not hmac.compare_digest(auth.password or '', password):
                return Response('Authentication required', 401,
                                {'WWW-Authenticate': 'Basic realm="NYTarr"'})
        if request.method == 'POST':
            token = session.get('csrf_token', '')
            if not token or not hmac.compare_digest(token, request.form.get('csrf_token', '')):
                abort(400, 'Invalid form token. Reload the page and try again.')

    @app.context_processor
    def csrf_context():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return {'csrf_token': session['csrf_token']}

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; form-action 'self'; base-uri 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/health')
    def health():
        healthy = app.config.get('HEALTH_CHECK', lambda: True)()
        return jsonify(status='ok' if healthy else 'unhealthy'), 200 if healthy else 503

    @app.route("/")
    def index():
        state = load_state()
        downloads = state.get("downloads", {})
        sorted_items = sorted(downloads.items(), key=lambda x: x[0], reverse=True)
        query = request.args.get("q", "").strip()
        selected_status = request.args.get("status", "")
        visible = [(key, entry) for key, entry in sorted_items
                   if (not selected_status or entry.get("status") == selected_status)
                   and (not query or query.casefold() in
                        " ".join(str(value) for value in [key, entry.get("title", ""), entry.get("file", "")]).casefold())]
        return render_template("index.html", downloads=sorted_items,
                               visible_downloads=visible, query=query,
                               selected_status=selected_status,
                               counts=Counter("failed" if entry.get("status") in ("review", "submitting") else entry.get("status") for _, entry in sorted_items),
                               cfg=load_config())

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = load_config()
        if request.method == "POST":
            try:
                cfg["schedule"]["search_start_hour"] = int(request.form["start_hour"])
                cfg["schedule"]["search_interval_hours"] = int(request.form["interval_hours"])

                cfg["backfill"]["days"] = int(request.form["backfill_days"])

                if not 0 <= cfg["schedule"]["search_start_hour"] <= 23:
                    raise ValueError("Start hour must be between 0 and 23.")
                if not 1 <= cfg["schedule"]["search_interval_hours"] <= 24:
                    raise ValueError("Retry interval must be between 1 and 24 hours.")
                if not 0 <= cfg["backfill"]["days"] <= 30:
                    raise ValueError("Backfill must be between 0 and 30 days.")
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
                lines = list(deque(f, maxlen=500))
        except Exception as e:
            lines = [f"Could not read log: {e}"]
        return render_template("logs.html", lines="".join(lines))

    @app.route("/search-now", methods=["POST"])
    def search_now():
        if not ready(load_config()):
            flash("Set Prowlarr and SABnzbd URLs and API keys in container settings first.", "error")
            return redirect(url_for("settings"))
        trigger_manual_search()
        flash("Manual search triggered.", "success")
        return redirect(url_for("index"))

    @app.route("/api/state")
    def api_state():
        return jsonify(load_state())

    return app
