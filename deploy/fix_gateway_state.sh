#!/usr/bin/env bash
# Fix Gateway + IBC install state.
# Removes stale symlinks, fixes the .desktop shortcut, fixes gatewaystart.sh
# to call the real binary name. Idempotent — safe to re-run.
#
# Usage: bash ~/code/tradingbot/deploy/fix_gateway_state.sh

set -eo pipefail

echo "=== Gateway state fixer ==="

HOME_DIR="${HOME:-/home/kali}"

# 1. Find the Gateway install version
GW_DIR="$HOME_DIR/Jts/ibgateway"
if [ ! -d "$GW_DIR" ]; then
    echo "FAIL: $GW_DIR does not exist. Gateway not installed in the default location."
    echo "     Install Gateway first (see deploy/CURRENT_STATE.md)."
    exit 1
fi

VERSION="$(ls "$GW_DIR" 2>/dev/null | head -1)"
if [ -z "$VERSION" ]; then
    echo "FAIL: no version folder found under $GW_DIR"
    exit 1
fi
echo "Gateway version folder: $VERSION"
GW_PATH="$GW_DIR/$VERSION"

# 2. Detect the binary name (ibgateway or ibgateway1 or similar)
BINARY=""
for candidate in ibgateway ibgateway1 ibgateway2; do
    if [ -x "$GW_PATH/$candidate" ] && [ ! -L "$GW_PATH/$candidate" ]; then
        BINARY="$candidate"
        break
    fi
done
if [ -z "$BINARY" ]; then
    echo "FAIL: no executable binary found in $GW_PATH/"
    ls -la "$GW_PATH/"
    exit 1
fi
echo "Real binary: $BINARY"
REAL_BINARY_PATH="$GW_PATH/$BINARY"

# 3. Remove stale symlinks inside GW_PATH
for f in "$GW_PATH"/ibgateway "$GW_PATH"/ibgateway1; do
    if [ -L "$f" ] && [ "$(basename "$f")" != "$BINARY" ]; then
        echo "Removing stale symlink: $f"
        rm -f "$f"
    fi
done

# 4. Remove stale parent-level symlinks
for link in "$HOME_DIR/ibgateway" "$HOME_DIR/ibgateway1" "$GW_DIR/1037"; do
    if [ -L "$link" ]; then
        echo "Removing stale symlink: $link"
        rm -f "$link"
    fi
done

# 5. Fix the .desktop shortcut if present
DESKTOP_FILE="$GW_PATH/IB Gateway $VERSION.desktop"
# Version folder might be "1037" while .desktop says "10.37" — try both
for d in "$GW_PATH"/*.desktop; do
    [ -f "$d" ] || continue
    DESKTOP_FILE="$d"
    echo "Fixing desktop shortcut: $DESKTOP_FILE"
    sed -i "s|^Exec=.*|Exec=$REAL_BINARY_PATH|" "$DESKTOP_FILE"
    # Copy to user's application menu
    mkdir -p "$HOME_DIR/.local/share/applications"
    cp "$DESKTOP_FILE" "$HOME_DIR/.local/share/applications/"
done
update-desktop-database "$HOME_DIR/.local/share/applications/" 2>/dev/null || true

# 6. Fix gatewaystart.sh if IBC is installed
IBC_SCRIPT="$HOME_DIR/code/ibc/gatewaystart.sh"
if [ -f "$IBC_SCRIPT" ]; then
    echo "Fixing IBC gatewaystart.sh"
    # Update version
    sed -i "s|^TWS_MAJOR_VRSN=.*|TWS_MAJOR_VRSN=$VERSION|" "$IBC_SCRIPT"
    # Update binary name — replace /ibgateway calls with /<real binary>
    if [ "$BINARY" != "ibgateway" ]; then
        sed -i "s|/ibgateway\"|/$BINARY\"|g; s|/ibgateway |/$BINARY |g" "$IBC_SCRIPT"
    fi
    echo "  TWS_MAJOR_VRSN=$VERSION"
    echo "  binary=$BINARY"
else
    echo "Skipping IBC fix (no gatewaystart.sh at $IBC_SCRIPT)"
fi

# 7. Final verification
echo ""
echo "=== Final state ==="
echo "Binary:       $REAL_BINARY_PATH"
echo "Executable:   $([ -x "$REAL_BINARY_PATH" ] && echo YES || echo NO)"
echo "Is symlink:   $([ -L "$REAL_BINARY_PATH" ] && echo YES || echo NO)"
if [ -f "$IBC_SCRIPT" ]; then
    echo "IBC script:   $IBC_SCRIPT"
    grep -E '^(TWS_MAJOR_VRSN|TWS_PATH|IBC_PATH)=' "$IBC_SCRIPT" | sed 's/^/  /'
fi
echo ""
echo "DONE. To start Gateway:"
echo "  directly:  $REAL_BINARY_PATH &"
echo "  via IBC:   bash $IBC_SCRIPT"
