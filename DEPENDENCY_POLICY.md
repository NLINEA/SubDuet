# Dependency and provenance policy

SubDuet's application code is independently implemented for this repository. Contributors must not
copy, port, translate, decompile, or closely adapt source code from competing subtitle products.
Public standards, academic descriptions, and official service documentation may be used to learn
required behaviour, but code must be written independently.

## Runtime dependencies

The default runtime accepts permissive licenses listed in `license-policy.toml`. The existing
MPL-2.0 `certifi` dependency is a package-scoped reviewed exception, not approval for new MPL
dependencies. Strong copyleft, source-available, proprietary, and unknown licenses fail the
automated gate. LGPL dependencies require explicit legal and architectural review and are not
accepted by default.

Every change to runtime dependencies must:

1. pass `python scripts/check_runtime_licenses.py` in a clean installation;
2. update `THIRD_PARTY_NOTICES.md` when a new direct dependency or external executable is added;
3. produce the CycloneDX SBOM in CI; and
4. avoid vendoring dependency source, model weights, media, or subtitle samples.

The gate walks the installed production graph from SubDuet and fails closed on unknown license
metadata. `license_overrides` may be used only after a manual upstream-license review, with the
reason recorded in the pull request.

Desktop archives use PyInstaller only at build time under its GPL-2.0-or-later Bootloader Exception.
Each archive must contain SubDuet's license and notices, a runtime CycloneDX SBOM, the Python
license, and the installed license or notice files for every distribution in SubDuet's runtime
dependency graph. The archive also carries the complete packaged license files for PyInstaller and
its official hooks. The build fails closed if one of those distributions has no packaged license.

## External services and executables

- Subtitle-provider integrations must use documented official APIs and user-supplied credentials.
  HTML scraping, bypassing quotas, or copying provider client code is not accepted.
- SubDuet does not redistribute downloaded subtitles. Users are responsible for provider terms and
  for the rights to download, transform, and store subtitle content in their jurisdiction.
- FFmpeg is an external executable. Its effective license depends on how it was built. SubDuet does
  not include it in source or desktop archives and does not publish a prebuilt container image;
  anyone distributing a locally built image must satisfy the FFmpeg build's applicable license and
  source-notice obligations.
- Optional model engines and weights must be reviewed separately before they can be bundled or
  distributed. A permissive engine license does not automatically cover every model or dataset.

## Contributions

All commits must carry a DCO sign-off. Contributors certify that they wrote the change or otherwise
have the right to submit it under SubDuet's MIT license. Generated or AI-assisted code receives the
same provenance and review requirements as manually typed code.
