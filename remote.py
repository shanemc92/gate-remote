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

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateremote")

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


@app.route("/toggle")
def toggle():
    toggle_gate()
    return redirect("/")


@app.route("/toggle-scheduler")
def toggle_scheduler():
    state["scheduler_enabled"] = not state["scheduler_enabled"]
    return redirect("/")


@app.route("/update_job/<job_id>", methods=["POST"])
def update_job(job_id):
    new_time = datetime.strptime(request.form["new_time"], "%H:%M").time()
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
    app.run(host="0.0.0.0", port=4000, debug=False)
