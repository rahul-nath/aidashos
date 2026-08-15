#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv tool install --reinstall -e .

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/pi" <<SH
#!/usr/bin/env bash
exec "$ROOT/scripts/pi.sh" "\$@"
SH
chmod +x "$HOME/.local/bin/pi"

if [ "${1:-}" = "--append-zshrc" ]; then
  marker="# local-first-agent-os pi terminal hook"
  path_line='export PATH="$HOME/.local/bin:$PATH"'
  hook="source \"$ROOT/scripts/pi_terminal_hook.zsh\""
  touch "$HOME/.zshrc"
  if ! grep -Fqx "$marker" "$HOME/.zshrc"; then
    {
      printf '\n%s\n' "$marker"
      printf '%s\n' "$path_line"
      printf '%s\n' "$hook"
    } >> "$HOME/.zshrc"
  fi
fi

echo "Installed pi command at $HOME/.local/bin/pi. Try: pi /start /ocr"
