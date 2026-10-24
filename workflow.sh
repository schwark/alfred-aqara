#!/bin/bash
# Alfred does not load interactive shell profiles. Find a supported Python explicitly.
set -euo pipefail
for python in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -x "$python" ]] && "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        exec "$python" "$@"
    fi
done
if [[ "${1:-}" == "filter.py" ]]; then
    echo '{"items":[{"title":"Python 3.9+ required","subtitle":"Install Python from python.org or Homebrew, then try again","valid":false}]}'
else
    echo 'Aqara requires Python 3.9+. Install Python and try again.'
fi
