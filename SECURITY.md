# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it by [opening a GitHub issue](../../issues/new) with the "security" label.

For critical vulnerabilities, please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

## Security Considerations

### Authentication
- **Always set `BRIDGE_ALLOWED_USERS`** — this restricts Telegram bot access to specific user IDs
- Without this setting, anyone who discovers your bot can send commands to Claude Code

### Network
- The server binds to `127.0.0.1` by default — only accessible from localhost
- If you change `BRIDGE_WEB_HOST` to `0.0.0.0`, ensure you have a reverse proxy with authentication
- The web terminal has no built-in authentication

### Claude Code
- `--dangerously-skip-permissions` is enabled by default for convenience
- Set `BRIDGE_CLAUDE_SKIP_PERMISSIONS=false` in `.env` if you want Claude to ask for confirmation

### Secrets
- Never commit `.env` files — they contain your Telegram bot token
- The `.gitignore` is pre-configured to exclude sensitive files
- Rotate your Telegram bot token if it's ever exposed
