#!/bin/bash
# Builds Equalizer and installs it for the current user, with nothing needing root.
set -euo pipefail
cd "$(dirname "$0")"
ID=io.github.zoeruda.Equalizer
PREFIX="$HOME/.local"
BOX=equalizer-dev

# On an immutable system the build tools live in a container, not on the host.
if pkg-config --exists gtk4 libadwaita-1 libpipewire-0.3 2>/dev/null; then
    in_box() { "$@"; }
else
    command -v toolbox >/dev/null || { echo "Install the gtk4, libadwaita and pipewire development packages first."; exit 1; }
    if ! toolbox list --containers | grep -q "$BOX"; then
        toolbox create -y -c "$BOX"
        toolbox run -c "$BOX" sudo dnf install -y --setopt=install_weak_deps=False \
            gtk4-devel libadwaita-devel pipewire-devel clang-devel gcc pkgconf-pkg-config
    fi
    in_box() { toolbox run -c "$BOX" "$@"; }
fi

in_box cargo build --release

install -Dm755 target/release/equalizer "$PREFIX/bin/equalizer"
# A full path, because the desktop's launcher may not have ~/.local/bin on its PATH.
sed "s|^Exec=.*|Exec=$PREFIX/bin/equalizer|" "data/$ID.desktop" | install -Dm644 /dev/stdin "$PREFIX/share/applications/$ID.desktop"
install -Dm644 "data/$ID.svg" "$PREFIX/share/icons/hicolor/scalable/apps/$ID.svg"

# The audio engine: LSP Plugins, unless the system already has them.
if [ -e "$PREFIX/lib/ladspa/lsp-plugins-ladspa.so" ] || [ -e /usr/lib64/ladspa/lsp-plugins-ladspa.so ] || [ -e /usr/lib/ladspa/lsp-plugins-ladspa.so ]; then
    echo "LSP Plugins are already installed."
else
    work=$(mktemp -d)
    in_box bash -c "cd '$work' && dnf -q download lsp-plugins-ladspa && rpm2cpio lsp-plugins-ladspa-*.rpm | cpio -idm --quiet"
    install -Dm755 "$work/usr/lib64/ladspa/lsp-plugins-ladspa.so" "$PREFIX/lib/ladspa/lsp-plugins-ladspa.so"
    rm -rf "$work"
fi

update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
echo "Installed. Open Equalizer from your app grid."
