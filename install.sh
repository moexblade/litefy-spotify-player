#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required. Install Python 3 and its venv/pip support, then run this again." >&2
    exit 1
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
    (umask 077; cp .env.example .env)
    echo "Created .env from .env.example. Add your Spotify app credentials to .env."
else
    echo "Keeping your existing .env file."
fi
chmod 600 .env
echo "Restricted .env permissions to the current user."

echo "Setting up Spotifyd, Litefy's only playback device."
./setup_spotifyd.sh

bin_dir="$HOME/.local/bin"
launcher="$bin_dir/litefy"
mkdir -p "$bin_dir"

install_launcher=true
if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    if [ -L "$launcher" ] || ! grep -Fqx '# Managed by Litefy install.sh' "$launcher"; then
        echo "An existing $launcher was left untouched."
        install_launcher=false
    fi
fi

if [ "$install_launcher" = true ]; then
    python3 - "$APP_DIR" "$launcher" <<'PY'
import os
import shlex
import sys

app_dir, launcher = sys.argv[1:3]
target = os.path.join(app_dir, "litefy")
with open(launcher, "w", encoding="utf-8") as script:
    script.write("#!/bin/sh\n# Managed by Litefy install.sh\n")
    script.write(f"exec {shlex.quote(target)} \"$@\"\n")
PY
    chmod 755 "$launcher"
    echo "Installed the litefy command at $launcher."
fi

case ":$PATH:" in
    *":$bin_dir:"*) echo "Setup complete. Start Litefy with: litefy" ;;
    *)
        echo "Setup complete. Start Litefy with: $launcher"
        echo "$bin_dir is not in PATH. Add it to your shell configuration to use 'litefy' from any folder."
        ;;
esac
