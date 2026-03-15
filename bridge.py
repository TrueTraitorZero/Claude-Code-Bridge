#!/usr/bin/env python3
# Run: python3 bridge.py
"""Claude Code Bridge — FastAPI server + Telegram bot + Web terminal."""
import asyncio
import json
import logging
import mimetypes
import os
import select
import subprocess
import sys
import tempfile
import time

from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request as FastAPIRequest
from fastapi.responses import FileResponse
from starlette.responses import JSONResponse
import uvicorn

# Add project dir to path
sys.path.insert(0, str(Path(__file__).parent))
import config
import tmux_manager
import project_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("bridge")

# Set proxy env vars from config (optional)
if config.PROXY:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.setdefault(var, config.PROXY)

# --- Project registry (loaded from projects.json) ---
project_config.load_projects()
_all_projects = project_config.get_projects()
current_project = _all_projects[0]["id"] if _all_projects else "default"
_switch_lock = asyncio.Lock()


def get_session() -> str:
    return tmux_manager.session_name_for_project(current_project)


def get_workdir() -> str:
    p = project_config.get_project(current_project)
    return p["workdir"] if p else os.path.expanduser("~")


def resolve_project(project_id: str = None):
    """Return (session, workdir) for a project, defaulting to current_project."""
    pid = project_id or current_project
    p = project_config.get_project(pid)
    if not p:
        p = project_config.get_project(current_project)
        pid = current_project
    session = tmux_manager.session_name_for_project(pid)
    return session, p["workdir"]


def safe_resolve_path(workdir: str, relative_path: str) -> Path:
    """Resolve a relative path within workdir, preventing path traversal."""
    base = Path(workdir).resolve()
    target = (base / relative_path).resolve()
    if target != base and not str(target).startswith(str(base) + "/"):
        raise ValueError("Path traversal detected")
    return target


app = FastAPI(title="Claude Code Bridge")


# --- Web Terminal ---

@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.post("/api/transcribe")
async def transcribe_audio(request: FastAPIRequest):
    """Receive audio blob, transcribe with Whisper, return text."""
    body = await request.body()
    logger.info(f"Transcribe: received {len(body)} bytes")
    if len(body) < 100:
        return JSONResponse({"text": "", "error": "Audio too short"})

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(body)
        webm_path = f.name

    wav_path = webm_path.replace(".webm", ".wav")
    try:
        # Convert to wav (async to avoid blocking event loop)
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            config.FFMPEG_BIN, "-y", "-i", webm_path,
            "-ar", str(config.AUDIO_SAMPLE_RATE), "-ac", "1", wav_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(ffmpeg_proc.wait(), timeout=config.FFMPEG_TIMEOUT)

        # Transcribe with whisperx (faster-whisper backend)
        # Try GPU first, fallback to CPU if OOM
        whisper_out = str(config.WHISPER_OUTPUT_DIR)
        text = ""
        devices = [("cuda", "float16"), ("cpu", "int8")] if config.WHISPER_DEVICE == "auto" else [(config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)]
        for device, compute in devices:
            whisper_proc = await asyncio.create_subprocess_exec(
                config.WHISPERX_BIN, wav_path,
                "--model", config.WHISPER_MODEL, "--language", config.WHISPER_LANGUAGE,
                "--device", device, "--compute_type", compute, "--no_align",
                "--output_format", "txt", "--output_dir", whisper_out,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await asyncio.wait_for(
                whisper_proc.communicate(), timeout=config.WHISPER_TIMEOUT
            )
            stderr_text = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

            txt_path = os.path.join(whisper_out, os.path.basename(wav_path).replace(".wav", ".txt"))
            if os.path.exists(txt_path):
                with open(txt_path) as f:
                    text = f.read().strip()
                os.unlink(txt_path)

            if text:
                logger.info(f"Transcribe [{device}]: '{text}'")
                break

            if whisper_proc.returncode != 0:
                stderr_short = stderr_text[-300:]
                logger.warning(f"Transcribe [{device}] failed (rc={whisper_proc.returncode}): {stderr_short}")
                if "out of memory" in stderr_text.lower() and device == "cuda":
                    logger.info("GPU OOM, falling back to CPU...")
                    continue
                break

        return JSONResponse({"text": text})
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return JSONResponse({"text": "", "error": str(e)})
    finally:
        for p in [webm_path, wav_path]:
            if os.path.exists(p):
                os.unlink(p)


@app.post("/api/refine-voice")
async def refine_voice(request: FastAPIRequest):
    """Send raw voice transcription to Claude CLI for cleanup."""
    body = await request.json()
    raw_text = body.get("text", "").strip()
    if not raw_text:
        return JSONResponse({"text": "", "error": "Empty text"})

    prompt = config.VOICE_CORRECTION_PROMPT + f"<transcription>\n{raw_text}\n</transcription>"

    try:
        env = os.environ.copy()
        for key in list(env.keys()):
            if "CLAUDE" in key.upper() and key not in ("CLAUDE_CONFIG_DIR",):
                del env[key]

        proc = await asyncio.create_subprocess_exec(
            config.CLAUDE_BIN, "-p", "--output-format", "text", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=config.CLAUDE_REFINE_TIMEOUT)
        result = stdout_data.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 or not result:
            logger.warning(f"refine-voice failed (rc={proc.returncode}): {stderr_data.decode()[-200:]}")
            return JSONResponse({"text": "", "error": "Claude CLI error"})

        logger.info(f"refine-voice: '{raw_text[:60]}...' -> '{result[:60]}...'")
        return JSONResponse({"text": result})
    except asyncio.TimeoutError:
        return JSONResponse({"text": "", "error": "Timeout"})
    except Exception as e:
        logger.error(f"refine-voice error: {e}")
        return JSONResponse({"text": "", "error": str(e)})


UPLOAD_DIR = config.UPLOADS_DIR


@app.post("/api/upload-image")
async def upload_image(request: FastAPIRequest):
    """Receive pasted image, save to temp dir, return file path."""
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    if len(body) < 100:
        return JSONResponse({"error": "Image too small"}, status_code=400)

    # Determine extension from content type
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
    ext = ext_map.get(content_type, ".png")

    filename = f"paste_{int(time.time()*1000)}{ext}"
    filepath = UPLOAD_DIR / filename
    filepath.write_bytes(body)
    logger.info(f"Image saved: {filepath} ({len(body)} bytes)")
    return JSONResponse({"path": str(filepath)})


# --- File Manager endpoints ---

@app.get("/api/files")
async def list_files(project: str = None, path: str = ""):
    """List files in the project working directory."""
    _, workdir = resolve_project(project)
    try:
        target = safe_resolve_path(workdir, path)
    except ValueError:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not target.is_dir():
        return JSONResponse({"error": "Not a directory"}, status_code=400)

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name == ".git":
                continue
            try:
                stat = entry.stat()
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size if not entry.is_dir() else 0,
                    "modified": int(stat.st_mtime),
                })
            except OSError:
                continue
    except PermissionError:
        return JSONResponse({"error": "Permission denied"}, status_code=403)

    return {"path": path, "items": items}


@app.get("/api/files/download")
async def download_file(project: str = None, path: str = ""):
    """Download a file from the project working directory."""
    _, workdir = resolve_project(project)
    try:
        target = safe_resolve_path(workdir, path)
    except ValueError:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not target.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)

    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(target, filename=target.name, media_type=media_type)


@app.post("/api/files/upload")
async def upload_files(project: str = None, path: str = "", files: list[UploadFile] = File(...)):
    """Upload files to the project working directory."""
    _, workdir = resolve_project(project)
    try:
        target = safe_resolve_path(workdir, path)
    except ValueError:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not target.is_dir():
        return JSONResponse({"error": "Not a directory"}, status_code=400)

    saved = []
    for f in files:
        safe_name = Path(f.filename).name
        if not safe_name:
            continue
        dest = target / safe_name
        content = await f.read()
        dest.write_bytes(content)
        saved.append(safe_name)
        logger.info(f"File uploaded: {dest} ({len(content)} bytes)")

    return {"uploaded": saved}


# --- Project endpoints ---

@app.get("/api/projects")
async def api_list_projects():
    projects = [
        {"id": p["id"], "name": p["name"], "color": p.get("color", "#a855f7"),
         "always_on": p.get("always_on", False), "workdir": p["workdir"]}
        for p in project_config.get_projects()
    ]
    return {"projects": projects, "current": current_project}


@app.post("/api/projects")
async def api_create_project(request: FastAPIRequest):
    body = await request.json()
    try:
        p = project_config.add_project(body)
        logger.info(f"Project created: {p['id']}")
        return {"ok": True, "project": p}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.put("/api/projects/{pid}")
async def api_update_project(pid: str, request: FastAPIRequest):
    body = await request.json()
    try:
        p = project_config.update_project(pid, body)
        logger.info(f"Project updated: {pid}")
        return {"ok": True, "project": p}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/projects/{pid}")
async def api_delete_project(pid: str):
    global current_project
    try:
        # Kill tmux session if exists
        session = tmux_manager.session_name_for_project(pid)
        if tmux_manager.session_exists(session):
            tmux_manager.kill_session(session)
        project_config.delete_project(pid)
        if current_project == pid:
            current_project = project_config.get_projects()[0]["id"]
        logger.info(f"Project deleted: {pid}")
        return {"ok": True, "current": current_project}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/projects/current")
async def switch_project_endpoint(request: FastAPIRequest):
    global current_project
    async with _switch_lock:
        body = await request.json()
        project_id = body.get("project")
        new_project = project_config.get_project(project_id)
        if not new_project:
            return JSONResponse({"error": f"Unknown project: {project_id}"}, status_code=400)

        # Kill old project's tmux sessions if it's on-demand
        old_project = project_config.get_project(current_project)
        if old_project and not old_project.get("always_on", False) and current_project != project_id:
            old_session = tmux_manager.session_name_for_project(current_project)
            if tmux_manager.session_exists(old_session):
                tmux_manager.kill_session(old_session)
                logger.info(f"Killed on-demand session '{old_session}'")

        current_project = project_id
        session = get_session()
        workdir = get_workdir()
        tmux_manager.ensure_session(session, workdir)
        logger.info(f"Switched to project '{project_id}' (session={session}, workdir={workdir})")
        return {"ok": True, "current": current_project}


@app.get("/api/backends")
async def list_backends():
    backends = [{"id": "claude", "name": "Claude", "icon": "\U0001f916", "sub": "Code assistant"}]
    if config.RALPH_AVAILABLE:
        backends.append({"id": "ralph", "name": "Ralph", "icon": "\U0001f3a9", "sub": "Orchestrator loop"})
    return backends


@app.get("/api/windows")
async def list_windows(project: str = None):
    session, workdir = resolve_project(project)
    tmux_manager.ensure_session(session, workdir)
    return tmux_manager.list_windows(session)


@app.post("/api/windows")
async def create_window(project: str = None, backend: str = "claude"):
    session, workdir = resolve_project(project)
    idx = tmux_manager.create_window(session, workdir, backend=backend)
    return {"index": idx, "backend": backend}


@app.delete("/api/windows/{idx}")
async def delete_window(idx: int, project: str = None):
    session, _ = resolve_project(project)
    tmux_manager.close_window(session, idx)
    return {"ok": True}


@app.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket):
    await ws.accept()

    # Get window index and project from query params
    window_idx = int(ws.query_params.get("window", "0"))
    project_id = ws.query_params.get("project", None)
    session, workdir = resolve_project(project_id)
    logger.info(f"Web terminal connected (session={session}, window {window_idx})")

    tmux_manager.ensure_session(session, workdir)

    # Validate window exists before creating PTY
    if not tmux_manager.window_exists(session, window_idx):
        logger.warning(f"Window {window_idx} does not exist in {session}, closing WebSocket")
        await ws.send_text(json.dumps({"type": "error", "error": "window_dead", "windowIdx": window_idx}))
        await ws.close(code=4404, reason=f"Window {window_idx} not found")
        return

    # Wait for the first resize message from the browser so we know the real terminal size
    init_cols, init_rows = config.TMUX_COLS, config.TMUX_ROWS
    try:
        first_msg = await asyncio.wait_for(ws.receive_text(), timeout=config.WS_INIT_TIMEOUT)
        msg = json.loads(first_msg)
        if isinstance(msg, dict) and msg.get("type") == "resize":
            init_cols = msg["cols"]
            init_rows = msg["rows"]
            logger.info(f"Window {window_idx}: initial size {init_cols}x{init_rows}")
    except Exception:
        pass

    master_fd, proc, link_name = tmux_manager.get_pty_fd_for_window(session, window_idx, cols=init_cols, rows=init_rows)

    async def read_pty():
        loop = asyncio.get_event_loop()
        while True:
            try:
                # Wait for data with select in executor to not block event loop
                ready = await loop.run_in_executor(None, lambda: select.select([master_fd], [], [], 0.1))
                if ready[0]:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except OSError:
                break
            except WebSocketDisconnect:
                break

    reader_task = asyncio.create_task(read_pty())

    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("text") or ""
            if not data and "bytes" in message:
                os.write(master_fd, message["bytes"])
                continue
            # Check for resize commands
            try:
                msg = json.loads(data)
                if isinstance(msg, dict) and msg.get("type") == "resize":
                    tmux_manager.resize_pty(master_fd, msg["cols"], msg["rows"], link_name=link_name)
                    continue
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            if data:
                os.write(master_fd, data.encode())
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.terminate()
        # Clean up linked session
        subprocess.run(f"tmux kill-session -t {link_name} 2>/dev/null", shell=True)
        logger.info(f"Web terminal disconnected (window {window_idx})")


# --- Telegram Bot ---

async def start_telegram_bot():
    if not config.TELEGRAM_TOKEN:
        logger.warning("BRIDGE_TELEGRAM_TOKEN not set — Telegram bot disabled")
        return

    token = config.TELEGRAM_TOKEN
    allowed = config.ALLOWED_USERS
    proxy = config.PROXY

    def get_projects():
        return project_config.get_projects_dict()

    def get_current_project():
        return current_project

    def switch_project(project_id):
        global current_project
        p = project_config.get_project(project_id)
        if p:
            # Kill old on-demand session
            old = project_config.get_project(current_project)
            if old and not old.get("always_on", False) and current_project != project_id:
                old_session = tmux_manager.session_name_for_project(current_project)
                if tmux_manager.session_exists(old_session):
                    tmux_manager.kill_session(old_session)
            current_project = project_id
            session = get_session()
            workdir = get_workdir()
            tmux_manager.ensure_session(session, workdir)
            logger.info(f"Telegram: switched to project '{project_id}'")

    from telegram_bot import setup_bot
    bot, dp = setup_bot(
        token, allowed, proxy,
        get_session=get_session, get_workdir=get_workdir,
        get_projects=get_projects, get_current_project=get_current_project,
        switch_project_fn=switch_project)

    logger.info("Starting Telegram bot...")
    await dp.start_polling(bot)


@app.on_event("startup")
async def startup():
    # Start tmux sessions only for always_on projects
    for p in project_config.get_projects():
        if p.get("always_on", False):
            session = tmux_manager.session_name_for_project(p["id"])
            tmux_manager.ensure_session(session, p["workdir"])
            logger.info(f"tmux session '{session}' started (always_on, project={p['id']})")
    # Also ensure current project has a session
    session = get_session()
    workdir = get_workdir()
    tmux_manager.ensure_session(session, workdir)
    logger.info(f"Current project: {current_project}")
    asyncio.create_task(start_telegram_bot())


if __name__ == "__main__":
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
