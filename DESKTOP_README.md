# SubDuet Desktop beta

SubDuet Desktop lets you start without installing Python or learning terminal commands.

SubDuet was previously called PairCue. Open the new SubDuet app to keep using your saved setup.
The private configuration stays in its existing PairCue folder; no key needs to be copied or
uploaded for this rename. Close the old app before opening the new one.

1. Open **SubDuet**.
2. First, choose where you watch: Plex, Jellyfin, Emby, Kodi, Infuse, VLC, or a media folder.
3. Choose the result you want first: run the safe demo, combine two SRTs, try one video, or
   automate the library.
4. SubDuet then shows only the settings needed for that result.

Want to check the installation first? Press **Try safe demo**. SubDuet creates and highlights a
tiny English-Spanish bilingual SRT using only dialogue written for this project. It needs no media,
server, account, API key, or network request.

Already have two SRT files? Press **Choose two SRTs**, choose the spoken subtitle and then the
learning subtitle. SubDuet creates a new bilingual `.mul.srt` locally, highlights it in the file
manager, and does not require you to finish setup or add any API key. Both original files remain
byte-for-byte unchanged.
SubDuet then ends that app run cleanly; reopen it whenever you want to pair another set.

For one video, SubDuet opens the system file picker and reports progress in the same setup page. A
successful bilingual `.mul.srt` is highlighted in Finder or your file manager. `mul` means
multiple languages and avoids falsely marking the result as hearing-impaired captions.

For library automation, SubDuet checks the selected platform and media folder before opening its
private local dashboard. The dashboard shows work in progress and recent results without revealing
full library paths. It can scan immediately, stop SubDuet, or return to setup. Reopen the app later
to go straight back to the dashboard. No Docker or terminal command is required for this desktop
flow. Use **Choose folder** instead of typing a path; a failed connection check stays on the setup
page so the address or credential can be corrected before anything is saved.

The beta builds are not yet code-signed. macOS may require right-clicking the app and choosing
**Open** the first time; Windows SmartScreen may show an unrecognized-publisher warning. Do not
download SubDuet from unofficial mirrors.

FFmpeg and FFprobe are not bundled because their effective license depends on how they were built.
SubDuet can still merge two existing SRT tracks or search and translate downloaded subtitles
without them. Embedded-track extraction, audio synchronization, and speech generation require a
separate FFmpeg installation.

SubDuet has no analytics or SubDuet account. Provider features use credentials from your own
provider accounts, and the setup stores them only in your private local configuration. SubDuet
aligns existing subtitles through temporary copies and leaves them unchanged by default.

If you get stuck, read the official Troubleshooting and Support links on the SubDuet GitHub page.
