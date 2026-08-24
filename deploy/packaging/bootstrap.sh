#!/bin/bash
# Install the relay into a venv on a board that has no MAVProxy at all, and the
# update path afterwards. Run from the clone:
#
#   bash deploy/packaging/bootstrap.sh
#
# Not as root: a root-created venv is not writable by the service user.
#
# Re-running is the whole update procedure. Module code, config, unit file and
# runtime deps all come from this clone, so `git pull` then this script is the
# only sequence there is.
set -euo pipefail

DEPLOY="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$DEPLOY/.." && pwd)"
VENV="${VENV:-/home/debian/fc-tpcm-venv}"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "do not run as root: $VENV would end up root-owned and the service" >&2
  echo "user could not write to it" >&2
  exit 1
fi

for cmd in python3 git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "missing $cmd: sudo apt install -y python3 git" >&2
    exit 1
  fi
done

# ensurepip, not venv: venv is stdlib and imports even when python3-venv is not
# installed, so testing for venv passes and `python3 -m venv` then fails several
# lines later with a message about a "failing command" instead.
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  echo "python3 cannot create a venv with pip in it." >&2
  echo "sudo apt install -y python3-venv" >&2
  exit 1
fi

# MAVProxy is installed from this clone, not from a git URL, so the clone has
# to be the whole fork and not just this directory.
if [[ ! -f "$REPO/setup.py" ]]; then
  echo "$REPO is not a MAVProxy clone: setup.py is missing." >&2
  echo "Clone the fc-tpcm-relay branch, do not copy deploy/ alone." >&2
  exit 1
fi

# packaging/fc-tpcm-relay.service names both paths literally, so a tree
# somewhere else installs fine and then starts the wrong thing.
if [[ "$REPO" != "/home/debian/fc-tpcm-relay" ]]; then
  echo "WARNING: clone is $REPO but the unit expects /home/debian/fc-tpcm-relay" >&2
fi
if [[ "$VENV" != "/home/debian/fc-tpcm-venv" ]]; then
  echo "WARNING: venv is $VENV but the unit expects /home/debian/fc-tpcm-venv" >&2
fi

[[ -d "$VENV" ]] || python3 -m venv "$VENV"
PY="$VENV/bin/python3"
"$PY" -m pip install --upgrade pip setuptools wheel

# MAVProxy's install_requires reaches well past this relay: numpy always, and
# on aarch64 also vtk, opencv-python and matplotlib for map3d.
# --default-modules=tpcm never loads those, and mavproxy.py imports only
# pymavlink and pyserial, so taking MAVProxy --no-deps keeps a small board from
# spending hours building numpy. requirements.txt is then the complete runtime
# set, and `pip check` reporting the unmet extras is expected, not a failure.
"$PY" -m pip install -r "$DEPLOY/requirements.txt"

# Installed from this clone rather than from git+URL@branch. A branch name in a
# pip URL means "whatever was on the branch when this board ran pip", which is
# not the same answer on two boards installed a week apart. Installing the
# clone makes the running code the commit that is checked out here, so
# `git rev-parse HEAD` identifies it.
#
# --force-reinstall is not optional on a re-run: the fork keeps version
# 1.8.74, so pip would otherwise call the requirement satisfied and do nothing.
"$PY" -m pip install --no-deps --force-reinstall "$REPO"

# --state-basedir in the unit; MAVProxy does not create the parent itself.
mkdir -p "$DEPLOY/state"

# Tracked in git because every board runs the same settings, so a missing file
# means the clone is broken rather than a first run needing the example copied.
if [[ ! -f "$DEPLOY/config/tpcm.yaml" ]]; then
  echo "missing $DEPLOY/config/tpcm.yaml -- it is tracked in git, so this" >&2
  echo "clone is incomplete. git checkout -- deploy/config/tpcm.yaml" >&2
  exit 1
fi

if ! id -nG "$(id -un)" | tr ' ' '\n' | grep -qx dialout; then
  echo "WARNING: $(id -un) is not in dialout, so the service cannot open the" >&2
  echo "serial port. sudo usermod -aG dialout $(id -un), then log out and in." >&2
fi

echo
echo "installed commit $(git -C "$REPO" rev-parse --short HEAD)"
"$PY" -c 'import MAVProxy.modules.mavproxy_tpcm as m; print("module:", m.__file__)'
"$PY" -c 'from MAVProxy.modules.mavproxy_tpcm.messages import MAVLink_tpcm_status_message as m; print("TPCM_STATUS id/crc_extra:", m.id, m.crc_extra)'
echo
echo "next, as root:"
echo "  sudo bash $DEPLOY/packaging/install_service.sh"
