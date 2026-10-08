#!/bin/sh
set -eu

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required to select the Spotifyd release." >&2
    exit 1
fi

spotifyd_bin=$(command -v spotifyd || true)
if [ -z "$spotifyd_bin" ] && [ -x "$HOME/.local/bin/spotifyd" ]; then
    spotifyd_bin="$HOME/.local/bin/spotifyd"
fi

if [ -z "$spotifyd_bin" ]; then
    for tool in curl tar sha512sum; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            echo "Install $tool, then rerun ./setup_spotifyd.sh" >&2
            exit 1
        fi
    done

    case "$(uname -m)" in
        x86_64) release_arch=x86_64 ;;
        aarch64) release_arch=aarch64 ;;
        armv7l|armv7) release_arch=armv7 ;;
        *)
            echo "No automatic Spotifyd binary for $(uname -m). See https://docs.spotifyd.rs/installation/" >&2
            exit 1
            ;;
    esac

    archive_name="spotifyd-linux-${release_arch}-full.tar.gz"
    checksum_name="spotifyd-linux-${release_arch}-full.sha512"
    release_json=$(curl -fsSL https://api.github.com/repos/Spotifyd/spotifyd/releases/latest)
    asset_url() {
        printf '%s' "$release_json" | python3 -c '
import json, sys
name = sys.argv[1]
release = json.load(sys.stdin)
print(next((asset["browser_download_url"] for asset in release.get("assets", [])
           if asset.get("name") == name), ""))
' "$1"
    }
    archive_url=$(asset_url "$archive_name")
    checksum_url=$(asset_url "$checksum_name")
    if [ -z "$archive_url" ] || [ -z "$checksum_url" ]; then
        echo "Could not find the official Spotifyd release for $release_arch." >&2
        echo "Install it manually using https://docs.spotifyd.rs/installation/" >&2
        exit 1
    fi

    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT HUP INT TERM
    curl -fsSL "$archive_url" -o "$tmpdir/$archive_name"
    curl -fsSL "$checksum_url" -o "$tmpdir/$checksum_name"
    expected=$(awk 'NR == 1 { print $1 }' "$tmpdir/$checksum_name")
    actual=$(sha512sum "$tmpdir/$archive_name" | awk '{ print $1 }')
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "Spotifyd archive checksum verification failed." >&2
        exit 1
    fi

    tar -xzf "$tmpdir/$archive_name" -C "$tmpdir"
    binary=$(find "$tmpdir" -type f -name spotifyd -print -quit)
    if [ -z "$binary" ]; then
        echo "The Spotifyd release archive did not contain the expected binary." >&2
        exit 1
    fi
    mkdir -p "$HOME/.local/bin"
    install -m 755 "$binary" "$HOME/.local/bin/spotifyd"
    spotifyd_bin="$HOME/.local/bin/spotifyd"
    echo "Installed Spotifyd in ~/.local/bin/spotifyd"
fi

config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}/spotifyd
config_file="$config_dir/spotifyd.conf"
mkdir -p "$config_dir"
if [ ! -f "$config_file" ]; then
    cat > "$config_file" <<'CONFIG'
[global]
device_name = "Litefy"
backend = "pulseaudio"
bitrate = 320
CONFIG
    echo "Created $config_file using the PulseAudio-compatible audio output."
elif ! grep -Eq '^[[:space:]]*device_name[[:space:]]*=' "$config_file"; then
    printf '\ndevice_name = "Litefy"\n' >> "$config_file"
    echo "Added the Litefy device name to $config_file."
fi

if ! "$spotifyd_bin" --version >/dev/null 2>&1; then
    echo "Spotifyd was found but could not start. Check its audio backend and libraries." >&2
    exit 1
fi

cache_path=$(sed -nE 's/^[[:space:]]*cache_path[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$config_file" | head -n 1)
if [ -z "$cache_path" ]; then
    cache_path=${XDG_CACHE_HOME:-"$HOME/.cache"}/spotifyd
fi
if [ ! -f "$cache_path/oauth/credentials.json" ]; then
    echo "A browser will open so Spotifyd can sign in to your Spotify Premium account."
    "$spotifyd_bin" authenticate
else
    echo "Spotifyd already has saved login credentials."
fi

echo "Spotifyd is ready. Litefy will start and target this Connect device automatically."
