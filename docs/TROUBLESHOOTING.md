# Troubleshooting SubDuet

Start with the owned safe demo. If it works, the app itself is installed correctly and you can
focus on your files or library connection.

## The app will not open

The beta is not yet signed by Apple or Microsoft.

- macOS: right-click **SubDuet**, choose **Open**, then confirm once.
- Windows: in the unrecognized-publisher warning, choose **More info**, then **Run anyway** only if
  the archive came from the official SubDuet release page.
- Linux: extract the archive before running it. Do not launch the executable from inside the
  compressed archive.

Every official release includes `SHA256SUMS.txt` if you want to verify the download.

## I do not know which first result to choose

- Choose **Try safe demo** to check SubDuet without any personal files.
- Choose **Choose two SRTs** when you already have two subtitle languages.
- Choose **Try one video** when you have a video but one or both subtitle tracks may be missing.
- Choose **Automate my library** only after one video succeeds.

## The bilingual subtitle does not appear in my player

Place the result beside the video and make the base filename match:

```text
Movie.mkv
Movie.mul.srt
```

Then refresh or rescan the library. Some players cache subtitle lists, so reopening the item may be
necessary. `mul` means multilingual; it is not a hearing-impaired caption tag.

## SubDuet says the timing match is too low

The two SRT files probably came from different cuts, frame rates, or episode releases. SubDuet keeps
both inputs unchanged and refuses to guess. Try subtitle tracks made for the same release, or enable
audio alignment after installing FFmpeg.

## SubDuet says FFmpeg is missing

FFmpeg and FFprobe are not bundled. They are needed only for embedded-track extraction,
audio-based alignment, and speech generation. The safe demo and two-SRT pairing work without them.
Install FFmpeg from a source you trust, then reopen SubDuet.

## Search finds nothing

Confirm that your OpenSubtitles.com API consumer is active and that the title, year, season, and
episode metadata are correct. Search quotas and available language tracks are controlled by the
provider. You can always place a matching SRT beside the video and run SubDuet again.

## Translation or speech generation fails

If the message asks for `APPROVED_ORIGIN`, your AI destination has not been confirmed, or its host
has changed. This also affects existing AI setups after upgrading. No AI request is sent until
the confirmation matches. Follow [the upgrade steps](CONFIGURATION.md#upgrading-an-existing-ai-setup)
to verify the host yourself; do not share your key or configuration file.

Check the endpoint, model name, account credit, and provider status. SubDuet never prints the key in
its diagnostic output. Run:

```bash
subduet doctor
```

If the provider returns an incomplete translation, SubDuet publishes no partial bilingual file.

## SubDuet cannot choose the spoken audio

Check that **Spoken language** matches the audio you want, not the translation language. Files can
contain a dub, original audio, and commentary with missing or conflicting language labels.
SubDuet stops speech generation before uploading rather than guessing between them.

The error lists safe stream indexes and language labels. An advanced one-video override is available
through `subduet learn /path/to/Movie.mkv --audio-stream-index 2`; select the index for your video,
not necessarily `2`. See [audio selection](CONFIGURATION.md#choosing-the-spoken-audio-track).
If you prefer not to use the command line, supply a source-language SRT beside the video to bypass
speech generation. There is not yet a visual audio-track chooser.

## I changed settings and want to start again

Open SubDuet and return to setup from the local dashboard. SubDuet backs up an older regular
configuration file before replacing it. Do not share either file because provider and server
credentials may be inside.

## I still need help

Read [Support](../SUPPORT.md). Describe the outcome and exact safe error wording, but remove API
keys, tokens, private paths, library names, logs with personal data, screenshots of your library,
and subtitle dialogue.
