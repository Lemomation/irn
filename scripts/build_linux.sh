#!/usr/bin/env bash
# Build script for Linux (equivalent to build_windows.ps1)
set -euo pipefail

APP_NAME="${APP_NAME:-intenserp-next-v2}"
PACKAGE_NAME="${PACKAGE_NAME:-intenserp-next-v2-linux-x64}"
PACKAGE_APP_DIR_NAME="${PACKAGE_APP_DIR_NAME:-intense-rp-next}"
PACKAGE_OPTIONAL_DIR_NAME="${PACKAGE_OPTIONAL_DIR_NAME:-optional}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENTRY_POINT="$REPO_ROOT/main.py"
UPDATER_ENTRY_POINT="$REPO_ROOT/updater/main.py"
ICON_PATH="$REPO_ROOT/ui/assets/brand/newlogo.ico"
VERSION_PATH="$REPO_ROOT/version.json"

for f in "$ENTRY_POINT" "$UPDATER_ENTRY_POINT" "$ICON_PATH" "$VERSION_PATH"; do
    if [[ ! -f "$f" ]]; then
        echo "Required file not found: $f" >&2
        exit 1
    fi
done

BUILD_DIR="$REPO_ROOT/build"
DIST_DIR="$REPO_ROOT/dist"
SPEC_PATH="$REPO_ROOT/$APP_NAME.spec"

# Cleanup previous build artifacts
for path in "$BUILD_DIR" "$DIST_DIR" "$SPEC_PATH"; do
    if [[ -e "$path" ]]; then
        rm -rf "$path"
    fi
done

echo "Building main application..."
python -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --noconsole \
    --name "$APP_NAME" \
    --add-data "version.json:." \
    --add-data ".github/state:.github/state" \
    --add-data "remote_control/assets:remote_control/assets" \
    --add-data "remote_control/templates:remote_control/templates" \
    --add-data "ui/assets:ui/assets" \
    --add-data "ui/fonts:ui/fonts" \
    --collect-all patchright \
    --collect-all playwright \
    --collect-all desktop_notifier \
    --collect-all h2 \
    --collect-all hpack \
    --collect-all hyperframe \
    "$ENTRY_POINT"

BUILT_APP_DIR="$DIST_DIR/$APP_NAME"
if [[ ! -d "$BUILT_APP_DIR" ]]; then
    echo "PyInstaller output folder not found: $BUILT_APP_DIR" >&2
    exit 1
fi

# Copy version.json to package root
cp -f "$VERSION_PATH" "$BUILT_APP_DIR/version.json"

echo "Building updater..."
UPDATER_WORK_DIR="$BUILD_DIR/updater-work"
UPDATER_SPEC_DIR="$BUILD_DIR/updater-spec"
mkdir -p "$UPDATER_WORK_DIR" "$UPDATER_SPEC_DIR"

python -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --noconsole \
    --name updater \
    --distpath "$DIST_DIR" \
    --workpath "$UPDATER_WORK_DIR" \
    --specpath "$UPDATER_SPEC_DIR" \
    "$UPDATER_ENTRY_POINT"

UPDATER_EXE="$DIST_DIR/updater"
if [[ ! -f "$UPDATER_EXE" ]]; then
    echo "Updater output not found: $UPDATER_EXE" >&2
    exit 1
fi

# Remove any logs/config from built app
for root in "$BUILT_APP_DIR" "$BUILT_APP_DIR/_internal"; do
    if [[ -d "$root" ]]; then
        for forbidden_dir in logs config_data; do
            p="$root/$forbidden_dir"
            if [[ -d "$p" ]]; then
                rm -rf "$p"
            fi
        done
        for forbidden_file in config_dir.txt .env; do
            p="$root/$forbidden_file"
            if [[ -f "$p" ]]; then
                rm -f "$p"
            fi
        done
    fi
done

echo "Creating release package..."
STAGING_DIR="$DIST_DIR/$PACKAGE_NAME"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

MAIN_STAGE="$STAGING_DIR/$PACKAGE_APP_DIR_NAME"
OPTIONAL_STAGE="$STAGING_DIR/$PACKAGE_OPTIONAL_DIR_NAME"
mkdir -p "$MAIN_STAGE" "$OPTIONAL_STAGE"

cp -r "$BUILT_APP_DIR"/. "$MAIN_STAGE/"
cp "$UPDATER_EXE" "$OPTIONAL_STAGE/updater"

# Create tar.gz archive
ARCHIVE_PATH="$DIST_DIR/$PACKAGE_NAME.tar.gz"
rm -f "$ARCHIVE_PATH"
tar -czvf "$ARCHIVE_PATH" -C "$STAGING_DIR" "$PACKAGE_APP_DIR_NAME" "$PACKAGE_OPTIONAL_DIR_NAME"

echo "Created release asset: $ARCHIVE_PATH"
