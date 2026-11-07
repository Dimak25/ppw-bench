#!/bin/sh
# Сборка Linux-релиза PPW Bench:
#   dist/ppw-bench-vX.Y.Z-linux-x86_64.tar.gz  — скрипт + статический bench (нужен python3)
#   dist/ppw-bench-vX.Y.Z-linux-x86_64         — самостоятельный бинарник (Nuitka onefile)
#
#   ./build-linux.sh              # всё
#   ./build-linux.sh --no-nuitka  # только tar.gz
#
# Для Nuitka нужны: python3 -m pip install nuitka zstandard patchelf
set -eu
cd "$(dirname "$0")"

NUITKA=1
[ "${1:-}" = "--no-nuitka" ] && NUITKA=0

PY=${PYTHON:-python3}
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ppwbench/__init__.py)
[ -n "$VERSION" ] || { echo "Не удалось прочитать __version__" >&2; exit 1; }
ARCH=$(uname -m)
NAME=ppw-bench-v$VERSION-linux-$ARCH
echo "==> PPW Bench v$VERSION ($ARCH)"

mkdir -p dist
echo "==> bench (статически)"
CC=${CC:-gcc}
if ! "$CC" -O2 -static -pthread bench.c -o bench 2>/dev/null; then
    echo "   статическая сборка не удалась (нет glibc-static?), собираю динамически"
    "$CC" -O2 -pthread bench.c -o bench
fi
strip bench 2>/dev/null || true
./bench 1 1 >/dev/null

echo "==> tar.gz"
STAGE=build/stage/$NAME
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -r ppwbench "$STAGE/"
find "$STAGE/ppwbench" -name __pycache__ -prune -exec rm -rf {} +
cp ppw_bench.py bench bench.c README.md README.en.md LICENSE "$STAGE/"
cp packaging/linux/ppw-bench.sh "$STAGE/ppw-bench"
cp packaging/linux/install-udev.sh packaging/linux/99-ppw-bench-powercap.rules "$STAGE/"
chmod +x "$STAGE/ppw-bench" "$STAGE/install-udev.sh" "$STAGE/bench"
# bench новее bench.c, чтобы при наличии gcc его не пересобирали без нужды
touch "$STAGE/bench"
tar -C build/stage -czf "dist/$NAME.tar.gz" --owner=0 --group=0 "$NAME"
"$STAGE/ppw-bench" --version

ASSETS="dist/$NAME.tar.gz"
if [ "$NUITKA" = 1 ]; then
    echo "==> Nuitka onefile"
    "$PY" -m nuitka --onefile --assume-yes-for-downloads \
        --include-data-files=bench=bench \
        --nofollow-import-to=ppwbench.platforms.windows \
        --output-dir=build/nuitka --output-filename="$NAME" \
        --product-name="PPW Bench" --product-version="$VERSION" \
        ppw_bench.py
    cp "build/nuitka/$NAME" "dist/$NAME"
    "dist/$NAME" --version
    ASSETS="$ASSETS dist/$NAME"
fi

echo "==> SHA256"
(cd dist && for f in $ASSETS; do sha256sum "$(basename "$f")"; done) > dist/SHA256SUMS-linux.txt
cat dist/SHA256SUMS-linux.txt
ls -la $ASSETS
