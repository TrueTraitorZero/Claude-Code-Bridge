#!/usr/bin/env bash
# setup.sh — Installation and setup of Claude Code Bridge
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Claude Code Bridge — Setup ==="
echo ""

# --- Check system dependencies ---

check_cmd() {
    if command -v "$1" &>/dev/null; then
        echo "  + $1"
        return 0
    else
        echo "  - $1 — not found"
        return 1
    fi
}

echo "Required dependencies:"
MISSING=0
check_cmd python3 || MISSING=1
check_cmd tmux    || { echo "    -> sudo apt install tmux"; MISSING=1; }
check_cmd claude  || { echo "    -> https://docs.anthropic.com/en/docs/claude-code"; MISSING=1; }

echo ""
echo "Optional (for voice messages):"
check_cmd ffmpeg   || echo "    -> sudo apt install ffmpeg"
check_cmd whisperx || echo "    -> pip install whisperx"

if [[ $MISSING -eq 1 ]]; then
    echo ""
    echo "Install missing dependencies and run the script again."
    exit 1
fi

# --- Virtual environment ---

echo ""
VENV_DIR="$PROJECT_DIR/venv"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists."
fi

source "$VENV_DIR/bin/activate"

echo "Installing Python dependencies..."
pip install -q -r "$PROJECT_DIR/requirements.txt"

# --- Configuration ---

echo ""

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "Setting up configuration..."
    echo ""

    # Telegram Bot Token
    read -rp "  Telegram Bot Token (from @BotFather): " BOT_TOKEN
    while [[ -z "$BOT_TOKEN" ]]; do
        echo "  Token is required."
        read -rp "  Telegram Bot Token: " BOT_TOKEN
    done

    # Telegram User ID
    read -rp "  Your Telegram User ID: " USER_ID
    while [[ -z "$USER_ID" ]]; do
        echo "  User ID is required."
        read -rp "  Your Telegram User ID: " USER_ID
    done

    # Working directory
    read -rp "  Working directory [$PWD]: " WORKDIR
    WORKDIR="${WORKDIR:-$PWD}"

    # Web port
    read -rp "  Web terminal port [8088]: " WEB_PORT
    WEB_PORT="${WEB_PORT:-8088}"

    # Proxy
    read -rp "  HTTP proxy (leave empty to skip): " PROXY

    # Whisper language
    read -rp "  Whisper language [en]: " WHISPER_LANG
    WHISPER_LANG="${WHISPER_LANG:-en}"

    # Whisper model
    read -rp "  Whisper model (tiny/base/small/medium/large) [small]: " WHISPER_MODEL
    WHISPER_MODEL="${WHISPER_MODEL:-small}"

    # Write .env
    cat > "$PROJECT_DIR/.env" <<EOF
# Telegram
BRIDGE_TELEGRAM_TOKEN=$BOT_TOKEN
BRIDGE_ALLOWED_USERS=$USER_ID

# Server
BRIDGE_WEB_PORT=$WEB_PORT

# Claude Code
BRIDGE_CLAUDE_WORKDIR=$WORKDIR
EOF

    if [[ -n "$PROXY" ]]; then
        echo "BRIDGE_PROXY=$PROXY" >> "$PROJECT_DIR/.env"
    fi

    if [[ "$WHISPER_LANG" != "en" ]]; then
        echo "BRIDGE_WHISPER_LANGUAGE=$WHISPER_LANG" >> "$PROJECT_DIR/.env"
    fi

    if [[ "$WHISPER_MODEL" != "small" ]]; then
        echo "BRIDGE_WHISPER_MODEL=$WHISPER_MODEL" >> "$PROJECT_DIR/.env"
    fi

    echo ""
    echo "  Configuration saved to .env"
else
    echo ".env already exists."
fi

# --- Optional: Ralph Orchestrator ---

echo ""
if ! command -v ralph &>/dev/null; then
    if command -v npm &>/dev/null; then
        read -rp "Install Ralph Orchestrator for AI agent loops? (y/N): " INSTALL_RALPH
        if [[ "${INSTALL_RALPH,,}" == "y" ]]; then
            echo "Installing Ralph Orchestrator..."
            npm install -g @ralph-orchestrator/ralph-cli
            echo "  Ralph installed successfully."
        else
            echo "  Skipping Ralph installation."
        fi
    else
        echo "Ralph Orchestrator requires npm (not found). Skipping."
        echo "  To install later: npm install -g @ralph-orchestrator/ralph-cli"
    fi
else
    echo "Ralph Orchestrator: already installed."
fi

# --- Projects config ---

if [[ ! -f "$PROJECT_DIR/projects.json" ]]; then
    WORKDIR_VAL="${WORKDIR:-$PWD}"
    cat > "$PROJECT_DIR/projects.json" <<EOJSON
[
  {
    "id": "default",
    "name": "Default",
    "workdir": "$WORKDIR_VAL",
    "color": "#a855f7",
    "always_on": false
  }
]
EOJSON
    echo ""
    echo "Created projects.json (working directory: $WORKDIR_VAL)"
    echo "  Projects can be added via web interface after startup."
else
    echo "projects.json already exists."
fi

# --- Done ---

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Run: source venv/bin/activate && python bridge.py"
echo "  2. Open http://localhost:${WEB_PORT:-8088} in browser"
echo "  3. Send a message to your Telegram bot"
echo ""
echo "Or run in background: ./restart.sh"
