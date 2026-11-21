#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "${1:-}" == "--fixture" ]]; then
  python fixture.py rollback "$2"
else
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./launch_native.ps1 -Stop
  python install.py --uninstall "$@"
fi
