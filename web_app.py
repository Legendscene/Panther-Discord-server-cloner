import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

HOST = "127.0.0.1"
PORT = 5000
TOKEN_MASK = "********"
MAX_LOG = 300_000
MAX_JOBS = 25

app = Flask(__name__, template_folder="templates", static_folder="static")

JOBS: dict = {}
JOBS_LOCK = threading.Lock()

BOOL_FIELDS = (
    "server_name",
    "server_icon",
    "server_banner",
    "roles",
    "categories",
    "channels",
    "settings",
    "permissions",
    "emojis",
    "stickers",
)

DEFAULTS = {
    "token": "",
    "source_guild_id": "",
    "target_guild_id": "",
    "options": {
        "server_name": True,
        "server_icon": True,
        "server_banner": False,
        "roles": True,
        "categories": True,
        "channels": True,
        "settings": True,
        "permissions": True,
        "emojis": False,
        "stickers": False,
    },
}


def ensure_config() -> None:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        return
    try:
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        current = {}
    if not isinstance(current, dict):
        current = {}
    changed = False
    for key, value in DEFAULTS.items():
        if key not in current:
            current[key] = value
            changed = True
    opts = current.get("options")
    if not isinstance(opts, dict):
        opts = {}
    for key, value in DEFAULTS["options"].items():
        if key not in opts:
            opts[key] = value
            changed = True
    current["options"] = opts
    if changed:
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")


def read_config() -> dict:
    ensure_config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(DEFAULTS)
    return data if isinstance(data, dict) else dict(DEFAULTS)


def _as_bool(data: dict, key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_options(data: dict) -> dict:
    nested = data.get("options")
    nested = nested if isinstance(nested, dict) else {}
    saved = read_config().get("options") or {}
    options = {}
    for key in BOOL_FIELDS:
        if key in nested:
            options[key] = _as_bool(nested, key)
        elif key in data:
            options[key] = _as_bool(data, key)
        else:
            options[key] = bool(saved.get(key, DEFAULTS["options"][key]))
    return options


def normalize_payload(data: dict) -> dict:
    token = str(data.get("token", "") or "").strip()
    if token == TOKEN_MASK:
        token = str(read_config().get("token", "") or "").strip()
    return {
        "token": token.strip('"').strip("'"),
        "source_guild_id": str(data.get("source_guild_id", "") or "").strip(),
        "target_guild_id": str(data.get("target_guild_id", "") or "").strip(),
        "options": normalize_options(data),
        "dry_run": _as_bool(data, "dry_run"),
    }


def validate(payload: dict) -> str | None:
    if not payload["token"]:
        return "Bot token missing. Developer Portal -> Bot -> Token se copy karke paste karo."
    if payload["token"].lower() in ("your_discord_bot_token", "token"):
        return "Bot token placeholder hai, asli token daalo."
    for key, label in (("source_guild_id", "source"), ("target_guild_id", "target")):
        value = payload[key]
        if not value:
            return f"{label} guild ID missing."
        if not value.isdigit():
            return f"{label} guild ID numeric honi chahiye (sirf digits)."
    if payload["source_guild_id"] == payload["target_guild_id"]:
        return "Source aur target same server hain. Alag (khali) target server chahiye."
    return None


def save_config(payload: dict) -> None:
    current = read_config()
    current["token"] = payload["token"]
    current["source_guild_id"] = payload["source_guild_id"]
    current["target_guild_id"] = payload["target_guild_id"]
    current["options"] = payload["options"]
    try:
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except OSError as exc:
        app.logger.warning("config save failed: %s", exc)


def build_command(payload: dict) -> list:
    cmd = [
        sys.executable,
        str(BASE_DIR / "main.py"),
        "--source",
        payload["source_guild_id"],
        "--target",
        payload["target_guild_id"],
        "--yes",
    ]
    options = payload["options"]
    if not options["server_name"]:
        cmd.append("--no-server-name")
    if not options["server_icon"]:
        cmd.append("--no-icon")
    if options["server_banner"]:
        cmd.append("--with-banner")
    if not options["roles"]:
        cmd.append("--skip-roles")
    if not options["categories"]:
        cmd.append("--skip-categories")
    if not options["channels"]:
        cmd.append("--skip-channels")
    if not options["settings"]:
        cmd.append("--skip-settings")
    if not options["permissions"]:
        cmd.append("--skip-permissions")
    if options["emojis"]:
        cmd.append("--with-emoji")
    if options["stickers"]:
        cmd.append("--with-stickers")
    if payload["dry_run"]:
        cmd.append("--dry-run")
    return cmd


def append_log(job: dict, line: str, log_path: Path) -> None:
    with JOBS_LOCK:
        job["log"] += line
        if len(job["log"]) > MAX_LOG:
            job["log"] = job["log"][-MAX_LOG:]
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def start_job(payload: dict) -> str:
    job_id = str(uuid.uuid4())
    log_path = LOG_DIR / f"{job_id}.log"
    job = {
        "id": job_id,
        "status": "queued",
        "log": "",
        "exit_code": None,
        "started_at": time.time(),
        "process": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
        while len(JOBS) > MAX_JOBS:
            oldest = next(iter(JOBS))
            JOBS.pop(oldest, None)

    def run() -> None:
        env = dict(os.environ)
        env["DISCORD_TOKEN"] = payload["token"]
        env["PYTHONUNBUFFERED"] = "1"
        process = None
        try:
            job["status"] = "running"
            append_log(job, "[i] starting clone run...\n", log_path)
            process = subprocess.Popen(
                build_command(payload),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(BASE_DIR),
                env=env,
                bufsize=1,
            )
            job["process"] = process
            if process.stdout is not None:
                with process.stdout:
                    for line in process.stdout:
                        append_log(job, line, log_path)
                        if job.get("cancel_requested") and process.poll() is None:
                            process.terminate()
            code = process.wait()
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                append_log(job, "\n[i] cancelled by user\n", log_path)
            else:
                job["status"] = "completed" if code == 0 else "failed"
                append_log(job, f"\n[i] exit code: {code}\n", log_path)
            job["exit_code"] = code
        except Exception as exc:
            job["status"] = "failed"
            job["exit_code"] = 1
            append_log(job, f"\n[X] {exc}\n", log_path)
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            job["process"] = None

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.get("/")
def index():
    ensure_config()
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    cfg = read_config()
    token = str(cfg.get("token", "") or "")
    return jsonify(
        {
            "has_token": bool(token),
            "token": TOKEN_MASK if token else "",
            "source_guild_id": cfg.get("source_guild_id", ""),
            "target_guild_id": cfg.get("target_guild_id", ""),
            "options": cfg.get("options", DEFAULTS["options"]),
        }
    )


@app.post("/api/run")
def api_run():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    payload = normalize_payload(data)
    error = validate(payload)
    if error:
        return jsonify({"error": error}), 400
    save_config(payload)
    job_id = start_job(payload)
    return jsonify({"job_id": job_id, "status": "running"})


def _public_job(job_id: str) -> dict:
    job = JOBS[job_id]
    return {
        "id": job_id,
        "status": job["status"],
        "exit_code": job["exit_code"],
        "log": job["log"],
        "started_at": job["started_at"],
    }


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found (server restart ho gaya hoga)"}), 404
    return jsonify(_public_job(job_id))


@app.post("/api/jobs/<job_id>/stop")
def api_stop(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    process = job.get("process")
    if process is not None and process.poll() is None:
        job["cancel_requested"] = True
        process.terminate()
        return jsonify({"status": "stopping"})
    return jsonify({"status": job["status"]})


@app.post("/api/test")
def api_test():
    return jsonify({"status": "ok", "message": "Discord Server Cloner dashboard is live."})


if __name__ == "__main__":
    ensure_config()
    url = f"http://{HOST}:{PORT}"
    if not os.environ.get("CLONER_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"[+] dashboard: {url}")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
