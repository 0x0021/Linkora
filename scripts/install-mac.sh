#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.user.linkora.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"

PYTHON_PATH="$PROJECT_DIR/.venv/bin/python3"
MAIN_PATH="$PROJECT_DIR/main.py"

echo "Installing 灵桥 (Linkora) service..."
echo "Project: $PROJECT_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>$MAIN_PATH</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:$HOME/.local/bin</string>
        <key>LLM_API_KEY</key>
        <string></string>
    </dict>
</dict>
</plist>
EOF

launchctl load "$PLIST_PATH"
echo "Service installed and started."
echo "Logs: $PROJECT_DIR/logs/"
echo "To uninstall: run scripts/uninstall-mac.sh"
echo "To check status: launchctl list | grep linkora"
