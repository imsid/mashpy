#!/bin/sh
set -e

REPO="imsid/mashpy"
INSTALL_DIR="${PILOT_INSTALL_DIR:-/usr/local/bin}"

detect_platform() {
    os="$(uname -s)"
    arch="$(uname -m)"

    case "$os" in
        Darwin) os="darwin" ;;
        Linux)  os="linux" ;;
        *)
            echo "Error: unsupported OS: $os" >&2
            exit 1
            ;;
    esac

    case "$arch" in
        arm64|aarch64) arch="arm64" ;;
        x86_64)        arch="x86_64" ;;
        *)
            echo "Error: unsupported architecture: $arch" >&2
            exit 1
            ;;
    esac

    echo "${os}-${arch}"
}

get_latest_release() {
    curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
        | grep '"tag_name"' \
        | head -1 \
        | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/'
}

main() {
    platform="$(detect_platform)"
    artifact="pilot-${platform}"

    echo "Detected platform: ${platform}"

    # linux-arm64 is not built yet
    if [ "$platform" = "linux-arm64" ]; then
        echo "Error: linux-arm64 binaries are not available yet." >&2
        exit 1
    fi

    echo "Fetching latest release..."
    tag="$(get_latest_release)"
    if [ -z "$tag" ]; then
        echo "Error: could not determine latest release." >&2
        exit 1
    fi
    echo "Latest release: ${tag}"

    url="https://github.com/${REPO}/releases/download/${tag}/${artifact}"
    echo "Downloading ${artifact}..."
    curl -fsSL -o pilot "$url"
    chmod +x pilot

    if [ -w "$INSTALL_DIR" ]; then
        mv pilot "${INSTALL_DIR}/pilot"
    else
        echo "Installing to ${INSTALL_DIR} (requires sudo)..."
        sudo mv pilot "${INSTALL_DIR}/pilot"
    fi

    echo "Installed pilot to ${INSTALL_DIR}/pilot"
    echo ""
    echo "Run: pilot repl"
}

main
