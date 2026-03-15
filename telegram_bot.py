"""Telegram bot for Claude Code bridge — uses claude CLI directly (no tmux scraping)."""
import asyncio
import html
import json
import os
import re
import time
import tempfile
import subprocess
import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

import config
import tmux_manager

logger = logging.getLogger(__name__)

ALLOWED_USERS = set()

_get_session = None
_get_workdir = None
_get_projects = None
_get_current_project = None
_switch_project_fn = None

_current_proc = None  # Track running claude subprocess for /stop


def setup_bot(token: str, allowed_users: str, proxy: str = None,
              get_session=None, get_workdir=None,
              get_projects=None, get_current_project=None, switch_project_fn=None):
    """Create and configure the bot."""
    global ALLOWED_USERS, _get_session, _get_workdir
    global _get_projects, _get_current_project, _switch_project_fn
    ALLOWED_USERS = {int(uid.strip()) for uid in allowed_users.split(",") if uid.strip()}
    _get_session = get_session
    _get_workdir = get_workdir
    _get_projects = get_projects
    _get_current_project = get_current_project
    _switch_project_fn = switch_project_fn

    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=token, session=session)
    dp = Dispatcher()

    @dp.message(F.text.startswith("/stop"))
    async def handle_stop(message: types.Message):
        if message.from_user.id not in ALLOWED_USERS:
            return
        if _current_proc and _current_proc.returncode is None:
            _current_proc.terminate()
            await message.reply("\U0001f6d1 Interrupted Claude request")
        else:
            tmux_manager.send_interrupt(_get_session())
            await message.reply("\U0001f6d1 Sent Ctrl+C to Claude Code")

    @dp.message(F.text.startswith("/restart"))
    async def handle_restart(message: types.Message):
        if message.from_user.id not in ALLOWED_USERS:
            return
        tmux_manager.kill_session(_get_session())
        tmux_manager.ensure_session(_get_session(), _get_workdir())
        await message.reply("\U0001f504 Claude Code restarted")

    @dp.message(F.text.startswith("/project"))
    async def handle_project(message: types.Message):
        if message.from_user.id not in ALLOWED_USERS:
            return
        if not _get_projects:
            await message.reply("Project switching not configured")
            return
        projects = _get_projects()
        current = _get_current_project()
        buttons = []
        for pid, info in projects.items():
            mark = "\u2705 " if pid == current else ""
            buttons.append([InlineKeyboardButton(
                text=f"{mark}{info['name']}",
                callback_data=f"project:{pid}"
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.reply(f"Current: <b>{projects[current]['name']}</b>\nSelect project:",
                          reply_markup=kb, parse_mode=ParseMode.HTML)

    @dp.callback_query(F.data.startswith("project:"))
    async def handle_project_callback(callback: CallbackQuery):
        if callback.from_user.id not in ALLOWED_USERS:
            await callback.answer("Not authorized", show_alert=True)
            return
        project_id = callback.data.split(":", 1)[1]
        if _switch_project_fn:
            _switch_project_fn(project_id)
            projects = _get_projects()
            name = projects.get(project_id, {}).get("name", project_id)
            await callback.message.edit_text(f"\u2705 Switched to <b>{name}</b>", parse_mode=ParseMode.HTML)
        await callback.answer()

    @dp.message(F.text)
    async def handle_text(message: types.Message):
        if message.from_user.id not in ALLOWED_USERS:
            await message.reply("\u26d4 Not authorized")
            return
        if message.text.startswith("/"):
            return
        await process_input(bot, message, message.text)

    @dp.message(F.voice)
    async def handle_voice(message: types.Message):
        if message.from_user.id not in ALLOWED_USERS:
            await message.reply("\u26d4 Not authorized")
            return
        file = await bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            await bot.download_file(file.file_path, f)
            ogg_path = f.name

        wav_path = ogg_path.replace(".ogg", ".wav")
        subprocess.run([config.FFMPEG_BIN, "-y", "-i", ogg_path,
                        "-ar", str(config.AUDIO_SAMPLE_RATE), "-ac", "1", wav_path],
                       capture_output=True)

        try:
            whisper_out = str(config.WHISPER_OUTPUT_DIR)
            text = ""
            devices = [("cuda", "float16"), ("cpu", "int8")] if config.WHISPER_DEVICE == "auto" else [(config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)]
            for device, compute in devices:
                result = subprocess.run(
                    [config.WHISPERX_BIN, wav_path,
                     "--model", config.WHISPER_MODEL, "--language", config.WHISPER_LANGUAGE,
                     "--device", device, "--compute_type", compute, "--no_align",
                     "--output_format", "txt", "--output_dir", whisper_out],
                    capture_output=True, text=True, timeout=config.WHISPER_TIMEOUT,
                )
                txt_path = os.path.join(whisper_out, os.path.basename(wav_path).replace(".wav", ".txt"))
                if os.path.exists(txt_path):
                    with open(txt_path) as f:
                        text = f.read().strip()
                    os.unlink(txt_path)
                if text:
                    logger.info(f"Voice transcribed [{device}]: '{text}'")
                    break
                if result.returncode != 0:
                    stderr_short = result.stderr[-300:] if result.stderr else ""
                    logger.warning(f"Whisperx [{device}] failed (rc={result.returncode}): {stderr_short}")
                    if "out of memory" in result.stderr.lower() and device == "cuda":
                        logger.info("GPU OOM, falling back to CPU...")
                        continue
                    break

            if not text:
                await message.reply("\u274c Could not transcribe voice")
                return
        except Exception as e:
            await message.reply(f"\u274c Transcription error: {e}")
            return
        finally:
            for p in [ogg_path, wav_path]:
                if os.path.exists(p):
                    os.unlink(p)

        await message.reply(f"\U0001f3a4 {text}")
        await process_input(bot, message, text)

    return bot, dp


# ---------------------------------------------------------------------------
# Direct Claude CLI execution (replaces tmux scraping)
# ---------------------------------------------------------------------------

async def process_input(bot: Bot, message: types.Message, text: str):
    """Run `claude -p --output-format stream-json`, parse events, update Telegram live."""
    global _current_proc
    status_msg = None
    try:
        status_msg = await message.reply("\u23f3")
        workdir = _get_workdir()

        env = os.environ.copy()
        if config.PROXY:
            env["HTTPS_PROXY"] = config.PROXY
            env["HTTP_PROXY"] = config.PROXY
        for key in list(env.keys()):
            if "CLAUDE" in key.upper():
                del env[key]

        cmd = [config.CLAUDE_BIN, "-p", "--verbose", "--continue", "--output-format", "stream-json"]
        if config.CLAUDE_SKIP_PERMISSIONS:
            cmd.append("--dangerously-skip-permissions")
        cmd.append(text)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
        )
        _current_proc = proc
        logger.info(f"Claude CLI started (pid={proc.pid}): {text[:80]}...")

        text_parts = []       # Final text response pieces
        tool_log = []          # Tool call names for progress display
        last_update_time = 0.0
        last_edit_text = ""
        start_time = time.time()
        MAX_DURATION = config.MAX_EXECUTION_DURATION

        async for raw_line in proc.stdout:
            # Timeout guard
            if time.time() - start_time > MAX_DURATION:
                proc.terminate()
                logger.warning("Claude CLI terminated: 15 min timeout")
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        name = block.get("name", "tool")
                        tool_log.append(name)
                        logger.info(f"  tool: {name}")
            elif etype == "result":
                break

            # Build display: show tool progress while working, text when available
            now = time.time()
            if (now - last_update_time) >= config.UPDATE_INTERVAL:
                if text_parts:
                    display_text = "".join(text_parts)
                elif tool_log:
                    elapsed = int(now - start_time)
                    display_text = f"\u2699\ufe0f Working ({elapsed}s)...\n" + "\n".join(
                        f"\u2022 {t}" for t in tool_log[-8:])
                else:
                    continue

                display = truncate_for_telegram(format_for_telegram(display_text))
                if display and display != last_edit_text:
                    try:
                        await status_msg.edit_text(display, parse_mode=ParseMode.HTML)
                    except Exception:
                        try:
                            await status_msg.edit_text(re.sub(r'<[^>]+>', '', display))
                        except Exception:
                            pass
                    last_edit_text = display
                    last_update_time = now

        await proc.wait()
        _current_proc = None

        elapsed = int(time.time() - start_time)
        final_text = "".join(text_parts)
        logger.info(f"Claude CLI finished (rc={proc.returncode}, {elapsed}s, {len(final_text)} chars, {len(tool_log)} tools)")

        if final_text.strip():
            formatted = format_for_telegram(final_text)
            await send_long_message(bot, message.chat.id, formatted, status_msg)
        elif tool_log:
            msg = f"\u2705 Done ({elapsed}s, {len(tool_log)} tool calls)"
            await status_msg.edit_text(msg)
        else:
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=config.STDERR_TIMEOUT)
                stderr_text = stderr_data.decode()[:300] if stderr_data else ""
            except asyncio.TimeoutError:
                stderr_text = ""
            err_msg = "\u26a0\ufe0f No response from Claude"
            if stderr_text:
                err_msg += f"\n<code>{html.escape(stderr_text)}</code>"
            await _send_html_or_plain(bot, message.chat.id, err_msg, edit_msg=status_msg)

    except Exception as e:
        logger.error(f"process_input error: {e}", exc_info=True)
        if status_msg:
            try:
                await status_msg.edit_text(f"\u26a0\ufe0f Error: {str(e)[:200]}")
            except Exception:
                pass
    finally:
        _current_proc = None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_for_telegram(text: str) -> str:
    """Convert Claude Code output to Telegram HTML."""
    # Strip ANSI escape sequences
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b\][^\x07]*\x07', '', text)

    lines = text.split('\n')
    result = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            result.append('')
            continue
        if all(c in '\u2500\u2501' for c in stripped):
            continue
        if 'bypass permissions' in stripped.lower():
            continue
        if 'ctrl+o' in stripped.lower():
            continue

        escaped = html.escape(line)

        if re.match(r'\s*\u25cf', line):
            escaped = re.sub(r'\u25cf\s*', '', escaped)
            escaped = f'<b>~ {escaped.strip()}</b>'
        elif re.match(r'\s*\u23bf', line):
            escaped = re.sub(r'\u23bf\s*', '', escaped)
            escaped = f'  <i>{escaped.strip()}</i>'
        elif re.match(r'\s*\u273b', line):
            escaped = re.sub(r'\u273b\s*', '', escaped)
            escaped = f'<i>({escaped.strip()})</i>'

        result.append(escaped)

    output = '\n'.join(result).strip()
    output = re.sub(r'\n{3,}', '\n\n', output)
    return output


def truncate_for_telegram(text: str) -> str:
    if len(text) <= config.MAX_MESSAGE_LEN:
        return text
    return text[:config.MAX_MESSAGE_LEN - 20] + "\n\n... (truncated)"


# ---------------------------------------------------------------------------
# Message sending with HTML fallback
# ---------------------------------------------------------------------------

async def _send_html_or_plain(bot: Bot, chat_id: int, text: str, edit_msg: types.Message = None):
    try:
        if edit_msg:
            await edit_msg.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        plain = re.sub(r'<[^>]+>', '', text)
        try:
            if edit_msg:
                await edit_msg.edit_text(plain)
            else:
                await bot.send_message(chat_id, plain)
        except Exception:
            pass


async def send_long_message(bot: Bot, chat_id: int, text: str, first_msg: types.Message):
    if len(text) <= config.MAX_MESSAGE_LEN:
        await _send_html_or_plain(bot, chat_id, text, edit_msg=first_msg)
        return

    chunks = []
    while text:
        if len(text) <= config.MAX_MESSAGE_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, config.MAX_MESSAGE_LEN)
        if split_at < config.MAX_MESSAGE_LEN // 2:
            split_at = config.MAX_MESSAGE_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    await _send_html_or_plain(bot, chat_id, chunks[0], edit_msg=first_msg)
    for chunk in chunks[1:]:
        await _send_html_or_plain(bot, chat_id, chunk)
        await asyncio.sleep(0.5)


