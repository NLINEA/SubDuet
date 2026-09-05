# Upgrading from PairCue

**PairCue is now SubDuet.** It is the same open-source project, under the same GitHub owner,
with the same subtitle workflows. Version `0.1.0b15` is the first SubDuet release.

## Desktop app

Close PairCue, download the SubDuet app for your computer from the
[official releases](https://github.com/NLINEA/SubDuet/releases), unzip it, and open **SubDuet**.
Your existing saved setup is reused. Do not run both apps against the same library at once.

The private configuration deliberately keeps its original location:

| System | Existing location, still used by SubDuet |
|---|---|
| macOS | `Library/Application Support/PairCue/paircue.env` inside your home folder |
| Windows | `PairCue/paircue.env` inside your roaming application-data folder |
| Linux | `paircue/paircue.env` inside your configured XDG config folder, or `.config` |

No credentials are moved into this repository or uploaded as part of the rename. Config backups
and private state stay local. The macOS bundle identifier also remains stable.

## Commands and configuration

The new command is `subduet`. The existing `paircue` command remains an alias with the same
arguments. Both report the new version. `python -m subduet` and `python -m paircue` both work.

Existing `paircue.env`, `PAIRCUE_` and `PAIRCUE_DS_` settings continue to work unchanged. Keep using
these documented names; the rename does not introduce a second set of environment variables.
The Python implementation remains in `paircue` so existing imports are not disrupted.

When updating a source installation, install this checkout in the same virtual environment:

```bash
python -m pip uninstall paircue
python -m pip install .
subduet --version
```

The distribution is now named `subduet`. This command installs from the checkout, not from an
unverified registry listing. Removing the old distribution first avoids overlapping package
metadata; it does not remove your separate private configuration or media files. New installations
can skip the uninstall command.

## Docker and NAS

Keep `paircue.env`, the Compose project name `paircue`, and the `paircue-state` volume. They are
intentionally unchanged so an upgrade reuses your existing configuration and state. The locally
built image is now named `subduet:0.1.0b15`.

Follow the normal [Docker upgrade steps](DOCKER.md). Do not remove the state volume to rename it.

## Repository and previous releases

The repository is now [NLINEA/SubDuet](https://github.com/NLINEA/SubDuet). Update an existing
checkout with:

```bash
git remote set-url origin https://github.com/NLINEA/SubDuet.git
```

Earlier releases retain their original PairCue archive filenames and history. New desktop
archives start with `SubDuet-`. Always use the checksum file from the same release as the archive.
