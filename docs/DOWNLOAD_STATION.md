# Optional Download Station service

Download Station is not part of SubDuet's main subtitle workflow. It remains available as an
isolated optional service for users who already understand torrent sources and their local laws.

It does not receive the media-server token, translation key, SubDuet state volume, or media-library
mount. Use a separate API token and do not expose it directly to the public internet.

## Start it

Copy `downloads.env.example` to `downloads.env`, replace the example token with a different strong
token, then run:

```bash
docker compose --env-file paircue.env --profile downloads up -d downloads
```

The default binding is `127.0.0.1:9293`. Use a VPN or trusted authenticated reverse proxy if remote
access is required. Open the page, enter the separate Download Station API token, then add a magnet
or upload a small `.torrent` file.

You are responsible for verifying the source, licence, and right to download or use any content.
SubDuet does not include a torrent search engine or copyrighted media catalogue.
