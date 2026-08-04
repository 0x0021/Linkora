#!/bin/bash

PLIST_NAME="com.user.linkora.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "Uninstalling 灵桥 (Linkora) service..."

launchctl unload "$PLIST_PATH" 2>/dev/null
rm -f "$PLIST_PATH"

echo "Service uninstalled."
