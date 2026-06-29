#!/usr/bin/env bash
# =========================================================================
#  Briefa - One-click setup (Mac/Linux)
#
#  Shared template across Lingora / Tella / Briefa. Only the CONFIG block
#  differs per project; core logic identical.
# =========================================================================
set -euo pipefail
cd "$(dirname "$0")"

# ─── CONFIG (per-project) ─────────────────────────────────────────────────
PROJECT_NAME="Briefa"
PROJECT_TAGLINE="Factual briefing video (URL/repo/text -> MP4 + citation)."
MIN_PYTHON_MAJ=3
MIN_PYTHON_MIN=11
REQUIRES_NODE=true
MIN_NODE=22
DEPS_INSTALL=(-r requirements.txt)
ENV_TEMPLATE=".env.example"
ENV_DEST=".env"
ENV_KEY_LINE="GEMINI_API_KEY"
RUN_HINT="./run.sh"
POST_HINTS=(
  "1. Open $ENV_DEST, paste your $ENV_KEY_LINE."
  "   - Free key: https://aistudio.google.com/apikey"
  "2. (Recommended) Paste CLOUDFLARE_ACCOUNTS for scene images."
  "   - Without it, layouts fall back to gradient backgrounds."
  "3. $RUN_HINT"
)

# ─── CORE (shared) ────────────────────────────────────────────────────────
BAR="$(printf '=%.0s' $(seq 1 73))"
echo
echo "$BAR"
echo "  $PROJECT_NAME - Setup"
echo "  $PROJECT_TAGLINE"
echo "$BAR"
echo

TOTAL=4
$REQUIRES_NODE && TOTAL=$((TOTAL + 1))
STEP=0
next_step() { STEP=$((STEP + 1)); echo "[$STEP/$TOTAL] $1"; }

HAS_ERROR=0

# --- Python --------------------------------------------------------------
next_step "Checking Python ${MIN_PYTHON_MAJ}.${MIN_PYTHON_MIN}+ ..."
PYTHON_EXE=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    VER=$("$cand" -c "import sys; print(f'{sys.version_info[0]} {sys.version_info[1]}')" 2>/dev/null || true)
    if [ -n "$VER" ]; then
      MAJ=$(echo "$VER" | awk '{print $1}')
      MIN=$(echo "$VER" | awk '{print $2}')
      if [ "$MAJ" -gt "$MIN_PYTHON_MAJ" ] || { [ "$MAJ" -eq "$MIN_PYTHON_MAJ" ] && [ "$MIN" -ge "$MIN_PYTHON_MIN" ]; }; then
        PYTHON_EXE="$cand"; break
      fi
    fi
  fi
done
if [ -z "$PYTHON_EXE" ]; then
  echo "    [X] Python ${MIN_PYTHON_MAJ}.${MIN_PYTHON_MIN}+ not found."
  echo "        macOS:   brew install python@${MIN_PYTHON_MAJ}.${MIN_PYTHON_MIN}"
  echo "        Ubuntu:  sudo apt install python${MIN_PYTHON_MAJ}.${MIN_PYTHON_MIN} python${MIN_PYTHON_MAJ}.${MIN_PYTHON_MIN}-venv"
  HAS_ERROR=1
else
  echo "    [OK] $($PYTHON_EXE --version 2>&1)"
fi

# --- Node ----------------------------------------------------------------
if $REQUIRES_NODE; then
  next_step "Checking Node.js ${MIN_NODE}+ ..."
  if ! command -v node >/dev/null 2>&1; then
    echo "    [X] Node.js ${MIN_NODE}+ not found."
    echo "        macOS:   brew install node"
    echo "        Ubuntu:  curl -fsSL https://deb.nodesource.com/setup_${MIN_NODE}.x | sudo bash - && sudo apt install nodejs"
    HAS_ERROR=1
  else
    NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]")
    if [ "$NODE_MAJOR" -lt "$MIN_NODE" ]; then
      echo "    [X] Node.js $(node --version) - need ${MIN_NODE}+."
      HAS_ERROR=1
    else
      echo "    [OK] Node.js $(node --version)"
    fi
  fi
fi

# --- ffmpeg --------------------------------------------------------------
next_step "Checking ffmpeg ..."
if command -v ffmpeg >/dev/null 2>&1; then
  echo "    [OK] ffmpeg on PATH"
else
  echo "    [X] ffmpeg not found."
  echo "        macOS:   brew install ffmpeg"
  echo "        Ubuntu:  sudo apt install ffmpeg"
  HAS_ERROR=1
fi

if [ "$HAS_ERROR" -eq 1 ]; then
  echo
  echo "$BAR"
  echo "  Missing tools above. Install them then re-run ./SETUP.sh"
  echo "$BAR"
  exit 1
fi

# --- venv + deps ---------------------------------------------------------
next_step "Creating venv + installing Python deps ..."
if [ ! -d .venv ]; then
  "$PYTHON_EXE" -m venv .venv
  echo "    [OK] Created .venv/"
else
  echo "    [OK] .venv/ exists"
fi
. .venv/bin/activate
python -m pip install --upgrade pip --quiet
python -m pip install "${DEPS_INSTALL[@]}"
echo "    [OK] Python deps installed"

# --- env -----------------------------------------------------------------
next_step "Initializing config file ..."
if [ ! -f "$ENV_DEST" ]; then
  if [ -f "$ENV_TEMPLATE" ]; then
    mkdir -p "$(dirname "$ENV_DEST")"
    cp "$ENV_TEMPLATE" "$ENV_DEST"
    echo "    [OK] Created $ENV_DEST from $ENV_TEMPLATE"
  else
    echo "    [!] $ENV_TEMPLATE missing — create $ENV_DEST manually."
  fi
else
  echo "    [OK] $ENV_DEST already exists"
fi

# --- Done ----------------------------------------------------------------
echo
echo "$BAR"
echo "  [OK] SETUP DONE."
echo
echo "  Next steps:"
for hint in "${POST_HINTS[@]}"; do
  echo "    $hint"
done
echo "$BAR"
echo
