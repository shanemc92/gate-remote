#!/usr/bin/env python3
"""Gate remote — Flask + RPi.GPIO relay toggle with a daily on/off scheduler."""
import atexit
import logging
import os
import subprocess
import threading
import time
from datetime import datetime

import requests
import RPi.GPIO as GPIO
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, redirect
from flask_wtf import CSRFProtect

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateremote")

def _require_env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set - see .env.example")
    return v


# --- Config (set in .env, see .env.example) ---
WEBHOOK_URL = os.environ.get("GATE_WEBHOOK_URL", "")
WEBHOOK_HEADERS = {"ta": os.environ.get("GATE_WEBHOOK_TAG", "")}
WEBHOOK_PAYLOAD = "Gate Remote Toggled"
GATE_PIN = int(os.environ.get("GATE_PIN", 17))
TOGGLE_HOLD_SECONDS = float(os.environ.get("GATE_HOLD_SECONDS", 1.5))

# Camera snapshot — pulls one still frame from an RTSP stream via ffmpeg,
# cached for SNAPSHOT_INTERVAL seconds so the feed isn't hit on every page load.
RTSP_URL = os.environ.get("RTSP_URL", "")
SNAPSHOT_INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL", 10))

app = Flask(__name__)
# Signs the session cookie that carries the CSRF token. No default: a predictable
# key would let anyone mint a valid token, so refuse to start without one.
app.config["SECRET_KEY"] = _require_env("SECRET_KEY")

# The app sits behind an auth proxy, which authenticates the session but does not
# stop a cross-site request riding it. CSRFProtect covers every POST.
csrf = CSRFProtect()
csrf.init_app(app)

GPIO.setmode(GPIO.BCM)
GPIO.setup(GATE_PIN, GPIO.OUT, initial=GPIO.HIGH)  # relay off
atexit.register(GPIO.cleanup)

scheduler = BackgroundScheduler()
scheduler.start()

state = {"scheduler_enabled": False}
job_list = {"1": "22:30", "2": "07:30"}
_snapshot_cache = {"data": None, "ts": 0.0}
_snapshot_lock = threading.Lock()


def fire_webhook():
    if not WEBHOOK_URL:
        return
    try:
        resp = requests.post(WEBHOOK_URL, headers=WEBHOOK_HEADERS, data=WEBHOOK_PAYLOAD, timeout=3)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Webhook post failed: %s", e)


def toggle_gate():
    GPIO.output(GATE_PIN, GPIO.LOW)
    time.sleep(TOGGLE_HOLD_SECONDS)
    GPIO.output(GATE_PIN, GPIO.HIGH)
    fire_webhook()
    log.info("Gate toggled")


def capture_snapshot():
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", RTSP_URL,
             "-frames:v", "1", "-f", "image2", "-"],
            capture_output=True, timeout=8,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        log.warning("ffmpeg snapshot failed: %s", result.stderr.decode(errors="ignore")[-300:])
    except Exception as e:
        log.warning("Snapshot capture error: %s", e)
    return None


@app.route("/")
def main():
    return render_template(
        "main.html",
        pin_state=GPIO.input(GATE_PIN),
        scheduler_enabled=state["scheduler_enabled"],
        jobs=job_list,
        camera_enabled=bool(RTSP_URL),
        snapshot_interval=SNAPSHOT_INTERVAL,
    )


@app.route("/snapshot.jpg")
def snapshot():
    if not RTSP_URL:
        return "", 404
    with _snapshot_lock:
        now = time.time()
        if _snapshot_cache["data"] is None or now - _snapshot_cache["ts"] > SNAPSHOT_INTERVAL:
            data = capture_snapshot()
            if data:
                _snapshot_cache["data"] = data
                _snapshot_cache["ts"] = now
        data = _snapshot_cache["data"]
    if data is None:
        return "", 503
    return Response(data, mimetype="image/jpeg")


@app.route("/toggle", methods=["POST"])
def toggle():
    toggle_gate()
    return redirect("/")


@app.route("/toggle-scheduler", methods=["POST"])
def toggle_scheduler():
    state["scheduler_enabled"] = not state["scheduler_enabled"]
    return redirect("/")


@app.route("/update_job/<job_id>", methods=["POST"])
def update_job(job_id):
    if job_id not in job_list:
        return "Unknown job id", 400
    try:
        new_time = datetime.strptime(request.form.get("new_time", ""), "%H:%M").time()
    except ValueError:
        return "Bad time format, expected HH:MM", 400
    job_list[job_id] = f"{new_time.hour:02}:{new_time.minute:02}"
    scheduler.reschedule_job(job_id, trigger="cron", hour=new_time.hour, minute=new_time.minute)
    return redirect("/")


def scheduled_task():
    if state["scheduler_enabled"]:
        toggle_gate()


for job_id, default_time in job_list.items():
    t = datetime.strptime(default_time, "%H:%M").time()
    scheduler.add_job(scheduled_task, trigger="cron", hour=t.hour, minute=t.minute, id=job_id)


if __name__ == "__main__":
    # Loopback by default. Set BIND_HOST to expose the dev server; the gunicorn
    # bind in install.sh stays on 0.0.0.0, with ufw limiting it to the proxy IP.
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=4000, debug=False)
