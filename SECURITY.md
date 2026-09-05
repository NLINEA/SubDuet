# Security policy

## Supported versions

Only the newest beta or stable release receives security fixes.

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Use GitHub private vulnerability reporting after
the repository is published. Include the affected version, impact, reproduction steps, and any
suggested mitigation. Never include live media-server, NAS, or translation credentials.

## Deployment boundary

SubDuet is designed for a trusted home network. Do not expose either service directly to the public
internet. Use a VPN or an authenticated reverse proxy and keep the bearer tokens separate.

The visual setup wizard binds to a random `127.0.0.1` port and accepts configuration writes only
with its one-time bearer token and matching local Origin. Desktop launch places that token in a URL
fragment, which is not sent in the HTTP request; the page removes the fragment from browser history
and keeps the token in memory. It loads no remote assets. Generated `paircue.env` files and
automatic backups use owner-only permissions; they contain secrets and must never be committed or
shared.

Continuous integration scans tracked files and Git history for common credential formats, private
keys, local user paths, and private AI-tool context markers. Desktop release jobs repeat the check
against the unpacked package. Findings identify only the file and category; the suspected value is
never printed. These checks are defense in depth, so contributors must still inspect every change
and keep `.env`, credential, local assistant, and editor-context files outside the repository.

Desktop Quick Pair accepts actions only from the tokenized same-origin setup page. Its two SRT
inputs are read directly from paths returned by native operating-system file windows, are limited
to 16 MB each, and are never uploaded. The browser receives only the new output filename.

Frozen desktop apps store that file under `~/Library/Application Support/SubDuet` on macOS,
`%APPDATA%\\SubDuet` on Windows, or `$XDG_CONFIG_HOME/paircue` (normally `~/.config/paircue`) on
Linux. Source installs retain `paircue.env` in the folder where setup was started.

The dashboard also binds to `127.0.0.1` by default. Desktop launches pass its bearer token in the
URL fragment, which is not sent in the HTTP request, then remove it from browser history before the
first authenticated API call. The page keeps the token in memory only and displays filenames rather
than full media paths. Do not expose the dashboard port directly to the internet.

When transcription is enabled, SubDuet sends extracted audio chunks to the configured endpoint.
When translation is enabled, it sends subtitle dialogue to the configured endpoint. These features
are disabled or unconfigured by default; review the provider's access, retention, and privacy terms
before enabling either one.

Remote translation and transcription endpoints must use HTTPS and require their own API key.
Loopback endpoints (`localhost`, `127.0.0.0/8`, or `::1`) may use HTTP and may omit a key for a
model running on the same device. SubDuet does not implement unofficial OAuth flows or reuse a
consumer AI-product login.

Each enabled AI connection also requires an explicitly approved origin. The backend and request
clients reject missing confirmations and host changes before connecting. Visual setup displays the
receiving host before enabling key entry, and clears that key and approval when the provider or
endpoint changes. Named provider presets are pinned to their official origins; custom endpoints
still require the user's own trust decision. Approval is stored locally with the configuration,
not a provider login or proof that a key belongs to that provider. Existing installations must
confirm their AI destinations once after upgrading; no private configuration is migrated or deleted.

AI request failures use bounded error categories and numeric HTTP status codes, not raw exceptions,
response URLs, headers, or bodies. HTTPX/HTTPCore diagnostics are suppressed within AI request
contexts, including debug-level output, because those diagnostics can contain provider-controlled
data. Pipeline errors, job history, and dashboard results receive the sanitized failure message.
Redirects are refused, never followed. This protects failure diagnostics, not arbitrary successful
model output; do not put credentials in subtitle text, prompts, or glossaries.

Audio selection uses language metadata and excludes marked commentary by default. An ambiguous
selection stops transcription before upload. A manual stream override bypasses these selection
checks and should only be used for a video whose tracks the user has verified.

The optional AI final quality check sends only source text, the draft translation, language/style
settings, title or episode context, and the local glossary through the same configured translation
provider. It never sends media, audio, local paths, media-server credentials, or the provider key
in the request body. The model cannot alter subtitle timing. SubDuet rejects incomplete, extra,
empty, malformed, or oversized responses before writing either translated output.

SubDuet does not collect model conversations, prompts used to develop the project, editor history,
or local AI-assistant metadata. Those materials are not runtime inputs and are blocked from release
artifacts by project policy and automated private-context checks.
