import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> str:
    frozen_base = getattr(sys, "_MEIPASS", None)
    base = Path(frozen_base) if is_frozen() and frozen_base else Path(__file__).resolve().parent
    return str(base / relative)


BASE_DIR = runtime_dir()
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGO_PATH = Path(resource_path("qt_logo.png"))
LOGO_ICO_PATH = Path(resource_path("qt_logo.ico"))

HOST = "127.0.0.1"
PORT = 5000
TOKEN_MASK = "********"
MAX_LOG = 300_000
MAX_JOBS = 25
WORKER_FLAG = "--qd-clone-worker"

app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)

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
    "clean_target",
)

DEFAULT_OPTIONS: dict = dict.fromkeys(BOOL_FIELDS, True)
DEFAULT_OPTIONS["server_banner"] = False
DEFAULT_OPTIONS["emojis"] = False
DEFAULT_OPTIONS["stickers"] = False
DEFAULT_OPTIONS["clean_target"] = True


def _default_options() -> dict:
    return dict(DEFAULT_OPTIONS)


DEFAULTS: dict = {
    "source_guild_id": "",
    "target_guild_id": "",
    "token_type": "bot",
    "options": _default_options(),
}

# The token (bot or user) is deliberately never written to disk. It only ever
# lives in this process's memory for the lifetime of the dashboard.
SESSION_TOKEN = ""


def worker_payload(payload: dict) -> dict:
    options = payload.get("options")
    options = options if isinstance(options, dict) else {}
    return {
        "source_guild_id": str(payload.get("source_guild_id", "") or ""),
        "target_guild_id": str(payload.get("target_guild_id", "") or ""),
        "token_type": payload.get("token_type", "bot"),
        "dry_run": bool(payload.get("dry_run", False)),
        "options": {key: bool(options.get(key, DEFAULT_OPTIONS[key])) for key in BOOL_FIELDS},
    }


def write_worker_payload(payload: dict) -> Path:
    fd, name = tempfile.mkstemp(prefix="qd-clone-", suffix=".json", dir=str(BASE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(worker_payload(payload), handle)
    except OSError:
        remove_worker_payload(Path(name))
        raise
    return Path(name)


def remove_worker_payload(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


async def run_clone_worker(payload_path: str) -> None:
    path = Path(payload_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[X] invalid clone-worker payload: {exc}", flush=True)
        raise SystemExit(2) from None
    finally:
        remove_worker_payload(path)

    if not isinstance(data, dict):
        print("[X] invalid clone-worker payload", flush=True)
        raise SystemExit(2)
    token = str(os.environ.get("DISCORD_TOKEN", "") or "").strip()
    if not token:
        print("[X] clone-worker token missing from DISCORD_TOKEN", flush=True)
        raise SystemExit(1)
    try:
        source_id = int(str(data.get("source_guild_id", "")).strip())
        target_id = int(str(data.get("target_guild_id", "")).strip())
    except ValueError:
        print("[X] clone-worker guild IDs must be numeric", flush=True)
        raise SystemExit(2) from None

    options_data = data.get("options")
    options_data = options_data if isinstance(options_data, dict) else {}
    selected = {key: bool(options_data.get(key, DEFAULT_OPTIONS[key])) for key in BOOL_FIELDS}

    from cloner import CloneOptions
    from main import execute_clone

    token_type = str(data.get("token_type", "bot") or "bot").strip().lower()
    if token_type not in ("bot", "user"):
        token_type = "bot"

    await execute_clone(
        token,
        source_id,
        target_id,
        CloneOptions(**selected),
        dry_run=bool(data.get("dry_run", False)),
        allow_pick=False,
        require_confirmation=False,
        token_type=token_type,
    )


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
    for key, value in DEFAULT_OPTIONS.items():
        if key not in opts:
            opts[key] = value
            changed = True
    # One-time migration: versions before clean-target existed saved the key as
    # False once and the user never asked for it to be off. The clean-before-
    # clone behaviour is the intended default, so force it back on.
    if opts.get("clean_target") is False:
        opts["clean_target"] = True
        changed = True
    current["options"] = opts
    if "token" in current:
        current.pop("token")
        changed = True
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
    saved = read_config().get("options")
    saved = saved if isinstance(saved, dict) else {}
    options: dict = {}
    for key in BOOL_FIELDS:
        if key in nested:
            options[key] = _as_bool(nested, key)
        elif key in data:
            options[key] = _as_bool(data, key)
        else:
            options[key] = bool(saved.get(key, DEFAULT_OPTIONS[key]))
    return options


def normalize_payload(data: dict) -> dict:
    token = str(data.get("token", "") or "").strip()
    if token in ("", TOKEN_MASK):
        token = SESSION_TOKEN
    token_type = str(data.get("token_type", "") or "").strip().lower()
    if token_type not in ("bot", "user"):
        saved = read_config().get("token_type", "bot")
        token_type = str(saved).strip().lower() if saved else "bot"
    if token_type not in ("bot", "user"):
        token_type = "bot"
    return {
        "token": token.strip('"').strip("'"),
        "token_type": token_type,
        "source_guild_id": str(data.get("source_guild_id", "") or "").strip(),
        "target_guild_id": str(data.get("target_guild_id", "") or "").strip(),
        "options": normalize_options(data),
        "dry_run": _as_bool(data, "dry_run"),
    }


def validate(payload: dict) -> str | None:
    if not payload["token"]:
        token_type = payload.get("token_type", "bot")
        if token_type == "user":
            return "User token is required. Grab it from your Discord account (see the hint below the field)."
        return "Bot token is required. Copy it from the Developer Portal under Bot > Token."
    if payload["token"].lower() in ("your_discord_bot_token", "your_user_token", "token"):
        return "That is a placeholder, not a real token. Paste the actual token."
    for key, label in (("source_guild_id", "Source"), ("target_guild_id", "Target")):
        value = payload[key]
        if not value:
            return f"{label} guild ID is required."
        if not value.isdigit():
            return f"{label} guild ID must be numeric (digits only)."
    if payload["source_guild_id"] == payload["target_guild_id"]:
        return "Source and target are the same server. Pick a separate, empty target server."
    return None


def save_config(payload: dict) -> None:
    current = read_config()
    current.pop("token", None)
    current["source_guild_id"] = payload["source_guild_id"]
    current["target_guild_id"] = payload["target_guild_id"]
    current["token_type"] = payload.get("token_type", "bot")
    current["options"] = payload["options"]
    try:
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except OSError as exc:
        app.logger.warning("config save failed: %s", exc)


def build_command(payload: dict, worker_file: Path | None = None) -> list:
    if worker_file is not None:
        return [sys.executable, WORKER_FLAG, str(worker_file)]
    token_type = str(payload.get("token_type", "bot") or "bot").strip().lower()
    cmd = [
        sys.executable,
        str(BASE_DIR / "main.py"),
        "--source",
        payload["source_guild_id"],
        "--target",
        payload["target_guild_id"],
        "--token-type",
        token_type,
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
    if not options.get("clean_target"):
        cmd.append("--no-clean-target")
    if payload["dry_run"]:
        cmd.append("--dry-run")
    return cmd


def find_free_port(start: int) -> int:
    import socket

    for port in range(start, start + 50):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((HOST, port))
        except OSError:
            continue
        finally:
            sock.close()
        return port
    return start


def wait_for_server(url: str, timeout: float = 20.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


def stop_all_jobs() -> None:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        process = job.get("process")
        if process is not None and process.poll() is None:
            job["cancel_requested"] = True
            with contextlib.suppress(OSError):
                process.terminate()


def launch_desktop(url: str, port: int) -> None:
    try:
        import webview
    except ImportError:
        print("[!] desktop window unavailable (pywebview missing), opening browser", flush=True)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        print(f"[+] dashboard: {url}")
        app.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)
        return

    server = threading.Thread(
        target=lambda: app.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True),
        daemon=True,
    )
    server.start()
    if not wait_for_server(url):
        print("[X] local server did not start", flush=True)
        raise SystemExit(1)

    window_kwargs: dict = {
        "width": 1220,
        "height": 880,
        "min_size": (980, 720),
        "resizable": True,
    }

    window = webview.create_window(
        "Quantum Devs Discord Server Cloner",
        url,
        **window_kwargs,
    )
    if window is None:
        print("[X] could not create desktop window", flush=True)
        raise SystemExit(1)
    window.events.closed += stop_all_jobs
    webview.start()


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
    global SESSION_TOKEN
    SESSION_TOKEN = payload["token"]
    job_id = str(uuid.uuid4())
    log_path = LOG_DIR / f"{job_id}.log"
    job: dict = {
        "id": job_id,
        "status": "queued",
        "log": "",
        "exit_code": None,
        "started_at": time.time(),
        "process": None,
        "cancel_requested": False,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
        while len(JOBS) > MAX_JOBS:
            oldest = next(iter(JOBS))
            JOBS.pop(oldest, None)

    def run() -> None:
        env = dict(os.environ)
        env["DISCORD_TOKEN"] = payload["token"]
        # The clone worker prints guild/channel/role names that can contain any
        # Unicode; without this Windows uses the legacy cp1252 console codec
        # and the worker dies with UnicodeEncodeError mid-run.
        env["PYTHONIOENCODING"] = "utf-8"
        env["DISCORD_TOKEN_TYPE"] = payload.get("token_type", "bot")
        env["PYTHONUNBUFFERED"] = "1"
        worker_file = write_worker_payload(payload) if is_frozen() else None
        process = None
        try:
            job["status"] = "running"
            append_log(job, "[+] starting clone run...\n", log_path)
            popen_kwargs: dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "cwd": str(BASE_DIR),
                "env": env,
                "bufsize": 1,
            }
            if os.name == "nt":
                # Never flash a console window for the clone worker.
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(build_command(payload, worker_file), **popen_kwargs)
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
                append_log(job, "\n[+] cancelled by user\n", log_path)
            else:
                job["status"] = "completed" if code == 0 else "failed"
                append_log(job, f"\n[+] exit code: {code}\n", log_path)
            job["exit_code"] = code
        except Exception as exc:
            job["status"] = "failed"
            job["exit_code"] = 1
            append_log(job, f"\n[X] {exc}\n", log_path)
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            job["process"] = None
            if worker_file is not None:
                remove_worker_payload(worker_file)

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.get("/")
def index():
    ensure_config()
    return render_template("index.html")


@app.get("/logo.png")
def logo_png():
    if not LOGO_PATH.exists():
        return ("", 404)
    return send_file(LOGO_PATH, mimetype="image/png", max_age=0)


@app.get("/favicon.ico")
def favicon_ico():
    if not LOGO_ICO_PATH.exists():
        return ("", 404)
    return send_file(LOGO_ICO_PATH, mimetype="image/vnd.microsoft.icon", max_age=0)


@app.get("/api/config")
def api_config():
    return jsonify(
        {
            "has_token": bool(SESSION_TOKEN),
            "token": TOKEN_MASK if SESSION_TOKEN else "",
            "token_type": read_config().get("token_type", "bot"),
            "source_guild_id": read_config().get("source_guild_id", ""),
            "target_guild_id": read_config().get("target_guild_id", ""),
            "options": read_config().get("options", DEFAULTS["options"]),
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
        return jsonify({"error": "job not found (the dashboard was probably restarted)"}), 404
    return jsonify(_public_job(job_id))


@app.get("/api/jobs/<job_id>/stream")
def api_job_stream(job_id: str):
    """Incremental log feed: return only the bytes after ?offset=N so the UI
    can poll fast (every 0.5s) without re-downloading or re-rendering the
    whole log. This is what makes the output feel truly live."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found (the dashboard was probably restarted)"}), 404
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    with JOBS_LOCK:
        log = job["log"]
        status = job["status"]
        exit_code = job["exit_code"]
    if offset > len(log):
        offset = len(log)
    slice_text = log[offset:]
    return jsonify(
        {
            "log_slice": slice_text,
            "next_offset": offset + len(slice_text),
            "status": status,
            "exit_code": exit_code,
        }
    )


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
    if WORKER_FLAG in sys.argv:
        flag_index = sys.argv.index(WORKER_FLAG)
        if flag_index + 1 >= len(sys.argv):
            print(f"[X] usage: {sys.argv[0]} {WORKER_FLAG} <payload-json>", flush=True)
            raise SystemExit(2)
        try:
            asyncio.run(run_clone_worker(sys.argv[flag_index + 1]))
        except KeyboardInterrupt:
            print("\ninterrupted.", flush=True)
            raise SystemExit(130) from None
    else:
        ensure_config()
        port = find_free_port(PORT)
        url = f"http://{HOST}:{port}"
        if os.environ.get("CLONER_NO_BROWSER"):
            print(f"[+] dashboard: {url}")
            app.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)
        else:
            launch_desktop(url, port)
