# Contributing to SubDuet

Thank you for helping make private media libraries useful for language learning. Small, focused
changes with a clear user outcome are the easiest to review.

## Before starting

- Use a [GitHub Discussion](https://github.com/NLINEA/SubDuet/discussions) for a question
  or early product idea.
- Open an issue before a large behavior, architecture, dependency, or file-format change.
- Read [Architecture](ARCHITECTURE.md), [Dependency policy](DEPENDENCY_POLICY.md), and the
  [roadmap](ROADMAP.md) when the change affects their boundaries.
- Never use private media or third-party subtitle dialogue as a test fixture. Build a minimal
  project-owned sample instead.

## Development setup

SubDuet supports Python 3.11, 3.12, and 3.13. From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip 'setuptools>=83'
python -m pip install -e '.[dev]'
```

On Windows PowerShell, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

Run SubDuet locally with `subduet setup` or inspect the CLI using `subduet --help`.

## Required checks

Run the same core gates as continuous integration:

```bash
python scripts/check_docs.py
python scripts/check_secrets.py --history
ruff check .
mypy src/paircue src/subduet
pytest --cov=paircue --cov=subduet --cov-report=term-missing --cov-fail-under=60
python scripts/check_runtime_licenses.py
pip-audit
```

Add or update tests for every bug fix and behavior change. Tests involving provider responses must
use local fakes; they must not call a real paid account or require a contributor's key.

## Privacy, provenance, and licensing

- Never commit media, credentials, `.env` files, local AI or editor context, private paths, personal
  logs, or subtitles you do not have permission to share.
- Do not copy, port, translate, decompile, or closely adapt code from another subtitle product.
  Implement from SubDuet's specifications, public standards, and official API documentation.
- Disclose a new dependency and why it is needed. Runtime dependencies must pass the automated
  license gate and receive a notice update where appropriate.
- Keep Download Station credentials and code paths isolated from the subtitle service.
- AI assistance does not change authorship responsibility: review every proposed line and confirm
  it has an acceptable origin before contributing it.

Pull requests containing copied code, unlicensed content, unexplained generated bulk changes, or
dependencies with unknown provenance will not be accepted.

## Commits and pull requests

Sign each commit:

```bash
git commit -s -m "fix: describe the user-visible outcome"
```

The sign-off certifies the [Developer Certificate of Origin](https://developercertificate.org/).
In the pull request, explain the user problem, the chosen behavior, safety or compatibility impact,
and the checks you ran. Keep unrelated cleanup separate.

By participating, follow the [community standards](CODE_OF_CONDUCT.md).
