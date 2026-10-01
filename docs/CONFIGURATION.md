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

SubDuet works with OpenAI-compatible translation endpoints. Visual setup asks you to select a
provider, review its receiving host, and confirm the destination **before** you can enter a key.
Changing the provider or endpoint clears the entered key and confirmation. No translation provider
or model is selected automatically.

For an environment file, verify the endpoint in your provider's documentation before adding its
key and explicitly approving the origin (scheme, host, optional port; no path):

```dotenv
PAIRCUE_TRANSLATION_ENABLED=true
PAIRCUE_TRANSLATION_PROVIDER=custom
PAIRCUE_TRANSLATION_BASE_URL=https://your-provider.example/v1
PAIRCUE_TRANSLATION_APPROVED_ORIGIN=https://your-provider.example
PAIRCUE_TRANSLATION_API_KEY=your-api-key
PAIRCUE_TRANSLATION_MODEL=your-model
PAIRCUE_TRANSLATION_FINAL_CHECK_ENABLED=true
```

Use `openai` for `https://api.openai.com`, `zai` for `https://api.z.ai`, `local` for a loopback
server on this device, or `custom` for another compatible provider. Named providers reject other
origins. URL credentials, query strings, and fragments are not accepted. An approved origin records
your choice; it does not certify a custom provider's trustworthiness or its model compatibility.

A second compatible provider can be configured through `PAIRCUE_FALLBACK_PROVIDER`, `BASE_URL`,
`APPROVED_ORIGIN`, `API_KEY`, and `MODEL` (all with the `PAIRCUE_FALLBACK_` prefix). It needs its
own destination confirmation and credentials. Leave all fallback connection fields empty if unused.
Subtitle text is sent to an enabled provider, so review its privacy policy, retention, pricing, and
model terms first.

### Upgrading an existing AI setup

The next release requires destination confirmation for existing translation, transcription, and
fallback connections. A missing or mismatched `APPROVED_ORIGIN` stops the connection before a
request is sent. Your existing keys and configuration files are not migrated or deleted.

Reopen setup to choose and confirm each provider, or privately edit your environment file after
checking the endpoint. For example, an existing `https://api.openai.com/v1` endpoint needs
`PAIRCUE_TRANSLATION_APPROVED_ORIGIN=https://api.openai.com` for translation. Transcription uses
`PAIRCUE_TRANSCRIPTION_APPROVED_ORIGIN`; fallback uses `PAIRCUE_FALLBACK_APPROVED_ORIGIN`.
Do not copy an origin from an error message or approve a host you do not recognize. If changing
providers, replace the key too. Never paste your environment file into an issue or chat.

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

If no source subtitle exists, SubDuet can extract the selected spoken audio into bounded FLAC chunks
and call an OpenAI-compatible transcription endpoint. Every returned segment and timestamp is
validated before the source SRT is published.

```dotenv
PAIRCUE_TRANSCRIPTION_ENABLED=true
PAIRCUE_TRANSCRIPTION_PROVIDER=openai
PAIRCUE_TRANSCRIPTION_BASE_URL=https://api.openai.com/v1
PAIRCUE_TRANSCRIPTION_APPROVED_ORIGIN=https://api.openai.com
PAIRCUE_TRANSCRIPTION_API_KEY=your-api-key
PAIRCUE_TRANSCRIPTION_MODEL=whisper-1
```

Choose a model that supports timestamped `verbose_json` segments; the example uses `whisper-1`.
A compatible self-hosted endpoint can also be used. Transcription is off by default and
sends extracted audio to the configured endpoint when enabled. FFmpeg is required and not bundled.
Remote endpoints require HTTPS and an API key; loopback endpoints may use HTTP without a key.

With **Do everything automatically** selected in visual setup, this stage becomes the final
fallback after existing and downloaded source subtitles. SubDuet does not upload audio when it has
already found a usable source track.

### Choosing the spoken audio track

FFprobe reads each audio stream's language metadata. SubDuet prefers the selected source language,
excludes marked commentary/audio-description tracks, and uses a unique default within matching
candidates. It does not identify spoken language by listening to the audio. A single unlabelled
track can be used; multiple unlabelled tracks, unresolved matches, or a known language mismatch
stop speech generation before upload. Automatic timing alignment uses the same selection policy
and keeps existing timing if selection fails.

For an ambiguous video, use the global stream index listed in the safe error message:

```bash
subduet learn /path/to/Movie.mkv --audio-stream-index 2
```

`PAIRCUE_AUDIO_STREAM_INDEX=2` provides the same override in an environment file. This is FFmpeg's
global stream index, not “the second audio track.” It overrides language/commentary checks, so use
it only after verifying that particular video. Do not reuse an index across a mixed library. A
visual per-video audio chooser is not available yet.

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

## Persistent automatic attempts

Library polling and webhook jobs use a SQLite attempt ledger. The default allows only the first
automatic attempt; additional retries require an explicit policy. Configure `PAIRCUE_RETRY_POLICY`
as JSON with `max_auto_attempts` (1–10, including the first), `retry_delays_ms` (1–9 nonnegative
integer delays, reusing the last for later waits), and `lease_ms` (1–86400000). Defaults are one
attempt, delays `[60000,300000]`, and a 900000 ms lease. These are operational choices, not universal
subtitle correctness or provider-budget limits. The synthetic C12 test explicitly uses three
attempts and delays `[1000,4000]`; those are test parameters, not a production recommendation.

The complete media and SRT contents, selected language/audio settings, output-affecting recipe,
glossary/context, and explicit recipe/validation versions determine job identity. Credentials,
polling intervals, retry policy, and output paths do not. Each identity keeps its original policy
and spent allowance across polling, process restart, restored inputs, and moved identical inputs.
Changing policy applies to new identities; it does not mint a new allowance for an existing job.
Changing source contents or output settings creates a different identity. Recipe/validation
versions must change when corresponding processing rules change.
Recorded generated-track content is normalized through the content ledger across paths. Conflicting
recorded original identities are blocked rather than guessed or granted a fresh allowance.

Claims and counters commit before processing. Duplicate workers share the claim. Owner, fence and
lease are checked after each temporary file is prepared, immediately before its atomic publication,
under the SQLite writer guard. An expired or superseded claim is refused at that boundary. A native
filesystem call can cross the lease deadline after that check; if it succeeds, the returned blocked
result explicitly lists the published files and requires provenance recovery, rather than claiming
no publication or committing success. A stale worker cannot replace a successor's state. Failures become
`retry_wait` with a persisted deadline, then `retry_exhausted` at the limit. Set the lease long
enough for the expected processing time; this implementation does not renew a long-running lease.
SRT work stays in a disposable staging folder until guarded publication. Media is read through a
local link and its complete hash is rechecked before publication; it is not an immutable copy.
Full media hashing adds I/O and currently holds the SQLite writer lock during the final check.

The protected `POST /v1/retry` endpoint accepts `{"item_id":"the-library-item-id"}`. Each accepted
claim records exactly one manual attempt separately; it never resets the automatic counter.
Queued requests are not durable until claimed. Legacy records without trustworthy counters stay
`blocked` with `legacy_attempts_unknown`; an explicit manual attempt is allowed, but their unknown
automatic allowance never silently becomes zero, including after settings changes.

Before publishing, the ledger commits an intent containing frozen input/settings/version hashes,
exact destination paths, expected output hashes and sizes, the result message, and full review
details. It then commits each prepared file's device/inode identity before final creation. Files
are created without replacement, except the existing explicit source-cleaning option. After a
restart, only a complete match of that proof, current inputs/settings, and every published file
can restore `completed`. Recovery restores the original partial-pairing/readability review details
and consumes no attempt, performs no file publication, and calls no provider. A file with the same
name or even the same bytes but a different identity is not adopted.

Missing, changed, partial, symlinked, legacy, or unsupported proofs stay `blocked`; existing files
are kept. No inferred proof is added to old outputs. Inspect an unwanted file before archiving it.
An explicit manual retry may rebuild an archived final from a completed recorded attempt with
matching input/settings/version identity as a separate attempt; it does not reset automatic
allowance. Incomplete publication intents cannot
be restarted through that exception. An expired native call that successfully created a file is
reported as publication occurring, with exact observed paths retained in the attempt's ledger;
subsequent recovery still requires the full proof and cannot let the stale owner change a
successor's state.

Filesystem publication and SQLite completion are separate operations, not one atomic transaction.
Recovery reconciles a process crash; it is not a power-loss or malicious filesystem/ledger tampering
guarantee. It requires stable device/inode identities, hard-link support, and safe nonblocking
descriptor opens. Recovery refuses non-regular files and validates the opened descriptor before
hashing; missing nonblocking support fails closed. Input-freezing errors are stored as blocked
with prior review details retained, unless a newer or active worker's state must be preserved.
NAS and other filesystem/platform behavior still need validation. The check cannot prevent a file
being changed after verification. Abrupt exit may leave staging or hidden temporary files; recovery
keeps these remnants rather than guessing which files to delete. These limits count pipeline
attempts, not each provider's internal
HTTP retry; they do not guarantee exactly-once provider calls or a spend cap.
Explicit one-shot `learn` processing is separate from automatic polling and its retry allowance.

## Readability review

Existing-track pairing reports a separate readability warning even when timing coverage is 100%.
Background summaries reserve space for both partial-pairing and readability reasons and counts.
The full unmatched cue lists and measured readability diagnostics are stored separately in SQLite,
without subtitle text. Expand **Review cue details** on the dashboard to browse pages of at most
100 entries; refreshing an unchanged result keeps the open page. The authenticated
`GET /v1/reviews/{review_id}?offset=0` route serves the same bounded pages. New results replace
the previous details for that media item. These diagnostics never change subtitle bytes.
It lists the output SRT cue, measurement, and limit. Warnings never reflow, split, or retime cues,
drop text, or invent translations. Partial timing coverage keeps its separate missing-language
cue notice. A saved file and full timing coverage do not prove meaning or playback readability.

The generic **proposed draft profile** has these configurable fields:

| JSON field | Default | Measurement |
|---|---:|---|
| `source_line_codepoints_max` | 42 | Longest source line, including spaces and punctuation |
| `target_line_codepoints_max` | 42 | Longest target line, including spaces and punctuation |
| `lines_per_language_max` | 2 | Lines in each emitted language block |
| `total_lines_max` | 4 | Lines in the complete output cue |
| `duration_ms_max` | 7000 | Duration of the output cue in milliseconds |

These are review prompts, not universal standards. Unicode codepoint counts do not measure pixels,
font width, clipping, or normal-speed reading. This profile does not check reading speed or meaning.
Limits are inclusive, must be positive integers, and omitted fields keep their defaults. Source
and target refer to the input roles even when the display order is reversed.

For CLI pairing, save a JSON profile such as `{"target_line_codepoints_max":22}` and pass its path:

```bash
subduet pair Movie.en.srt Movie.zh-TW.srt -o Movie.mul.srt --readability-profile readability.json
```

The library service accepts the same JSON object in the optional configuration variable:

```dotenv
PAIRCUE_READABILITY_PROFILE={"target_line_codepoints_max":22}
```

Desktop Quick Pair uses the generic profile and shows review reasons in its existing result panel.
It has no profile editor. Relaxing a limit changes the warning only; the subtitle output stays the
same. Outputs that need review are still saved for intentional inspection.

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

Existing source and target subtitles are preserved byte-for-byte by default. Working copies
normalize whitespace but retain parentheses, brackets, speaker labels, sound-effect cues, and
lyrics: removing these by pattern can change the meaning of dialogue. The advanced
`PAIRCUE_CLEAN_SOURCE_OUTPUT=true` option explicitly rewrites the source with synchronized,
whitespace-normalized cues; leave it `false` unless that destructive behavior is intentional.
