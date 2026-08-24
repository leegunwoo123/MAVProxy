# Deploying the FC/TPCM relay on a board

## What this branch is

`fc-tpcm-relay` is the only branch involved. It carries the module and this
`deploy/` directory together, so one clone is both the code and the settings,
and the code that runs is the commit that is checked out.
`git -C /home/debian/fc-tpcm-relay rev-parse --short HEAD` is the whole answer
to "what is this board running".

There used to be a second branch, so that the pull request to upstream would
not contain one installation's serial ports, systemd unit and `/home/debian`
paths. That pull request is closed and the module is staying on the fork, so
the two are merged. Nothing about the board changes if it is ever reopened:
strip `deploy/` onto a branch of its own at that point.

The `tpcm` module and its MAVLink definition (`tpcm.xml`) live in
`MAVProxy/modules/mavproxy_tpcm/` on this same branch. Installing MAVProxy
installs the module; there is no separate module install and no code
generation step, because the module defines `TPCM_STATUS` itself.

Nothing under `deploy/` is on the Python path at runtime. `bootstrap.sh`
installs the repository root into the venv, so editing a `.py` in the clone
does nothing until the next `bootstrap.sh` — and there are no `.py` files under
`deploy/` anyway.

Two paths are hardcoded in `packaging/fc-tpcm-relay.service` and enforced by
`packaging/install_service.sh`:

- clone: `/home/debian/fc-tpcm-relay`
- venv: `/home/debian/fc-tpcm-venv`

## First install

### 1. Board prerequisites (once per board)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
sudo usermod -aG dialout debian
```

Log out and back in so the `dialout` membership takes effect. Without it the
service starts and then cannot open the serial port.

### 2. Clone the branch

```bash
git clone --depth 1 --single-branch -b fc-tpcm-relay \
  https://github.com/leegunwoo123/MAVProxy.git /home/debian/fc-tpcm-relay
```

`--depth 1 --single-branch` is what keeps this small: the checkout is about
7 MB over ~420 files, against 68 MB for the full history.

Clone the whole repository, not just `deploy/`. `bootstrap.sh` installs the
repository root and refuses to run without `setup.py` above it.

### 3. Install

```bash
cd /home/debian/fc-tpcm-relay
bash deploy/packaging/bootstrap.sh
```

Not as root: a root-created venv is not writable by the service user. The
script creates the venv, installs `deploy/requirements.txt`, installs MAVProxy
from this clone with `--no-deps`, creates `deploy/state/`, and prints the
installed commit along with the module path and `TPCM_STATUS` id/crc_extra.

`--no-deps` is deliberate. MAVProxy's `install_requires` asks for `numpy`
everywhere and, on aarch64, also `vtk`, `opencv-python` and `matplotlib` for
the map3d module. `--default-modules=tpcm` never loads any of those, and
`mavproxy.py` itself imports only `pymavlink` and `pyserial`, so pulling them
in would mean a long build — or an out-of-memory numpy compile on a small
board — for code that never runs. `deploy/requirements.txt` is therefore the
complete runtime set. A consequence: `pip check` reports the unmet
requirements. That is expected, not a failure.

### 4. Start it

```bash
sudo bash deploy/packaging/install_service.sh
```

`config/tpcm.yaml` is tracked in git and every board runs the same file, so
there is nothing to edit here on a first install. If the settings need to
change, see "Changing the config" below.

### 5. Verify

```bash
systemctl --no-pager --full status fc-tpcm-relay
journalctl -u fc-tpcm-relay -n 80 --no-pager
ss -ltn | grep 6000
```

Port 6000 is opened by the module, so if it is not listening the module did not
load — check the journal for the config load failing.

Confirm the pieces resolve to what you expect:

```bash
V=/home/debian/fc-tpcm-venv
git -C /home/debian/fc-tpcm-relay rev-parse --short HEAD
$V/bin/python3 -c 'import MAVProxy.modules.mavproxy_tpcm as m; print(m.__file__)'
$V/bin/python3 -c 'from MAVProxy.modules.mavproxy_tpcm.messages import MAVLink_tpcm_status_message as m; print(m.id, m.crc_extra)'
```

That last line should print `5600 43`. A different `crc_extra` means the field
table changed, and every GCS decoding 5600 then needs the matching `tpcm.xml`.
The `tpcm xml` command at the MAVProxy prompt prints where that file is, or:

```bash
$V/bin/python3 -c 'from MAVProxy.modules.mavproxy_tpcm.encode import dialect_xml_path; print(dialect_xml_path())'
```

## Updates

One procedure, whatever changed — module code, config, unit file or runtime
deps. Run it on each board:

```bash
cd /home/debian/fc-tpcm-relay
git pull
bash deploy/packaging/bootstrap.sh
sudo bash deploy/packaging/install_service.sh
```

The last step is `install_service.sh` rather than `systemctl restart` because
systemd reads `/etc/systemd/system/fc-tpcm-relay.service`, a copy, not the one
in the clone. `git pull` updating the unit therefore changes nothing until the
copy is replaced, and a plain restart would come back up on the old
`ExecStart` while looking like it had picked up the change.
`install_service.sh` replaces the copy, reloads systemd and restarts, and does
no harm when the unit did not change — which is why this is one procedure
instead of a decision to get wrong.

`git pull` on the shallow clone fast-forwards as long as the branch is never
rebased. See "Never rebase or force push this branch".

## Changing the module code

```bash
git checkout fc-tpcm-relay
# edit MAVProxy/modules/mavproxy_tpcm/...
git add MAVProxy/modules/mavproxy_tpcm
git commit -m "tpcm: what changed and why"
git push origin fc-tpcm-relay
```

`git add` on the directory rather than `git commit -a`: a file added to the
module is untracked, so `-a` leaves it out and the commit builds and runs
without it.

Worth doing before the push, because a broken module means the boards lose the
GCS link entirely — port 6000 is the module's, not MAVProxy's:

```bash
python scripts/run_flake8.py MAVProxy
python -c "from MAVProxy.modules.mavproxy_tpcm.messages import MAVLink_tpcm_status_message as m; print(m.id, m.crc_extra)"
```

`5600 43`. A change there is a wire format change, so every GCS decoding 5600
needs the new `tpcm.xml` too — that is not a board-only update.

Then run the update procedure on each board.

## Changing the config

`deploy/config/tpcm.yaml` is tracked, so it changes the same way the code does:
edit it on this branch, commit, push, and pull on the boards. Do not edit it on
a board — the next `git pull` conflicts, and that board is running settings no
commit describes.

`fc.device` and `fc.baud_rate` must agree with `--master` and `--baudrate` in
`packaging/fc-tpcm-relay.service`, so those two change together. Do not add
`--out=tcpin` to the unit: the `tpcm` module opens port 6000 itself, and a
MAVProxy-owned listener would bind first and make `tcp.session` unreachable.

`config/tpcm.example.yaml` is the same file with every option explained. It is
reference only; nothing reads it.

If one board ever does need different settings, do not branch for it. Point
`TPCM_CONFIG` in that board's installed unit at a file outside the clone —
`$TPCM_CONFIG` is checked before `~/.mavproxy/tpcm.yaml`.

## Never rebase or force push this branch

The boards clone it shallow, and a force-pushed branch cannot be
fast-forwarded, so every board's `git pull` fails and has to be repaired by
hand:

```bash
cd /home/debian/fc-tpcm-relay
git fetch --depth 1 origin fc-tpcm-relay
git reset --hard origin/fc-tpcm-relay
```

With more than one board that is how a fleet ends up on mixed versions. Add a
commit instead; revert one if it was wrong.

## Pinning or rolling back

Any commit on this branch works:

```bash
cd /home/debian/fc-tpcm-relay
git fetch --depth 50 origin fc-tpcm-relay
git checkout <commit-sha>
bash deploy/packaging/bootstrap.sh
sudo bash deploy/packaging/install_service.sh
```

Same last step as an update, and for the same reason: the older commit may
carry an older unit file, and only `install_service.sh` puts it in place.

The shallow clone has one commit, so the `fetch --depth` is needed before an
older commit exists locally. Afterwards the board is on a detached HEAD, which
`rev-parse HEAD` still reports correctly; `git checkout fc-tpcm-relay` returns
it to the branch.

## Editing on the board to chase a bug

```bash
# on the board, find the install location
/home/debian/fc-tpcm-venv/bin/python3 \
  -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
# -> /home/debian/fc-tpcm-venv/lib/python3.11/site-packages

# then edit the installed copy directly, e.g.
nano /home/debian/fc-tpcm-venv/lib/python3.11/site-packages/MAVProxy/modules/mavproxy_tpcm/parser.py
sudo systemctl restart fc-tpcm-relay
```

This is for narrowing down a failure, not for deploying. The clone is
untouched, so `git rev-parse HEAD` now lies about what is running, and the next
`bootstrap.sh` silently reverts the edit. Commit the fix and go back through
the update procedure.
