#!/bin/sh
# PPW Bench launcher for the tar.gz release: needs python3 >= 3.9.
HERE=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)
PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Нужен python3 (>= 3.9): sudo apt install python3 / sudo dnf install python3" >&2
    echo "Или скачайте самостоятельный бинарник ppw-bench-*-linux-x86_64 из релиза." >&2
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo "Нужен python3 >= 3.9, найден: $("$PY" --version 2>&1)" >&2
    exit 1
fi
exec "$PY" "$HERE/ppw_bench.py" "$@"
