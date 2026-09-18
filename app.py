import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, flash, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me-in-render")

VIDEO_DIR = Path(os.environ.get("VIDEO_DIR", "videos"))
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
RTMP_URL = os.environ.get("KICK_RTMP_URL", "").strip()
STREAM_KEY = os.environ.get("KICK_STREAM_KEY", "").strip()
DEFAULT_VIDEO = os.environ.get("VIDEO_FILE", "")

process = None
process_lock = threading.Lock()
current_video = DEFAULT_VIDEO


def is_running():
    return process is not None and process.poll() is None


def stop_stream():
    global process
    with process_lock:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        process = None


def kick_output_url():
    raw_url = RTMP_URL
    raw_key = STREAM_KEY

    if raw_key.startswith(("rtmps://", "rtmp://", "https://", "http://")):
        parsed = urlparse(raw_key)
        raw_key = parsed.path.strip("/").split("/")[-1]
        raw_url = f"rtmps://{parsed.netloc}/app"

    if raw_url.startswith("https://"):
        raw_url = "rtmps://" + raw_url[len("https://"):]
    elif raw_url.startswith("http://"):
        raw_url = "rtmp://" + raw_url[len("http://"):]

    raw_url = raw_url.rstrip("/")

    if "/app" not in raw_url:
        raw_url += ":443/app"
    elif raw_url.endswith("/app") and ":443" not in raw_url:
        raw_url = raw_url[:-4].rstrip("/") + ":443/app"

    if not raw_url.startswith(("rtmps://", "rtmp://")):
        raise RuntimeError("KICK_RTMP_URL خاصو يبدا بـ rtmps://")
    if not raw_key or "/" in raw_key or not raw_key.startswith("sk_"):
        raise RuntimeError("KICK_STREAM_KEY خاصو يكون Stream Key الجديد بوحدو")

    return f"{raw_url}/{raw_key}"


def download_google_drive_video(url):
    output_file = VIDEO_DIR / f"drive-{uuid.uuid4().hex}.mp4"
    result = subprocess.run(
        ["gdown", "--fuzzy", url, "-O", str(output_file)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0 or not output_file.exists() or output_file.stat().st_size == 0:
        detail = result.stderr.strip() or result.stdout.strip() or "رابط Drive غير قابل للتحميل"
        raise RuntimeError(f"فشل تحميل Google Drive: {detail[-500:]}")
    return output_file


def start_stream(video_source):
    global process, current_video

    output_url = kick_output_url()
    source = video_source.strip()
    if not source:
        raise RuntimeError("خاصك تحط رابط الفيديو")

    is_drive = "drive.google.com" in source or "drive.usercontent.google.com" in source
    is_url = source.startswith(("http://", "https://"))

    if is_drive:
        input_source = str(download_google_drive_video(source))
        current_video = source
    elif is_url:
        input_source = source
        current_video = source
    else:
        video_path = Path(source)
        if not video_path.is_absolute():
            video_path = VIDEO_DIR / video_path
        if not video_path.exists():
            raise RuntimeError(f"الفيديو ما لقايناهش: {video_path}")
        input_source = str(video_path)
        current_video = video_path.name

    stop_stream()
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1", "-i", input_source,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", "4500k", "-maxrate", "4500k", "-bufsize", "9000k",
        "-g", "60", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "flv", output_url,
    ]
    with process_lock:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    time.sleep(4)
    if process.poll() is not None:
        error = process.stderr.read().decode("utf-8", errors="ignore")[-1200:]
        process = None
        raise RuntimeError(f"FFmpeg توقف: {error}")


def login_required():
    return session.get("logged_in") is True


@app.route("/", methods=["GET", "POST"])
def index():
    if not login_required():
        return redirect(url_for("login"))
    videos = sorted(p.name for p in VIDEO_DIR.iterdir() if p.is_file() and not p.name.startswith("drive-"))
    if request.method == "POST":
        try:
            action = request.form.get("action")
            if action == "start":
                source = request.form.get("video_url", "").strip() or request.form.get("video", "").strip()
                start_stream(source)
                flash("البث تخدم بنجاح", "ok")
            elif action == "stop":
                stop_stream()
                flash("البث توقف", "ok")
            else:
                flash("أمر غير معروف", "error")
        except Exception as error:
            flash(str(error), "error")
        return redirect(url_for("index"))
    return render_template("index.html", running=is_running(), video=current_video, videos=videos)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("كلمة السر غير صحيحة", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/health")
def health():
    return {"ok": True, "streaming": is_running(), "video": current_video}


def shutdown(*_):
    stop_stream()


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
