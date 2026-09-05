# Run SubDuet continuously with Docker

Use the desktop app for the first video. Docker is intended for a NAS or home server that should
keep scanning without a desktop login.

## Prepare the setup

Save `paircue.env` beside `docker-compose.yml`. The file contains both Docker host settings and
SubDuet settings, so there is only one configuration to manage. Start with a copied test folder or
a few media files.

The host path in `MEDIA_PATH` must be the same library that Plex, Jellyfin, or Emby reports through
`PAIRCUE_SERVER_PATH_PREFIX`. SubDuet needs read and write access so it can save subtitle sidecars.

## Build, check, and start

```bash
docker compose --env-file paircue.env build core
docker compose --env-file paircue.env run --rm core subduet doctor
docker compose --env-file paircue.env up -d core
```

This repository provides the Dockerfile for a local build; it does not publish an official
prebuilt image. The service drops Linux capabilities and runs without root privileges.

Polling is the default. The status service binds to `127.0.0.1:9292` by default and should not be
exposed directly to the public internet.

## View status

Open `http://127.0.0.1:9292/` and paste the API token from `paircue.env`, or request JSON:

```bash
curl -H "Authorization: Bearer <token from paircue.env>" http://127.0.0.1:9292/v1/status
```

The dashboard reports filenames rather than full library paths. It shows queue totals and recent
results and lets you scan again or stop SubDuet.

## Faster event triggers

Polling needs no extra setup. Plex can use its native webhook. Jellyfin's Webhook Plugin and Emby
can send `ItemAdded` events as described in [Configuration](CONFIGURATION.md#webhooks).

## Updating

Back up `paircue.env`, read [CHANGELOG.md](../CHANGELOG.md), rebuild the local image, rerun
`subduet doctor`, then start the service again. Keep a test library until the new version has
successfully produced and revealed one subtitle.
