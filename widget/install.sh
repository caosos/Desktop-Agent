#!/usr/bin/env bash
# One-time widget setup for Michael's desktop login (the only step that must run inside his GNOME session).
# What it does: fetch the public repo into ~/Desktop-Agent, add an "Aria" launcher to the applications menu,
# and start the widget, which asks for the pairing code shown by the panel's "Pair widget" button.
# What it never does: touch the control plane, its token file, services, or any other user's files.
set -euo pipefail
REPO="https://github.com/caosos/Desktop-Agent.git"
DIR="$HOME/Desktop-Agent"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only origin main; else git clone -q "$REPO" "$DIR"; fi
python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" 2>/dev/null || { echo "GTK 4 for Python is missing (sudo apt install python3-gi gir1.2-gtk-4.0)"; exit 1; }
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/aria-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Aria
Comment=Desktop-Agent Mission Control assistant
Exec=sh -c 'cd "$DIR" && exec python3 -m widget.app'
Icon=utilities-terminal
Terminal=false
Categories=Utility;
EOF
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed. Launcher 'Aria' is in your applications menu. Starting the widget now."
cd "$DIR" && exec python3 -m widget.app
