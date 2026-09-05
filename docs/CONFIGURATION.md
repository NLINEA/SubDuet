# SubDuet configuration

SubDuet was previously PairCue. Existing `paircue.env` files and `PAIRCUE_` variable names are
intentionally unchanged; [upgrade details](RENAMING.md) explain the compatibility choices.

SubDuet's desktop setup asks for a platform and a result first, then reveals only the settings that
result needs. This reference is for advanced setup, command-line use, and `paircue.env` files.

Start with a single video or the project-owned safe demo before automating a full library. Run
`subduet doctor` to check a saved setup without printing secrets.

## Pick one media source

SubDuet processes one media server or filesystem root per installation.

Plex:

```dotenv
PAIRCUE_PLATFORM=plex
PAIRCUE_SERVER_URL=http://plex:32400
PAIRCUE_SERVER_TOKEN=your-plex-token
PAIRCUE_SERVER_PATH_PREFIX=/volume1/Media
```

Jellyfin:

```dotenv
PAIRCUE_PLATFORM=jellyfin
PAIRCUE_SERVER_URL=http://jellyfin:8096
PAIRCUE_SERVER_TOKEN=your-api-key
PAIRCUE_SERVER_USER_ID=your-user-id
PAIRCUE_SERVER_PATH_PREFIX=/media
```

Use `PAIRCUE_PLATFORM=emby` with the Emby URL and credentials for Emby. For a plain media folder:

```dotenv
PAIRCUE_PLATFORM=filesystem
```

`PAIRCUE_SERVER_PATH_PREFIX` is the library path reported by the server. `MEDIA_PATH` is that same
library on the Docker host; Docker mounts it as `PAIRCUE_MEDIA_ROOT=/media` inside SubDuet. Existing
`PAIRCUE_PLEX_*` variables remain accepted for backward compatibility.

The selected folder must be readable and writable by SubDuet so it can save the SRT beside each
video. Stop SubDuet before disconnecting a network drive.

## Languages and line order

The setup page shows common language names and keeps their standard BCP-47 tags in the field. You
can choose a suggestion or type another valid tag. Environment files use the tags directly:

```dotenv
PAIRCUE_SOURCE_LANGUAGE=ja
PAIRCUE_TARGET_LANGUAGE=en
PAIRCUE_BILINGUAL_ORDER=target-first
```

Common tags include `en`, `es`, `fr`, `ja`, `ko`, `zh-TW`, `zh-HK`, `zh-Hant`, `zh-CN`, and
`pt-BR`. Source and target must differ. `target-first` places the learning language on top;
`source-first` places the spoken language on top.

To describe a regional style more precisely:

```dotenv
PAIRCUE_TARGET_LANGUAGE_NAME=Traditional Chinese (Hong Kong)
PAIRCUE_TARGET_LANGUAGE_STYLE=natural Hong Kong wording suitable for subtitles
```

When AI translation is disabled, SubDuet can search for the target language instead. Chinese
targets can also use a safe OpenCC script conversion when applicable.

## Translation provider

SubDuet works with OpenAI-compatible translation endpoints:

```dotenv
PAIRCUE_TRANSLATION_BASE_URL=https://your-provider.example/v1
PAIRCUE_TRANSLATION_API_KEY=your-api-key
PAIRCUE_TRANSLATION_MODEL=your-model
PAIRCUE_TRANSLATION_FINAL_CHECK_ENABLED=true
```

A second compatible provider can be configured as fallback. Subtitle text is sent to an enabled
translation provider, so review its privacy policy, retention, pricing, and model terms first.

The final quality check is a second request through the same configured provider. It receives the
source text, draft translation, language/style settings, title or episode context, and glossary. It
does not receive video, audio, local paths, media-server credentials, API keys in the request body,
or other local data. It checks meaning, omissions, natural wording, and glossary consistency; then
SubDuet independently validates exact cue coverage again. Timing remains local and cannot be
changed by the model.

Translation is fail-closed: SubDuet publishes no translated or bilingual result if either pass is
missing a cue, adds an unexpected cue, returns empty text, exceeds size limits, or fails.

Remote AI endpoints must use HTTPS and an API key. A loopback endpoint such as
`http://127.0.0.1:11434/v1` or `http://localhost:9000/v1` can run without a key, allowing a local
OpenAI-compatible model to keep subtitle text on the device. SubDuet does not use unofficial OAuth
or borrow a consumer AI-account login.

## Subtitle search and download

SubDuet contains an independently written adapter for the documented OpenSubtitles.com REST API.
It calculates the OpenSubtitles file hash, tries an exact release match, then falls back to title,
year, season, and episode metadata.

Create your own OpenSubtitles API consumer and set:

```dotenv
PAIRCUE_SUBTITLE_DOWNLOAD_ENABLED=true
PAIRCUE_OPENSUBTITLES_API_KEY=your-api-key
```

An account login is optional. If used, set both `PAIRCUE_OPENSUBTITLES_USERNAME` and
`PAIRCUE_OPENSUBTITLES_PASSWORD`. Search is disabled without an API key. API quotas, provider
terms, and permission to use downloaded subtitle content remain the user's responsibility.

## Generate subtitles from speech

If no source subtitle exists, SubDuet can extract the first audio track into bounded FLAC chunks
and call an OpenAI-compatible transcription endpoint. Every returned segment and timestamp is
validated before the source SRT is published.

```dotenv
PAIRCUE_TRANSCRIPTION_ENABLED=true
PAIRCUE_TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
PAIRCUE_TRANSCRIPTION_API_KEY=your-api-key
PAIRCUE_TRANSCRIPTION_MODEL=whisper-1
```

`whisper-1` is the safe default because its documented API supports timestamped `verbose_json`
segments. A compatible self-hosted endpoint can also be used. Transcription is off by default and
sends extracted audio to the configured endpoint when enabled. FFmpeg is required and not bundled.
Remote endpoints require HTTPS and an API key; loopback endpoints may use HTTP without a key.

With **Do everything automatically** selected in visual setup, this stage becomes the final
fallback after existing and downloaded source subtitles. SubDuet does not upload audio when it has
already found a usable source track.

## Pair two existing subtitle languages

When both sidecars exist, such as `Movie.ja.srt` and `Movie.en.srt`, SubDuet matches cues by time and
creates `Movie.mul.srt` without translation. One cue can safely align with two shorter cues in the
other track.

The default minimum is 70% timing coverage in both tracks. SubDuet refuses to publish below that
confidence. Advanced thresholds are configurable:

```dotenv
PAIRCUE_BILINGUAL_MERGE_TOLERANCE_MS=350
PAIRCUE_BILINGUAL_MERGE_MIN_MATCH_RATIO=0.7
```

## Automatic synchronization

Synchronization is enabled by default. SubDuet uses a user-installed FFmpeg to decode temporary
mono audio, then its own activity detector and FFT cross-correlation to estimate subtitle offset.
Existing SRT files are copied to a temporary working directory first. SubDuet keeps the originals
untouched and keeps the working timing unchanged if confidence is too low.

```dotenv
PAIRCUE_SYNC_ENABLED=true
PAIRCUE_SYNC_MAX_OFFSET_SECONDS=120
PAIRCUE_SYNC_MIN_CONFIDENCE=0.24
```

The translated and bilingual cues inherit the synchronized source timing. Writes are atomic.

## Webhooks

Polling requires no webhook setup. For faster events, Jellyfin or Emby can send authenticated JSON
to `/v1/webhooks/jellyfin` or `/v1/webhooks/emby`:

```json
{"NotificationType":"ItemAdded","ItemId":"the-item-id","ItemType":"Movie"}
```

Use `Content-Type: application/json` and `Authorization: Bearer <PAIRCUE_API_TOKEN>`. Jellyfin's
official Webhook Plugin supports custom generic templates. Plex can use its native webhook.

## Files written

- `Movie.<source>.srt` — synchronized source subtitle.
- `Movie.<target>.srt` — target-language subtitle.
- `Movie.mul.srt` — both languages sharing one timeline.

Existing source and target subtitles are preserved byte-for-byte by default. Non-dialogue cleanup
affects the new target and bilingual result in memory, not the original file. The advanced
`PAIRCUE_CLEAN_SOURCE_OUTPUT=true` option explicitly rewrites the source with the synchronized,
dialogue-only cues; leave it `false` unless that destructive behavior is intentional.
