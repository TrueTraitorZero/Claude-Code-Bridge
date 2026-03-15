# Claude Bridge

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Bridge between [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and external interfaces: **Telegram bot** and **web terminal**.

Control Claude Code from Telegram (text + voice) or via browser with a full-featured terminal.

## Features

- **Telegram bot** — send text and voice messages, get streaming output via live message editing
- **Web terminal** — full xterm.js terminal connected to Claude Code via tmux
- **Multi-project** — switch between projects with separate working directories
- **Voice input** — speech recognition via WhisperX with automatic language detection
- **File manager** — upload/download files through the web interface
- **Image paste** — paste screenshots directly into the terminal via Ctrl+V
- **Ralph integration** — optional [Ralph Orchestrator](https://github.com/mikeyobrien/ralph-orchestrator) support for AI agent loops

## Prerequisites

**Required:**
- Python 3.10+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — installed and authorized
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

**Optional:**
- [ffmpeg](https://ffmpeg.org/) — for voice message conversion
- [WhisperX](https://github.com/m-bain/whisperX) — for speech recognition
- [Ralph Orchestrator](https://github.com/mikeyobrien/ralph-orchestrator) — for AI agent loops

## Quick Start

```bash
git clone https://github.com/youruser/claude-bridge.git
cd claude-bridge
./setup.sh
```

The setup script will:
1. Check dependencies
2. Create a virtual environment and install packages
3. Interactively generate your `.env` configuration
4. Optionally install Ralph Orchestrator
5. Create a default `projects.json`

Then start the server:

```bash
./restart.sh
```

## Configuration

All settings are managed via `.env` file. See [`.env.example`](.env.example) for the full list.

### Required Settings

| Variable | Description |
|----------|-------------|
| `BRIDGE_TELEGRAM_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `BRIDGE_ALLOWED_USERS` | Your Telegram user ID (comma-separated for multiple users) |
| `BRIDGE_CLAUDE_WORKDIR` | Default working directory for Claude Code |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `BRIDGE_WEB_PORT` | `8088` | Web terminal port |
| `BRIDGE_WEB_HOST` | `127.0.0.1` | Server bind address |
| `BRIDGE_PROXY` | — | HTTP proxy for Telegram API and Claude CLI |
| `BRIDGE_WHISPER_MODEL` | `small` | Whisper model size (tiny/base/small/medium/large) |
| `BRIDGE_WHISPER_LANGUAGE` | `en` | Speech recognition language (ISO 639-1) |
| `BRIDGE_WHISPER_DEVICE` | `auto` | Whisper device (auto/cuda/cpu) |
| `BRIDGE_CACHE_DIR` | `~/.cache/claude-bridge` | Cache directory for uploads and temp files |
| `BRIDGE_TMUX_COLS` | `200` | Terminal width |
| `BRIDGE_TMUX_ROWS` | `50` | Terminal height |
| `BRIDGE_MAX_EXECUTION_DURATION` | `900` | Max Claude execution time in seconds |
| `BRIDGE_CLAUDE_SKIP_PERMISSIONS` | `true` | Skip Claude Code permission prompts |

## Usage

After starting the server:

- **Web terminal**: Open `http://localhost:8088` in your browser
- **Telegram bot**: Send a message to your bot

### Telegram Commands

| Command | Action |
|---------|--------|
| `/project` | Switch between projects |
| `/stop` | Interrupt current Claude request |
| `/restart` | Restart Claude Code session |

### Web Interface

The web interface supports:
- Multiple terminal panels (split view)
- Project switching via dropdown
- Voice input with two modes: direct to terminal or edit before sending
- File manager for browsing, uploading, and downloading project files
- Image pasting via Ctrl+V
- Ralph Orchestrator backend (if installed)

## Architecture

```
bridge.py          — FastAPI server, REST API, WebSocket handler
telegram_bot.py    — Telegram bot with streaming output
tmux_manager.py    — tmux session and window management
project_config.py  — Project configuration (projects.json)
config.py          — Centralized configuration (.env)
web/index.html     — Web terminal interface (xterm.js)
```

## License

[MIT](LICENSE)
