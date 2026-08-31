#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv tool install --reinstall -e .

mkdir -p "$HOME/.local/bin"
operator_token_file="$HOME/.local-agent/operator.token"
mkdir -p "$(dirname "$operator_token_file")"
if [ ! -s "$operator_token_file" ]; then
  umask 077
  UV_CACHE_DIR=/tmp/uv-cache uv run python -c \
    'import secrets, sys; print(secrets.token_urlsafe(48), file=open(sys.argv[1], "w", encoding="utf-8"))' \
    "$operator_token_file"
fi
chmod 600 "$operator_token_file"
cat > "$HOME/.local/bin/pi" <<SH
#!/usr/bin/env bash
export LOCAL_AGENT_OPERATOR_TOKEN_FILE="$operator_token_file"
export LOCAL_AGENT_OPERATOR_TOKEN="\$(<"$operator_token_file")"
exec "$ROOT/scripts/pi.sh" "\$@"
SH
chmod +x "$HOME/.local/bin/pi"

if [ "${1:-}" = "--append-zshrc" ]; then
  marker="# local-first-agent-os pi terminal hook"
  path_line='export PATH="$HOME/.local/bin:$PATH"'
  hook="source \"$ROOT/scripts/pi_terminal_hook.zsh\""
  token_path_line="export LOCAL_AGENT_OPERATOR_TOKEN_FILE=\"$operator_token_file\""
  token_line='export LOCAL_AGENT_OPERATOR_TOKEN="$(<"$LOCAL_AGENT_OPERATOR_TOKEN_FILE")"'
  touch "$HOME/.zshrc"
  if ! grep -Fqx "$marker" "$HOME/.zshrc"; then
    {
      printf '\n%s\n' "$marker"
      printf '%s\n' "$path_line"
      printf '%s\n' "$hook"
      printf '%s\n' "$token_path_line"
      printf '%s\n' "$token_line"
    } >> "$HOME/.zshrc"
  fi
fi

echo "Installed pi command at $HOME/.local/bin/pi. Try: pi /start /ocr"
