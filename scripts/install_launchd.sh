#!/bin/bash
set -euo pipefail

PLIST_DIR="$(cd "$(dirname "$0")/../launchd" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

LABELS=(
    "com.news.digest.0900"
    "com.news.digest.1300"
    "com.news.digest.1700"
    "com.news.digest.2100"
)

install() {
    echo "Installing News Digest launchd agents..."

    # Create LaunchAgents directory if it doesn't exist
    mkdir -p "$LAUNCH_AGENTS_DIR"

    # Copy and load each plist
    for label in "${LABELS[@]}"; do
        plist_file="${label}.plist"
        source_path="${PLIST_DIR}/${plist_file}"
        dest_path="${LAUNCH_AGENTS_DIR}/${plist_file}"

        if [[ ! -f "$source_path" ]]; then
            echo "ERROR: Source plist not found: $source_path"
            exit 1
        fi

        echo "  Installing $label..."
        cp "$source_path" "$dest_path"
        launchctl load "$dest_path"
        echo "    ✓ Loaded"
    done

    echo ""
    echo "Installation complete!"
    echo "Digest runs scheduled for: 09:00, 13:00, 17:00, 21:00 Athens time"
    echo ""
    echo "To check status: $0 status"
    echo "To uninstall: $0 uninstall"
}

uninstall() {
    echo "Uninstalling News Digest launchd agents..."

    for label in "${LABELS[@]}"; do
        plist_file="${label}.plist"
        dest_path="${LAUNCH_AGENTS_DIR}/${plist_file}"

        echo "  Uninstalling $label..."

        # Unload if loaded
        if launchctl list | grep -q "$label"; then
            launchctl unload "$dest_path" 2>/dev/null || true
            echo "    ✓ Unloaded"
        else
            echo "    (not loaded)"
        fi

        # Remove plist
        if [[ -f "$dest_path" ]]; then
            rm "$dest_path"
            echo "    ✓ Removed"
        else
            echo "    (not installed)"
        fi
    done

    echo ""
    echo "Uninstall complete!"
}

status() {
    echo "News Digest launchd agents status:"
    echo ""

    for label in "${LABELS[@]}"; do
        if launchctl list | grep -q "$label"; then
            echo "  ✓ $label - LOADED"
        else
            echo "  ✗ $label - not loaded"
        fi
    done

    echo ""
    echo "Recent launchd logs:"
    echo ""

    # Show last 5 lines from each log file
    for time in 0900 1300 1700 2100; do
        log_file="$PLIST_DIR/../data/launchd-${time}.log"
        if [[ -f "$log_file" ]]; then
            echo "  Last run ($time):"
            tail -1 "$log_file" 2>/dev/null | sed 's/^/    /' || echo "    (no logs yet)"
        fi
    done
}

# Main script
case "${1:-install}" in
    install)
        install
        ;;
    uninstall)
        uninstall
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {install|uninstall|status}"
        echo ""
        echo "  install   - Install and load launchd agents (default)"
        echo "  uninstall - Unload and remove launchd agents"
        echo "  status    - Show status of launchd agents"
        exit 1
        ;;
esac
