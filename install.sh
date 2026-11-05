#!/bin/bash
# Builds Equalizer and installs it for the current user, with nothing needing root.
#
# It works in three stages, so a problem leaves any existing install as it was:
#   1. Check that everything is ready, and stop with an explanation if it isn't.
#   2. Build the app and gather every file in a temporary folder.
#   3. Move the finished files into place.
set -euo pipefail
cd "$(dirname "$0")"

ID=io.github.zoeruda.Equalizer
PREFIX="$HOME/.local"
BOX=equalizer-dev
MIN_RUST=1.92
PLUGIN=lsp-plugins-ladspa.so
OS_RELEASE=/etc/os-release
LIBS=(/usr/lib64 /usr/lib /usr/lib/*-linux-gnu)
FLATPAK_URL=https://github.com/zoeruda/Equalizer/releases/latest

stage=""
moving=false

die() {
    printf '%s\n' "$@" >&2
    exit 1
}

finish() {
    local status=$?
    [ -z "$stage" ] || rm -rf "$stage"
    if [ "$status" -ne 0 ]; then
        echo >&2
        if "$moving"; then
            echo "The install was interrupted while moving files. Run this script again to finish it." >&2
        else
            echo "Equalizer wasn't installed, and any earlier install is unchanged." >&2
        fi
    fi
}
trap finish EXIT
# Without these, finish wouldn't know that Ctrl+C or a kill stopped the script.
trap 'exit 130' INT
trap 'exit 143' TERM

is_fedora_based() {
    # shellcheck source=/dev/null
    (. "$OS_RELEASE" && echo " ${ID:-} ${ID_LIKE:-} ") 2>/dev/null | grep -q ' fedora '
}

# Everything but Rust, which the toolbox gets from the home folder rather than dnf.
build_tools_ready() {
    in_box pkg-config --atleast-version=4.14 gtk4 &&
        in_box pkg-config --atleast-version=1.5 libadwaita-1 &&
        in_box pkg-config --exists libpipewire-0.3 &&
        in_box sh -c 'command -v clang && command -v gcc'
} >/dev/null 2>&1

# The same places the app looks, in find_plugin in src/engine.rs.
plugin_installed() {
    local dirs=() d
    IFS=: read -ra dirs <<<"${LADSPA_PATH:-}"
    dirs+=("$PREFIX/lib/ladspa" "$HOME/.ladspa" /usr/lib64/ladspa /usr/lib/ladspa /usr/local/lib/ladspa /usr/lib/x86_64-linux-gnu/ladspa)
    for d in "${dirs[@]}"; do
        if [ -e "$d/$PLUGIN" ]; then
            return 0
        fi
    done
    return 1
}

# Stage 1: check everything.

[ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo. It installs into your home folder."

for file in Cargo.toml Cargo.lock "data/$ID.desktop" "data/$ID.svg"; do
    [ -e "$file" ] || die "$file is missing. Run this script from a complete copy of the Equalizer source."
done

# rustup's installer only updates the PATH of new terminals.
if [ -f "$HOME/.cargo/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.cargo/env"
fi

# The app always runs on this system, even when it's built in a container.
[ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/pipewire-0" ] ||
    die "PipeWire isn't running in this session, and Equalizer needs it." \
        "If your system uses PipeWire, run this from a terminal in your desktop session."

module_found=false
ladspa_found=false
graph_found=false
for lib in "${LIBS[@]}"; do
    if [ -e "$lib/pipewire-0.3/libpipewire-module-filter-chain.so" ]; then
        module_found=true
    fi
    if [ -d "$lib/spa-0.2/filter-graph" ]; then
        graph_found=true
    fi
    if [ -e "$lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-ladspa.so" ]; then
        ladspa_found=true
    fi
done
"$module_found" || die "PipeWire's filter-chain module isn't installed, and Equalizer needs it." \
    "Install the package that provides libpipewire-module-filter-chain.so, then run this again."
if "$graph_found" && ! "$ladspa_found"; then
    die "PipeWire's LADSPA support isn't installed, and Equalizer needs it." \
        "Install the package that provides libspa-filter-graph-plugin-ladspa.so, then run this again."
fi

for dir in bin share/applications share/icons/hicolor/scalable/apps lib/ladspa; do
    mkdir -p "$PREFIX/$dir" 2>/dev/null && [ -w "$PREFIX/$dir" ] ||
        die "Can't write to $PREFIX/$dir. Check who owns it, for example if a past install ran with sudo."
done
stage=$(mktemp -d "$PREFIX/.equalizer-install.XXXXXX")

in_box() { "$@"; }
if ! build_tools_ready; then
    is_fedora_based || die "This script can only set up the build tools on Fedora-based systems." \
        "To use it here, first install:" \
        "  - Development packages for GTK 4.14 or newer, libadwaita 1.5 or newer, and PipeWire" \
        "  - clang, gcc, and pkg-config" \
        "  - Rust $MIN_RUST or newer, for example from https://rustup.rs" \
        "  - LSP Plugins (lsp-plugins-ladspa)" \
        "On an immutable system, use the Flatpak instead: $FLATPAK_URL"
    command -v toolbox >/dev/null ||
        die "This script builds in a toolbox container, but toolbox isn't installed." \
            "Install toolbox and run this again, or use the Flatpak: $FLATPAK_URL"
    # Checked here as well, to avoid downloading a container only to stop.
    command -v cargo >/dev/null || die "Rust isn't installed. Get version $MIN_RUST or newer, for example from https://rustup.rs"

    in_box() { toolbox run -c "$BOX" "$@"; }
    if ! toolbox list --containers | grep -qw "$BOX"; then
        toolbox create -y -c "$BOX"
    fi
    # Also repairs a container whose setup was interrupted on an earlier run.
    if ! build_tools_ready; then
        toolbox run -c "$BOX" sudo dnf install -y --setopt=install_weak_deps=False \
            gtk4-devel libadwaita-devel pipewire-devel clang clang-devel gcc pkgconf-pkg-config
    fi
    build_tools_ready || die "The build tools still aren't ready in the $BOX toolbox." \
        "It needs GTK 4.14 or newer and libadwaita 1.5 or newer, so your Fedora version may be too old."
fi

rust_version=$(in_box cargo --version 2>/dev/null | awk '{print $2}') || true
[ -n "$rust_version" ] || die "Rust isn't installed where the build runs. Get version $MIN_RUST or newer, for example from https://rustup.rs"
[ "$(printf '%s\n' "$MIN_RUST" "$rust_version" | sort -V | head -n 1)" = "$MIN_RUST" ] ||
    die "Equalizer needs Rust $MIN_RUST or newer, but this is $rust_version. With rustup, update it with: rustup update"

need_plugin=false
if ! plugin_installed; then
    in_box sh -c 'command -v dnf && command -v rpm2cpio && command -v cpio' >/dev/null 2>&1 ||
        die "LSP Plugins aren't installed, and this system doesn't have the tools to download them." \
            "Install your distribution's lsp-plugins-ladspa package, then run this again."
    need_plugin=true
fi

# Stage 2: build and gather. Nothing outside the temporary folder changes here.

in_box cargo build --release --locked --target-dir target

# Catches a toolbox build that this system's own libraries can't run.
if command -v ldd >/dev/null; then
    missing=$(ldd -r target/release/equalizer 2>&1 | grep -E 'not found|undefined symbol' || true)
    [ -z "$missing" ] || die "The app was built, but it can't run on this system:" "$missing" \
        "Your system's GTK, libadwaita, or PipeWire may be missing or too old. Try the Flatpak: $FLATPAK_URL"
fi

install -Dm755 target/release/equalizer "$stage/bin/equalizer"
# A full path, because the desktop's launcher may not have ~/.local/bin on its PATH.
sed "s|^Exec=.*|Exec=$PREFIX/bin/equalizer|" "data/$ID.desktop" | install -Dm644 /dev/stdin "$stage/share/applications/$ID.desktop"
install -Dm644 "data/$ID.svg" "$stage/share/icons/hicolor/scalable/apps/$ID.svg"

if "$need_plugin"; then
    mkdir -p "$stage/rpm"
    # shellcheck disable=SC2016 # Expanded by the inner shell.
    in_box bash -c 'set -euo pipefail; cd "$1"; dnf -q download lsp-plugins-ladspa; rpm2cpio lsp-plugins-ladspa-*.rpm | cpio -idm --quiet' _ "$stage/rpm" ||
        die "Couldn't download LSP Plugins. Check your connection, or install lsp-plugins-ladspa yourself, then run this again."
    downloaded=$(find "$stage/rpm" -name "$PLUGIN" | head -n 1)
    [ -n "$downloaded" ] || die "The LSP Plugins download didn't contain $PLUGIN."
    install -Dm755 "$downloaded" "$stage/lib/ladspa/$PLUGIN"
fi

# Stage 3: move everything into place. These are renames on the same disk, with the
# launcher last, so it never appears without the app it opens.

moving=true
if "$need_plugin"; then
    mv -f "$stage/lib/ladspa/$PLUGIN" "$PREFIX/lib/ladspa/$PLUGIN"
fi
mv -f "$stage/bin/equalizer" "$PREFIX/bin/equalizer"
mv -f "$stage/share/icons/hicolor/scalable/apps/$ID.svg" "$PREFIX/share/icons/hicolor/scalable/apps/$ID.svg"
mv -f "$stage/share/applications/$ID.desktop" "$PREFIX/share/applications/$ID.desktop"
moving=false

# Moved files keep the temporary folder's SELinux label, so give them the right ones.
if command -v restorecon >/dev/null; then
    restorecon "$PREFIX/bin/equalizer" "$PREFIX/lib/ladspa/$PLUGIN" \
        "$PREFIX/share/icons/hicolor/scalable/apps/$ID.svg" "$PREFIX/share/applications/$ID.desktop" >/dev/null 2>&1 || true
fi

update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true

echo "Installed. Open Equalizer from your app grid."
if command -v pgrep >/dev/null && pgrep -x equalizer >/dev/null; then
    echo "Equalizer is running now, so quit it from its menu and open it again to use this version."
fi
if command -v flatpak >/dev/null && flatpak info "$ID" >/dev/null 2>&1; then
    echo "The Flatpak version is also installed. Don't run both at the same time, as they create the same audio devices."
fi
