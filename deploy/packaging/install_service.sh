#!/bin/bash
# Install and start the systemd unit. Run on the board as root, after
# bootstrap.sh:
#
#   sudo bash deploy/packaging/install_service.sh
set -euo pipefail

DEPLOY="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$DEPLOY/.." && pwd)"
SRC="$DEPLOY/packaging/fc-tpcm-relay.service"
DST=/etc/systemd/system/fc-tpcm-relay.service

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo bash $0" >&2
  exit 1
fi

if [[ ! -f "$SRC" ]]; then
  echo "missing: $SRC" >&2
  exit 1
fi

# The unit hardcodes this path in WorkingDirectory, TPCM_CONFIG and
# --state-basedir, so installing it from anywhere else starts a service that
# reads files this clone does not own.
if [[ "$REPO" != "/home/debian/fc-tpcm-relay" ]]; then
  echo "clone is $REPO but the unit expects /home/debian/fc-tpcm-relay." >&2
  echo "Move the clone, or edit the paths in $SRC first." >&2
  exit 1
fi

if [[ ! -x /home/debian/fc-tpcm-venv/bin/mavproxy.py ]]; then
  echo "missing /home/debian/fc-tpcm-venv/bin/mavproxy.py" >&2
  echo "run this first, as the service user: bash $DEPLOY/packaging/bootstrap.sh" >&2
  exit 1
fi

if [[ ! -f "$DEPLOY/config/tpcm.yaml" ]]; then
  echo "missing config: $DEPLOY/config/tpcm.yaml" >&2
  exit 1
fi

# MAVProxy does not create the --state-basedir parent, and bootstrap.sh runs as
# the service user, so this only matters when the unit is reinstalled after the
# directory was cleaned out.
install -d -o debian -g dialout -m 755 "$DEPLOY/state"

install -m 644 "$SRC" "$DST"
systemctl daemon-reload
systemctl enable fc-tpcm-relay.service
systemctl restart fc-tpcm-relay.service
systemctl --no-pager --full status fc-tpcm-relay.service
