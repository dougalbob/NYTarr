import os
import re
import json
import time
import shutil
import logging
import threading
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import yaml
import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from web import create_app

# Paths (inside container)
CONFIG_DIR = "/config"
LOG_DIR = "/config/logs"
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yml")
STATE_PATH = os.path.join(CONFIG_DIR, "grabberstate.json")

os.makedirs(LOG_DIR, exist_ok=True)

# Logging setup
logger = logging.getLogger("nytarr")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "nytarr.log"),
    maxBytes=2_000_000,
    backupCount=5,
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Locks
state_lock = threading.Lock()
config_lock = threading.Lock()
run_lock = threading.Lock()

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

NYT_REGEX = re.compile(
    r"The[.\s_-]*New[.\s_-]*York[.\s_-]*Times[.\s_-]*-[.\s_-]*"
    r"(\d{1,2})[.\s_-]*(January|February|March|April|May|June|July|August|September|October|November|December)[.\s_-]*(\d{4})",
    re.IGNORECASE,
)


def load_config():
    with config_lock:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)


def save_config(cfg):
    with config_lock:
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)


def load_state():
    with state_lock:
        if not os.path.exists(STATE_PATH):
            return {"downloads": {}}
        try:
            with open(STATE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {"downloads": {}}


def save_state(state):
    with state_lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_PATH)


def get_now_local(cfg):
    tz = pytz.timezone(cfg.get("timezone", "Europe/London"))
    return datetime.now(tz)


def date_key(d):
    return d.strftime("%Y-%m-%d")


def target_filename(d):
    return f"New York Times - {d.strftime('%Y.%m.%d')}.pdf"


def prowlarr_search(cfg, query):
    """Search Prowlarr for a query, filtered to a specific indexer if set."""
    base = cfg["prowlarr"]["url"].rstrip("/")
    api_key = cfg["prowlarr"]["api_key"]
    indexer_name = cfg["prowlarr"].get("indexer_name", "").strip()

    headers = {"X-Api-Key": api_key}

    indexer_ids = []
    if indexer_name:
        try:
            r = requests.get(f"{base}/api/v1/indexer", headers=headers, timeout=15)
            r.raise_for_status()
            for idx in r.json():
                if idx.get("name", "").lower() == indexer_name.lower():
                    indexer_ids.append(idx.get("id"))
        except Exception as e:
            logger.error(f"Failed to fetch indexer list from Prowlarr: {e}")

    params = {"query": query, "type": "search", "limit": 100}
    if indexer_ids:
        params["indexerIds"] = indexer_ids

    try:
        r = requests.get(f"{base}/api/v1/search", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Prowlarr search failed: {e}")
        return []


def find_match_for_date(results, target_date):
    """Given prowlarr results, return the best match for target_date, or None."""
    target_day = target_date.day
    target_month = MONTHS[target_date.month - 1]
    target_year = target_date.year

    candidates = []
    for item in results:
        title = item.get("title", "")
        m = NYT_REGEX.search(title)
        if not m:
            continue
        day = int(m.group(1))
        month = m.group(2).capitalize()
        year = int(m.group(3))
        if day == target_day and month == target_month and year == target_year:
            candidates.append(item)

    if not candidates:
        return None

    # Pick largest size (usually most complete release)
    candidates.sort(key=lambda x: x.get("size", 0), reverse=True)
    return candidates[0]


def send_to_sabnzbd(cfg, nzb_url, nzb_name):
    """Send NZB URL to SABnzbd via addurl."""
    base = cfg["sabnzbd"]["url"].rstrip("/")
    api_key = cfg["sabnzbd"]["api_key"]
    category = cfg["sabnzbd"]["category"]

    params = {
        "mode": "addurl",
        "name": nzb_url,
        "nzbname": nzb_name,
        "cat": category,
        "apikey": api_key,
        "output": "json",
    }
    try:
        r = requests.get(f"{base}/api/", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status"):
            nzo_ids = data.get("nzo_ids", [])
            return nzo_ids[0] if nzo_ids else None
        else:
            logger.error(f"SABnzbd rejected NZB: {data}")
            return None
    except Exception as e:
        logger.error(f"Failed to send NZB to SABnzbd: {e}")
        return None


def sab_job_status(cfg, nzo_id):
    """Return 'queue', 'history_ok', 'history_fail', or 'unknown'."""
    base = cfg["sabnzbd"]["url"].rstrip("/")
    api_key = cfg["sabnzbd"]["api_key"]

    # Check queue
    try:
        r = requests.get(
            f"{base}/api/",
            params={"mode": "queue", "apikey": api_key, "output": "json"},
            timeout=15,
        )
        r.raise_for_status()
        slots = r.json().get("queue", {}).get("slots", [])
        for s in slots:
            if s.get("nzo_id") == nzo_id:
                return "queue"
    except Exception as e:
        logger.error(f"Failed to query SAB queue: {e}")

    # Check history
    try:
        r = requests.get(
            f"{base}/api/",
            params={"mode": "history", "apikey": api_key, "output": "json", "limit": 50},
            timeout=15,
        )
        r.raise_for_status()
        slots = r.json().get("history", {}).get("slots", [])
        for s in slots:
            if s.get("nzo_id") == nzo_id:
                status = s.get("status", "").lower()
                if status == "completed":
                    return "history_ok"
                elif status in ("failed", "deleted"):
                    return "history_fail"
                else:
                    return "queue"
    except Exception as e:
        logger.error(f"Failed to query SAB history: {e}")

    return "unknown"


def find_and_move_pdf(cfg, target_date):
    """Look inside the download folder for a matching PDF, move & rename."""
    download_folder = cfg["paths"]["download_folder"]
    media_folder = cfg["paths"]["media_folder"]
    os.makedirs(media_folder, exist_ok=True)

    if not os.path.isdir(download_folder):
        logger.error(f"Download folder does not exist: {download_folder}")
        return None

    target_day = target_date.day
    target_month = MONTHS[target_date.month - 1]
    target_year = target_date.year

    for root, dirs, files in os.walk(download_folder):
        for fname in files:
            if not fname.lower().endswith(".pdf"):
                continue
            m = NYT_REGEX.search(fname)
            if not m:
                continue
            if (int(m.group(1)) == target_day
                    and m.group(2).capitalize() == target_month
                    and int(m.group(3)) == target_year):
                src = os.path.join(root, fname)
                new_name = target_filename(target_date)
                dst = os.path.join(media_folder, new_name)

                if os.path.exists(dst):
                    logger.warning(f"Target already exists, skipping move: {dst}")
                    return dst

                try:
                    shutil.move(src, dst)
                    logger.info(f"Moved {src} -> {dst}")
                    return dst
                except Exception as e:
                    logger.error(f"Failed to move {src} -> {dst}: {e}")
                    return None

    return None


def process_date(cfg, target_date):
    """Full pipeline for a single target date. Returns True if newly completed."""
    state = load_state()
    key = date_key(target_date)
    entry = state["downloads"].get(key, {})

    if entry.get("status") == "completed":
        return False

    # If media file already exists, mark done
    media_folder = cfg["paths"]["media_folder"]
    expected = os.path.join(media_folder, target_filename(target_date))
    if os.path.exists(expected):
        logger.info(f"Media file already present for {key}, marking complete.")
        state["downloads"][key] = {
            "status": "completed",
            "file": expected,
            "completed_at": datetime.utcnow().isoformat() + "Z",
        }
        save_state(state)
        return True

    query = f"The New York Times {target_date.strftime('%d %B %Y')}"
    logger.info(f"Searching Prowlarr for {key}: '{query}'")
    results = prowlarr_search(cfg, query)
    logger.info(f"Prowlarr returned {len(results)} results for {key}")

    match = find_match_for_date(results, target_date)
    if not match:
        logger.info(f"No match found for {key}")
        state["downloads"][key] = {
            "status": "searching",
            "last_attempt": datetime.utcnow().isoformat() + "Z",
        }
        save_state(state)
        return False

    nzb_url = match.get("downloadUrl") or match.get("link") or match.get("guid")
    title = match.get("title", "")
    if not nzb_url:
        logger.error(f"Match found but no download URL: {title}")
        return False

    logger.info(f"Match found: {title} ({match.get('size', 0)} bytes) -> sending to SAB")
    nzo_id = send_to_sabnzbd(cfg, nzb_url, title)
    if not nzo_id:
        logger.error(f"Failed to enqueue in SABnzbd for {key}")
        return False

    state["downloads"][key] = {
        "status": "downloading",
        "nzo_id": nzo_id,
        "title": title,
        "queued_at": datetime.utcnow().isoformat() + "Z",
    }
    save_state(state)

    # Wait 5 minutes, then poll SAB up to ~30 min
    logger.info(f"Waiting 5 minutes before checking SAB for {key}")
    time.sleep(300)

    deadline = time.time() + 30 * 60
    while time.time() < deadline:
        status = sab_job_status(cfg, nzo_id)
        logger.info(f"SAB status for {key} ({nzo_id}): {status}")
        if status == "history_ok":
            break
        if status == "history_fail":
            logger.error(f"SAB reports failure for {key}")
            state = load_state()
            state["downloads"][key] = {
                "status": "failed",
                "nzo_id": nzo_id,
                "title": title,
                "failed_at": datetime.utcnow().isoformat() + "Z",
            }
            save_state(state)
            return False
        time.sleep(60)
    else:
        logger.warning(f"Timeout waiting for SAB completion for {key}")

    # Try to move the PDF
    moved = find_and_move_pdf(cfg, target_date)
    if moved:
        state = load_state()
        state["downloads"][key] = {
            "status": "completed",
            "nzo_id": nzo_id,
            "title": title,
            "file": moved,
            "completed_at": datetime.utcnow().isoformat() + "Z",
        }
        save_state(state)
        logger.info(f"Successfully completed {key}")
        return True
    else:
        logger.warning(f"Could not locate PDF for {key} after download")
        return False


def run_cycle():
    """Main scheduled cycle: try today + backfill window."""
    if not run_lock.acquire(blocking=False):
        logger.info("Previous cycle still running, skipping.")
        return

    try:
        cfg = load_config()
        now = get_now_local(cfg)
        backfill_days = int(cfg.get("backfill", {}).get("days", 3))

        # Build list: today, yesterday, ..., up to backfill_days back
        dates_to_try = [now.date() - timedelta(days=i) for i in range(backfill_days + 1)]

        state = load_state()
        for d in dates_to_try:
            key = date_key(d)
            entry = state["downloads"].get(key, {})
            if entry.get("status") == "completed":
                continue
            logger.info(f"Processing date: {key}")
            try:
                process_date(cfg, d)
            except Exception as e:
                logger.exception(f"Error processing {key}: {e}")
            # reload state after each
            state = load_state()
    finally:
        run_lock.release()


def trigger_manual_search():
    """Called from web UI."""
    t = threading.Thread(target=run_cycle, daemon=True)
    t.start()


def setup_scheduler(cfg):
    scheduler = BackgroundScheduler(timezone=cfg.get("timezone", "Europe/London"))

    start_hour = int(cfg["schedule"]["search_start_hour"])
    interval_hours = int(cfg["schedule"]["search_interval_hours"])

    # Daily kick-off at start_hour
    scheduler.add_job(
        run_cycle,
        CronTrigger(hour=start_hour, minute=0),
        id="daily_start",
        replace_existing=True,
    )

    # Recurring retries every N hours, but only between start_hour and 23:59
    scheduler.add_job(
        run_cycle,
        IntervalTrigger(hours=interval_hours),
        id="interval_retry",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"Scheduler started. Daily at {start_hour}:00, retry every {interval_hours}h.")
    return scheduler


def main():
    logger.info("=" * 60)
    logger.info("NYTarr starting up")
    logger.info("=" * 60)

    cfg = load_config()
    scheduler = setup_scheduler(cfg)

    # Ensure state file exists
    if not os.path.exists(STATE_PATH):
        save_state({"downloads": {}})

    # Start Flask
    flask_app = create_app(
        load_config=load_config,
        save_config=save_config,
        load_state=load_state,
        log_path=os.path.join(LOG_DIR, "nytarr.log"),
        trigger_manual_search=trigger_manual_search,
        reschedule_cb=lambda: reschedule(scheduler),
    )

    flask_app.run(host="0.0.0.0", port=6789, debug=False, use_reloader=False)


def reschedule(scheduler):
    """Reload config and reschedule jobs after settings change."""
    cfg = load_config()
    start_hour = int(cfg["schedule"]["search_start_hour"])
    interval_hours = int(cfg["schedule"]["search_interval_hours"])

    scheduler.reschedule_job("daily_start", trigger=CronTrigger(hour=start_hour, minute=0))
    scheduler.reschedule_job("interval_retry", trigger=IntervalTrigger(hours=interval_hours))
    logger.info(f"Rescheduled: daily at {start_hour}:00, retry every {interval_hours}h.")


if __name__ == "__main__":
    main()
