# Changelog

## Unreleased

- Require explicit AI destination confirmation for translation, transcription, and fallback.
  Show the provider host before key entry; clear entered keys when changing providers or endpoints.
  Remove implicit translation vendor/model defaults and use neutral example configuration.
- Existing AI setups must confirm their receiving origins once before reconnecting. Private keys
  and configuration files are not migrated or deleted. See the [upgrade steps](docs/CONFIGURATION.md#upgrading-an-existing-ai-setup).
- Preserve parentheses, brackets, speaker labels, lyrics, and sound-effect cues during subtitle
  preparation, avoiding accidental changes to dialogue meaning.
- Select audio by source-language metadata for both speech generation and timing alignment instead
  of always using the first track. Stop ambiguous speech generation before upload; offer a manual
  per-video stream override through the CLI/configuration. A visual track chooser is still pending.
- Add synthetic multi-track FFmpeg and no-outbound-request security regressions.
- Keep AI failure diagnostics private: report safe error categories and numeric HTTP status codes,
  never raw provider exceptions. Suppress HTTP library diagnostics during AI requests, including
  debug logs; retain normal application logs and unrelated requests' diagnostics.

## 0.1.0b15 — 2026-09-05

- Rename the product and repository to **SubDuet** — Two languages. One subtitle.
- Update setup, dashboard, documentation, provider app names, and desktop download filenames.
- Add the `subduet` command and `python -m subduet`; retain the `paircue` command and Python
  imports for compatibility.
- Preserve private configuration locations, `paircue.env`, `PAIRCUE_` settings, the macOS bundle
  identifier, and Docker state so users do not need to re-enter or move credentials.
- Extend credential-file exclusions and secret scanning to cover both product names.
- This release changes branding and upgrade compatibility; subtitle processing behavior is unchanged.

See [Upgrading from PairCue](docs/RENAMING.md).

## 0.1.0b14 - 2026-08-19

- Add an optional-by-setting, enabled-by-default AI final quality pass for translated subtitles.
  It reviews meaning, omissions, wording, and glossary consistency through the same configured
  provider, then fails closed unless every cue passes PairCue's validation again.
- Make the visual **Do everything automatically** route fall back to speech generation only when
  no existing or downloaded source subtitle can be found, then align, translate, quality-check,
  and create the bilingual SRT.
- Support user-owned remote OpenAI-compatible APIs and keyless loopback AI. Require HTTPS and an
  API key for remote translation or transcription; PairCue does not use unofficial OAuth flows.
- Keep video, audio timing, local paths, media-server credentials, and API keys out of translation
  and final-check request bodies. Bound AI request/response sizes and refuse redirects.
- Reject subtitle symlinks and oversized SRT inputs, restrict FFmpeg to local-media protocols, pin
  every GitHub Action by commit SHA, and enable extended CodeQL default scanning.
- Add a public security best-practices report covering fixed findings, verification, residual
  distribution risks, and the release gate.

## 0.1.0b13 - 2026-08-18

- Preserve existing source and target subtitles byte-for-byte by default: audio alignment now uses
  temporary working copies, low-confidence pairing cannot fall through and overwrite a target,
  and an existing bilingual result is never replaced.
- Reposition PairCue around one distinctive outcome — a portable, aligned two-language subtitle in
  the player's existing library — with a new original outcome image and Cantonese beginner page.
- Add a documentation map, troubleshooting guide, support policy, public roadmap, community
  standards, and a complete contributor setup without copying another product's code or content.
- Add `paircue --version`, automated local-link validation, and CI/release scans that report common
  credential or private AI-context risks without printing the suspected value.
- Scan tracked files, Git history, and unpacked desktop release packages before publication, and
  expand ignore rules for local environment and private-key files.

## 0.1.0b12 - 2026-08-18

- Replace filename-only release instructions with direct Apple silicon, Intel Mac, Windows, and
  Linux beta downloads.
- Reveal a voluntary first-result feedback link only after the safe demo, two-SRT pairing, or
  one-video flow succeeds; no usage data is collected or sent automatically.
- Add a privacy-safe 10-minute beta mission and a short GitHub form that measures whether a user
  reached a bilingual subtitle without outside help.
- Make every release build visibly smoke-test the packaged executable and refuse to publish unless
  all four desktop archives are present.

## 0.1.0b11 - 2026-08-18

- Add a real animated product demo and a built-in safe-demo button that creates a project-owned
  English-Spanish bilingual SRT without media, a server, an account, a key, or a network request.
- Shorten the GitHub introduction and move advanced configuration, Docker, and Download Station
  instructions into focused guides so first-time visitors see the result before setup details.
- Keep the private setup token in a URL fragment only, remove it from browser history immediately,
  and send it to the local setup server through the Authorization header rather than request URLs.
- Stop persisting the optional Download Station token in browser storage; it now stays in page
  memory and must be pasted again after a refresh.
- Add response-header clickjacking protection and a server-delivered Content Security Policy to
  the visual setup wizard.
- Publish SHA-256 checksums alongside every tagged desktop release archive.
- Group dependency updates, avoid duplicate branch runs, and keep release tooling above its
  audited security floor.

## 0.1.0b10 - 2026-08-18

- Turn first-run setup into three progressive stages: platform, first result, then only the
  settings needed for that result. The platform and its primary action now fit in a 900px desktop
  viewport instead of being buried below the marketing introduction.
- Add Kodi, Infuse, VLC, NAS, and local media as an explicit **Other players** platform choice.
- Name bilingual sidecars `Movie.mul.srt` using the ISO 639-2 multiple-languages code. Stop using
  `.cc.srt`, which Plex and Jellyfin interpret as hearing-impaired captions.
- Give Quick Pair the most prominent zero-setup path after platform selection while keeping one
  video and library automation as clear alternatives.
- Add a self-contained PairCue favicon and verify the progressive flow at desktop and mobile
  widths without remote assets, horizontal overflow, HTTP errors, or console errors.

## 0.1.0b9 - 2026-08-18

- Add **Quick Pair** to the desktop setup so a first-time user can choose two existing SRT files
  and receive a bilingual track without an account, API key, media server, or terminal command.
- Use role-specific native file windows for the spoken and learning subtitle, then reveal the
  finished file in the operating system's file manager.
- Keep Quick Pair local and origin-protected, return only the output filename to the browser, and
  limit each selected input to 16 MB.
- Never overwrite either input or an earlier paired result; use a new numbered output when a
  `.cc.srt` already exists.

## 0.1.0b8 - 2026-08-18

- Add a self-contained local dashboard with live queue totals, recent filename-only results, and a
  one-click library scan.
- Make desktop library setup verify its selected media platform before automatically opening the
  dashboard, with useful credential, network, folder, and permission failures shown in setup.
- Add a native media-folder chooser and keep failed platform checks editable instead of saving a
  configuration that cannot start.
- Hide container-only port and permission fields from desktop users while retaining them for NAS
  and home-server installs.
- Keep desktop library automation running without Docker or terminal commands and reopen an
  existing library directly on later app launches.
- Add dashboard controls to stop PairCue cleanly or return to visual setup, completing the
  returning-user loop.
- Pass the private dashboard token in a URL fragment, remove it from browser history, and keep it
  out of browser storage and static assets.
- Add narrow-screen dashboard behavior and package-level UI security tests based on rendered
  desktop and mobile QA.

## 0.1.0b7 - 2026-08-18

- Make platform selection the first setup decision, before choosing a one-video trial or
  full-library automation.
- Keep the first video's file selection, processing progress, failure guidance, and completed
  bilingual result in one visual setup journey.
- Add self-contained macOS, Windows, and Linux desktop release builds so first-time users do not
  need Python or terminal commands.
- Store desktop settings in the operating system's private application-data folder while retaining
  working-directory configuration for source installs.
- Include a runtime SBOM, Python license, and collected dependency license files in every desktop
  archive without bundling FFmpeg, models, media, or subtitles.
- Treat missing video tools as optional until speech transcription is enabled and add native video
  filters plus a second Linux file-picker fallback.
- Add a no-console-safe desktop entry point so double-click launches do not depend on terminal
  streams being present.

## 0.1.0b6 - 2026-08-18

- Add `paircue setup`, a packaged private visual wizard that saves a ready-to-use `paircue.env`
  through a token-protected localhost connection without analytics or browser storage.
- Open the setup wizard when `paircue` is run without a subcommand, so first-time users do not need
  to learn the CLI structure.
- Add `paircue learn` for running the complete subtitle pipeline on one local video without a
  media server or persistent state database.
- Make the onboarding journey start with one video, then graduate to full-library automation.
- Show video-tool readiness in the local setup page and reveal the finished subtitle in the native
  file manager after the first guided run.
- Search for both configured languages in no-translation mode and merge them when timing coverage
  passes the confidence threshold.
- Use one environment file for Docker host and PairCue settings, with the status API bound to a
  localhost-only published port.

## 0.1.0b5 - 2026-08-18

- Complete the fallback chain from existing subtitle to exact-hash search, metadata search,
  timestamped speech transcription, translation, and bilingual SRT.
- Add bounded FLAC segmentation for OpenAI-compatible transcription endpoints with atomic
  all-or-nothing output.
- Add `paircue pair` for a zero-configuration two-SRT trial.
- Add `paircue doctor` for secret-safe configuration and dependency checks.
- Add a protected `/v1/status` endpoint with queue counts and recent filename-only results.
- Centralize runtime version headers and close owned HTTP clients during shutdown.

## 0.1.0b4 - 2026-08-18

- Replace Subliminal with an independently written OpenSubtitles REST API adapter.
- Replace ffsubsync with PairCue's own conservative audio-alignment implementation.
- Add runtime license policy enforcement, third-party notices, CycloneDX SBOM generation, DCO
  checks, and contribution provenance rules.
