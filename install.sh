#!/usr/bin/env bash
# PyStudio installer for Ubuntu.
# Run from the extracted PyStudio folder:   bash install.sh
# Re-run it any time to update to a newer PyStudio version (your settings are kept).
set -e

APP_NAME="PyStudio"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$HOME/.local/share/pystudio"
APP_DIR="$BASE/app"
VENV="$BASE/venv"
LAUNCHER="$HOME/.local/bin/pystudio"
DESKTOP_FILE="$HOME/.local/share/applications/pystudio.desktop"

echo "== Installing $APP_NAME =="

# --- System packages ---------------------------------------------------------
NEED=()
command -v python3 >/dev/null || NEED+=(python3)
python3 -c "import ensurepip" 2>/dev/null || NEED+=(python3-venv)
# Qt 6.5+ needs libxcb-cursor0 on X11 or the window won't open
dpkg -s libxcb-cursor0 >/dev/null 2>&1 || NEED+=(libxcb-cursor0)
if [ ${#NEED[@]} -gt 0 ]; then
    case " $* " in
        *" --update "*)
            echo "PyStudio needs these Ubuntu packages first: ${NEED[*]}"
            echo "Run  bash install.sh  in a terminal (it asks for your password once)."
            exit 3 ;;
    esac
    echo "Installing system packages: ${NEED[*]} (sudo password may be requested)"
    sudo apt-get update
    sudo apt-get install -y "${NEED[@]}"
fi

# --- Optional programs some features use (same list as Tools > Install Components) --
#   bash install.sh --all       install all of them without asking
#   bash install.sh --minimal   skip them (add them later from Tools > Install Components)
#   bash install.sh --update    what Help > Install Update runs: no questions, no desktop-icon copy
MODE="ask"
UPDATE=0
for arg in "$@"; do
    case "$arg" in
        --all) MODE="all" ;;
        --minimal) MODE="none" ;;
        --update) MODE="none"; UPDATE=1 ;;
    esac
done
OPTIONAL=(
    "git|git|Git - source control (the Git menu)"
    "gh|gh|GitHub CLI - publish to GitHub, build Windows .exe files on GitHub"
    "wine|wine wine64|Wine - build Windows .exe files on this computer (a large download)"
    "nsis|makensis|NSIS - make Setup.exe installers for Windows"
    "unixodbc|isql|unixODBC - ODBC database connections"
    "gdb|gdb|gdb - Debug > Attach to Process (debug a Python program that's already running)"
)
PICK=()
TTY_OK=0
(exec < /dev/tty) 2>/dev/null && TTY_OK=1      # a terminal to ask in (not when run by another program)
if [ "$MODE" != "none" ]; then
    ASKED=0
    for entry in "${OPTIONAL[@]}"; do
        IFS="|" read -r pkg cmds what <<< "$entry"
        have=0
        for c in $cmds; do command -v "$c" >/dev/null 2>&1 && have=1; done
        [ $have -eq 1 ] && continue
        if [ "$MODE" = "all" ]; then
            PICK+=("$pkg")
        elif [ $TTY_OK -eq 1 ]; then
            if [ $ASKED -eq 0 ]; then
                echo
                echo "Optional programs (Enter = yes). You can add them later: Tools > Install Components."
                ASKED=1
            fi
            answer=""
            read -r -p "  Install $what? [Y/n] " answer < /dev/tty || answer="n"
            case "$answer" in
                ""|[Yy]*) PICK+=("$pkg") ;;
            esac
        fi
    done
fi
if [ ${#PICK[@]} -gt 0 ]; then
    echo "Installing: ${PICK[*]} (sudo password may be requested)"
    sudo apt-get update
    # one failing package (say, gh on an older Ubuntu) shouldn't stop PyStudio's install
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${PICK[@]}" || \
        echo "Some optional programs didn't install - add them later from Tools > Install Components."
fi

# --- App files -------------------------------------------------------------------
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/pystudio" "$APP_DIR/resources"
cp -r "$SRC_DIR/pystudio" "$SRC_DIR/resources" "$SRC_DIR/main.py" "$APP_DIR/"
[ -f "$SRC_DIR/README.pdf" ] && cp "$SRC_DIR/README.pdf" "$APP_DIR/"     # Help > PyStudio Guide, F1
find "$APP_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +

# --- Python virtual environment with PySide6 -------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating Python environment..."
    python3 -m venv "$VENV"
fi
echo "Installing/updating PySide6, Jedi, pyflakes, Ruff and mypy (first time takes a minute)..."
"$VENV/bin/python" -m pip install --upgrade pip -q
"$VENV/bin/python" -m pip install --upgrade PySide6 jedi pyflakes black keyring pyte ruff mypy -q

# --- Launcher command --------------------------------------------------------------
mkdir -p "$(dirname "$LAUNCHER")"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/python" "$APP_DIR/main.py" "\$@"
EOF
chmod +x "$LAUNCHER"

# --- Menu entry + desktop icon -------------------------------------------------------
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Visual Studio-style Python IDE
Exec="$LAUNCHER" %F
Icon=$APP_DIR/resources/pystudio.svg
Terminal=false
Categories=Development;IDE;
StartupWMClass=PyStudio
MimeType=text/x-python;text/x-python3;
EOF
chmod +x "$DESKTOP_FILE"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ] && { [ $UPDATE -eq 0 ] || [ -e "$DESKTOP_DIR/pystudio.desktop" ]; }; then
    cp "$DESKTOP_FILE" "$DESKTOP_DIR/pystudio.desktop"
    chmod +x "$DESKTOP_DIR/pystudio.desktop"
    gio set "$DESKTOP_DIR/pystudio.desktop" metadata::trusted true 2>/dev/null || true
fi
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo
echo "Done. Launch $APP_NAME from the desktop icon, the app menu, or by typing: pystudio"
echo "(If the desktop icon shows a warning, right-click it and choose 'Allow Launching'.)"
