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

from pathlib import Path
import signal
from config import ConfigStore, atomic_write, ready

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
STATE_PATH = os.path.join(CONFIG_DIR, "grabberstate.json")
logger = logging.getLogger("nytarr")
state_lock = threading.RLock()
run_lock = threading.Lock()
stop_event = threading.Event()
store = None


def initialize():
    global store
    store = ConfigStore(CONFIG_DIR)
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for handler in (RotatingFileHandler(os.path.join(LOG_DIR, "nytarr.log"),
                                       maxBytes=2_000_000, backupCount=5),
                    logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    load_state()  # Fail closed on corrupt history, never silently re-download it.


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
    return store.load()


def save_config(cfg):
    store.save(cfg)


def load_state():
    with state_lock:
        if not os.path.exists(STATE_PATH):
            return {"downloads": {}}
        state = json.loads(Path(STATE_PATH).read_text())
        if not isinstance(state, dict) or not isinstance(state.get("downloads"), dict):
            raise ValueError("Invalid download history; restore grabberstate.json from backup.")
        for key, entry in state["downloads"].items():
            datetime.strptime(key, "%Y-%m-%d")
            if not isinstance(entry, dict):
                raise ValueError("Invalid history entry.")
        return state


def save_state(state):
    with state_lock:
        atomic_write(STATE_PATH, json.dumps(state, indent=2))


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
            logger.error(f"Failed to fetch indexer list from Prowlarr: {type(e).__name__}")

    if indexer_name and not indexer_ids:
        logger.error("Configured Prowlarr indexer unavailable; refusing unrestricted search.")
        return []

    params = {"query": query, "type": "search", "limit": 100}
    if indexer_ids:
        params["indexerIds"] = indexer_ids

    try:
        r = requests.get(f"{base}/api/v1/search", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Prowlarr search failed: {type(e).__name__}")
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
        r = requests.post(f"{base}/api/", data=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status"):
            nzo_ids = data.get("nzo_ids", [])
            return nzo_ids[0] if nzo_ids else None
        else:
            logger.error("SABnzbd rejected submission; check SABnzbd logs.")
            return None
    except Exception as e:
        logger.error(f"Failed to send NZB to SABnzbd: {type(e).__name__}")
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
        logger.error(f"Failed to query SAB queue: {type(e).__name__}")

    # Check history
    try:
        r = requests.get(
            f"{base}/api/",
            params={"mode": "history", "apikey": api_key, "output": "json", "nzo_ids": nzo_id, "limit": 100},
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
        logger.error(f"Failed to query SAB history: {type(e).__name__}")

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
                    if os.path.islink(src) or not Path(src).resolve().is_relative_to(Path(download_folder).resolve()):
                        continue
                    with open(src, "rb") as source:
                        if source.read(5) != b"%PDF-":
                            logger.warning("Skipping file without a PDF header")
                            continue
                    tmp = dst + ".importing"
                    try:
                        with open(src, "rb") as source, open(tmp, "wb") as output:
                            shutil.copyfileobj(source, output)
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(tmp, dst)
                        os.unlink(src)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    logger.info(f"Moved {src} -> {dst}")
                    return dst
                except Exception as e:
                    logger.error(f"Failed to move {src} -> {dst}: {type(e).__name__}")
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

    if entry.get("status") in ("submitting", "review", "failed"):
        return False  # Explicit review required, never blindly resubmit.
    if entry.get("status") == "downloading":
        return monitor_entry(cfg, target_date, state, entry)

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
    state["downloads"][key] = {"status": "submitting", "title": title,
                                "queued_at": datetime.utcnow().isoformat() + "Z"}
    save_state(state)  # Persist intent before performing a non-idempotent request.
    nzo_id = send_to_sabnzbd(cfg, nzb_url, title)
    if not nzo_id:
        logger.error(f"Submission outcome uncertain for {key}; manual review required")
        state["downloads"][key]["status"] = "review"
        save_state(state)
        return False

    state["downloads"][key] = {
        "status": "downloading",
        "nzo_id": nzo_id,
        "title": title,
        "queued_at": datetime.utcnow().isoformat() + "Z",
    }
    save_state(state)

    return False


def monitor_entry(cfg, target_date, state, entry):
    key = date_key(target_date)
    if not entry.get("nzo_id"):
        entry["status"] = "review"
    else:
        status = sab_job_status(cfg, entry["nzo_id"])
        if status == "history_ok":
            moved = find_and_move_pdf(cfg, target_date)
            if moved:
                entry.update(status="completed", file=moved,
                             completed_at=datetime.utcnow().isoformat() + "Z")
            else:
                logger.warning("Completed SAB job for %s awaiting matching PDF", key)
        elif status == "history_fail":
            entry.update(status="failed", failed_at=datetime.utcnow().isoformat() + "Z")
        elif status == "unknown":
            logger.warning("SAB job for %s not visible; retaining job ID, no resubmission", key)
    state["downloads"][key] = entry
    save_state(state)
    return entry.get("status") == "completed"


def monitor_downloads():
    if not run_lock.acquire(blocking=False):
        return
    try:
        cfg = load_config()
        if not ready(cfg):
            return
        state = load_state()
        for key, entry in state["downloads"].items():
            if stop_event.is_set():
                break
            if entry.get("status") == "submitting":
                entry["status"] = "review"
                save_state(state)
            elif entry.get("status") == "downloading":
                monitor_entry(cfg, datetime.strptime(key, "%Y-%m-%d").date(), state, entry)
    except Exception as e:
        logger.error("Monitor failed: %s", type(e).__name__)
    finally:
        run_lock.release()


def run_cycle():
    """Main scheduled cycle: try today + backfill window."""
    if not run_lock.acquire(blocking=False):
        logger.info("Previous cycle still running, skipping.")
        return

    try:
        cfg = load_config()
        if not ready(cfg):
            logger.info("Set service URLs and keys in container settings before searching.")
            return
        now = get_now_local(cfg)
        backfill_days = int(cfg.get("backfill", {}).get("days", 3))

        # Build list: today, yesterday, ..., up to backfill_days back
        dates_to_try = [now.date() - timedelta(days=i) for i in range(backfill_days + 1)]

        state = load_state()
        for d in dates_to_try:
            if stop_event.is_set():
                break
            key = date_key(d)
            entry = state["downloads"].get(key, {})
            if entry.get("status") == "completed":
                continue
            logger.info(f"Processing date: {key}")
            try:
                process_date(cfg, d)
            except Exception as e:
                logger.error(f"Error processing {key}: {type(e).__name__}")
            # reload state after each
            state = load_state()
    finally:
        run_lock.release()


def trigger_manual_search():
    # Queue through the single scheduler rather than spawning unbounded threads.
    scheduler.add_job(run_cycle, id="manual_search", replace_existing=True)


def search_trigger(cfg):
    start = cfg["schedule"]["search_start_hour"]
    interval = cfg["schedule"]["search_interval_hours"]
    return CronTrigger(hour=",".join(str(h) for h in range(start, 24, interval)),
                       minute=0, timezone=cfg["timezone"])


def setup_scheduler(cfg):
    worker = BackgroundScheduler(timezone=cfg["timezone"],
                                 executors={"default": {"type": "threadpool", "max_workers": 1}},
                                 job_defaults={"coalesce": True, "max_instances": 1})
    worker.add_job(run_cycle, search_trigger(cfg), id="search")
    worker.add_job(monitor_downloads, "interval", seconds=60, id="monitor",
                   next_run_time=datetime.now(pytz.utc))
    worker.start()
    return worker


def reschedule(worker):
    worker.reschedule_job("search", trigger=search_trigger(load_config()))


def main():
    global scheduler
    from waitress import create_server
    os.umask(0o027)
    initialize()
    scheduler = setup_scheduler(load_config())
    app = create_app(load_config, save_config, load_state,
                     os.path.join(LOG_DIR, "nytarr.log"), trigger_manual_search,
                     lambda: reschedule(scheduler))
    app.config['HEALTH_CHECK'] = lambda: scheduler.running and not stop_event.is_set()
    server = create_server(app, host="0.0.0.0", port=int(os.environ.get("PORT", "6789")),
                           threads=4, clear_untrusted_proxy_headers=True)

    def shutdown(signum, frame):
        stop_event.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.run()
    finally:
        stop_event.set()
        scheduler.shutdown(wait=True)
        server.close()


if __name__ == "__main__":
    main()
