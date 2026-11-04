#!/bin/bash
# Installs (or updates) Sales Coach on this Mac. Run in Terminal:
#   curl -fsSL https://rmmarketingmcc.github.io/sales-coach/install.sh | bash
# The same line again updates it. To remove it:
#   curl -fsSL https://rmmarketingmcc.github.io/sales-coach/install.sh | bash -s -- --uninstall
#
# Everything goes in your own user account (no admin password):
#   ~/Applications/Sales Coach.app             the window, the menu bar item and your calendar; the coach is inside it
#   ~/Applications/Sales Coach Recorder.app    records your calls (microphone + call audio) to ~/CallRecordings
#   ~/Library/Application Support/Sales Coach  settings, call history, the coach's own Python and packages
#   ~/.cache/huggingface                       the speech model, Whisper large-v3 (about 6 GB, downloaded once)
#   two background services (LaunchAgents): the recorder and the coach, both start at login
# For tests: SALES_COACH_ZIP=<a local zip>, SALES_COACH_NO_SERVICES=1 (no services, no permission questions).
set -euo pipefail

REPO="rmmarketingmcc/sales-coach"
ZIP="${SALES_COACH_ZIP:-https://github.com/$REPO/releases/latest/download/Sales-Coach.zip}"
APPS="$HOME/Applications"
APP="$APPS/Sales Coach.app"
REC="$APPS/Sales Coach Recorder.app"
ENGINE="$APP/Contents/Resources/engine"
DATA="$HOME/Library/Application Support/Sales Coach"
ROOT="$HOME/CallRecordings"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(dk.rmmarketing.salescoach.recorder dk.rmmarketing.salescoach.engine)
UID_NUM="$(id -u)"
NO_SERVICES="${SALES_COACH_NO_SERVICES:-}"
PY="$DATA/venv/bin/python"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# The script itself arrives through the pipe, so questions are read from the terminal. No terminal: the default.
ask() {
  local answer=""
  printf '%s ' "$1" >&2
  { read -r answer </dev/tty; } 2>/dev/null || true
  printf '%s' "${answer:-${2:-}}"
}
ask_secret() {
  local answer=""
  printf '%s ' "$1" >&2
  { read -rs answer </dev/tty; } 2>/dev/null || true
  printf '\n' >&2
  printf '%s' "$answer"
}

stop_services() {
  for label in "${LABELS[@]}"; do launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true; done
  pkill -f "Sales Coach.app/Contents/MacOS/SalesCoach" 2>/dev/null || true
  pkill -f "Sales Coach Recorder.app/Contents/MacOS/SalesCoachRecorder" 2>/dev/null || true
  sleep 1
}

agent() {   # agent <label> <log file> <program> [args...]: a LaunchAgent that starts at login and is kept running
  local label="$1" logfile="$2"
  shift 2
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    for arg in "$@"; do echo "    <string>$arg</string>"; done
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>$DATA</string>"
    echo "  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>"
    echo '  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>'
    echo '  <key>LimitLoadToSessionType</key><string>Aqua</string><key>ProcessType</key><string>Interactive</string>'
    echo "  <key>StandardOutPath</key><string>$logfile</string><key>StandardErrorPath</key><string>$logfile</string>"
    echo '</dict></plist>'
  } > "$AGENTS/$label.plist"
  # bootout returns before the old process is gone; bootstrap meanwhile fails with "Input/output error"
  for _ in 1 2 3 4 5 6 7 8; do
    launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist" 2>/dev/null && return 0
    sleep 1
  done
  launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1 || { echo "Kunne ikke starte $label"; return 1; }
}

uninstall() {
  stop_services
  for label in "${LABELS[@]}"; do rm -f "$AGENTS/$label.plist"; done
  rm -rf "$APP" "$REC" "$DATA"
  security delete-generic-password -s "Sales Coach" -a anthropic >/dev/null 2>&1 || true
  echo "Sales Coach er fjernet, med indstillinger og historik."
  echo "Dine optagelser og noter ligger stadig i ~/CallRecordings. Slet mappen, hvis de også skal væk."
  echo "Talemodellen (cirka 6 GB) ligger i ~/.cache/huggingface/hub: de to mapper med \"whisper-large-v3\" i navnet."
}

main() {
  if [[ "${1:-}" == "--uninstall" ]]; then uninstall; return; fi

  [[ "$(uname -m)" == "arm64" ]] || { echo "Sales Coach kræver en Mac med Apple Silicon (M1 eller nyere)."; exit 1; }
  local os
  os="$(sw_vers -productVersion)"
  [[ "$(printf '%s\n%s\n' 14.4 "$os" | sort -V | head -1)" == "14.4" ]] ||
    { echo "Sales Coach kræver macOS 14.4 eller nyere. Den her Mac har $os."; exit 1; }
  local hub="${HF_HOME:-$HOME/.cache/huggingface}/hub"
  if [[ ! -d "$hub/models--mlx-community--whisper-large-v3-mlx" ]]; then
    local free_gb=$(( $(df -k "$HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
    (( free_gb >= 9 )) || { echo "Sales Coach skal bruge cirka 9 GB fri plads første gang (talemodellen). Den her Mac har $free_gb GB."; exit 1; }
  fi
  local first=""
  [[ -f "$DATA/settings.json" ]] || first=1

  say "Henter Sales Coach..."
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "'"$tmp"'"' EXIT
  if [[ -f "$ZIP" ]]; then cp "$ZIP" "$tmp/sc.zip"; else curl -fsSL "$ZIP" -o "$tmp/sc.zip"; fi
  [[ -n "$NO_SERVICES" ]] || stop_services
  mkdir -p "$APPS" "$DATA" "$ROOT" "$AGENTS"
  rm -rf "$APP" "$REC"
  ditto -x -k "$tmp/sc.zip" "$APPS/"
  xattr -dr com.apple.quarantine "$APP" "$REC" 2>/dev/null || true
  echo "Version $(cat "$APP/Contents/Resources/VERSION")"

  say "Gør coachen klar (sin egen Python, i Sales Coach-mappen)..."
  export UV_PYTHON_INSTALL_DIR="$DATA/python" UV_CACHE_DIR="$DATA/.uv-cache"
  local uv="$DATA/bin/uv"
  if [[ ! -x "$uv" ]]; then
    curl -fsSL https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$DATA/bin" sh >/dev/null 2>&1
  fi
  "$uv" venv --quiet --allow-existing --python 3.12 --python-preference only-managed "$DATA/venv"
  "$uv" pip install --quiet --python "$PY" -r "$ENGINE/requirements.txt"
  "$uv" cache clean --quiet >/dev/null 2>&1 || true   # the venv holds what it needs; the cache would double the size on disk

  say "Henter talemodellen (Whisper large-v3, cirka 6 GB første gang)..."
  "$PY" - <<'PYEOF'
from huggingface_hub import snapshot_download
for repo in ("mlx-community/whisper-large-v3-mlx", "Systran/faster-whisper-large-v3"):
    snapshot_download(repo)
print("Talemodellen er klar.")
PYEOF

  if [[ -n "$first" ]]; then
    say "Et par spørgsmål, så coachen ved, hvem du er (Retur springer over)"
    local name company about lang
    name="$(ask "Dit navn:")"
    company="$(ask "Dit firma:")"
    about="$(ask "Hvad du sælger, i én linje:")"
    lang="$(ask "Sproget i dine møder, da (dansk) eller en (engelsk) [da]:" da)"
    [[ "$lang" == en* ]] && lang=en || lang=da
    "$PY" "$ENGINE/coach.py" settings --name "$name" --company "$company" --about "$about" --language "$lang" >/dev/null
  fi

  say "AI-delen (tjekliste, stikord og noter)"
  local claude=""
  for c in "$(command -v claude 2>/dev/null || true)" "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" \
           /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if [[ -n "$c" && -x "$c" ]]; then claude="$c"; break; fi
  done
  if [[ -n "$claude" ]]; then
    echo "Claude Code fundet: coachen kører på dit eget Claude-abonnement."
  elif security find-generic-password -s "Sales Coach" -a anthropic >/dev/null 2>&1; then
    echo "Din API-nøgle fra Anthropic ligger i nøgleringen, og coachen bruger den."
  else
    echo "Den skal bruge din egen Claude: Claude Code (claude.com/claude-code, på dit Claude-abonnement) eller en API-nøgle fra Anthropic."
    echo "Uden nogen af dem virker taletid, transskription og ur stadig."
    local key
    key="$(ask_secret "Sæt en API-nøgle ind nu, eller tryk Retur for at springe over:")"
    if [[ -n "$key" ]]; then
      "$PY" "$ENGINE/coach.py" settings --api-key "$key" >/dev/null && echo "Gemt i din nøglering."
    fi
  fi

  if [[ -n "$NO_SERVICES" ]]; then
    say "Installeret (uden baggrundstjenester: SALES_COACH_NO_SERVICES)."
    return
  fi

  say "Tilladelser (macOS spørger én gang)"
  echo "1. Sales Coach Recorder: Mikrofonen og lyden fra mødet (\"Skærm- og systemlydoptagelse\", kun lyd)."
  echo "   Tryk Tillad til begge. To korte lyde tester det, og intet bliver gemt."
  ( sleep 5; afplay /System/Library/Sounds/Glass.aiff; sleep 2; afplay /System/Library/Sounds/Glass.aiff ) >/dev/null 2>&1 &
  open -W "$REC" --args --test 15 || true
  local result
  result="$(grep "test done" "$ROOT/callrec.log" 2>/dev/null | tail -1 || true)"
  "$PY" - "$ROOT" <<'PYEOF'
import json, shutil, sys
from pathlib import Path
for meta in Path(sys.argv[1]).glob("*/meta.json"):   # the test recording: not a call
    try:
        if json.loads(meta.read_text()).get("status") == "test":
            shutil.rmtree(meta.parent)
    except (OSError, ValueError):
        pass
PYEOF
  if [[ "$result" =~ system\ ([0-9.]+)\ mic\ ([0-9.]+) ]]; then
    [[ "${BASH_REMATCH[1]}" != "0.000" ]] && echo "   Lyden fra mødet: virker." ||
      echo "   Lyden fra mødet var tavs. Tillad Sales Coach Recorder i Systemindstillinger > Anonymitet og sikkerhed > Skærm- og systemlydoptagelse."
    [[ "${BASH_REMATCH[2]}" != "0.000" ]] && echo "   Mikrofonen: virker." ||
      echo "   Mikrofonen var tavs. Tillad Sales Coach Recorder i Systemindstillinger > Anonymitet og sikkerhed > Mikrofon."
  fi
  echo "2. Sales Coach: din Kalender, så coachen ved, hvem du mødes med, og hvornår mødet slutter. Tryk Tillad."
  open -W "$APP" --args --export-calendar || true
  echo "3. Sales Coach: lov til at styre din browser, så den selv kan skrive i Google Meet-chatten, at du optager. Tryk OK."
  open -W "$APP" --args --ask-browser || true
  echo "   Bruger du Google Meet i Chrome, så vælg én gang Se > Udvikler > Tillad JavaScript via Apple Events."
  echo "   I Safari: Udvikler > Tillad JavaScript fra Apple Events."

  say "Starter Sales Coach..."
  agent dk.rmmarketing.salescoach.recorder "$ROOT/callrec.err.log" "$REC/Contents/MacOS/SalesCoachRecorder"
  agent dk.rmmarketing.salescoach.engine "$DATA/coach.log" "$PY" "$ENGINE/coach.py" serve
  sleep 3
  open "$APP"
  echo
  echo "Færdig. Sales Coach kører i baggrunden fra nu af: gå til et møde (Meet, Zoom, Teams, Slack...),"
  echo "så åbner coachen ved siden af det. \"Coach\" i menulinjen har kontakterne."
  echo "Optagelser og noter: ~/CallRecordings. Opdatering: kør den samme linje igen."
}

main "$@"
