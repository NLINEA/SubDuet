import re
from html.parser import HTMLParser
from pathlib import Path

import paircue


class SetupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.csp = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(str(values["href"]))
        if tag == "meta" and values.get("http-equiv") == "Content-Security-Policy":
            self.csp = str(values.get("content") or "")


def _setup_root() -> Path:
    return Path(paircue.__file__).with_name("setup")


def test_visual_setup_is_self_contained_and_only_calls_its_local_origin() -> None:
    root = _setup_root()
    html = (root / "index.html").read_text(encoding="utf-8")
    javascript = (root / "setup.js").read_text(encoding="utf-8")
    stylesheet = root / "setup.css"
    parser = SetupParser()
    parser.feed(html)

    assert stylesheet.is_file()
    assert parser.scripts == ["setup.js"]
    assert parser.stylesheets == ["setup.css"]
    assert len(parser.ids) == len(set(parser.ids))
    assert "connect-src 'self'" in parser.csp
    assert not re.search(r"\b(XMLHttpRequest|WebSocket|sendBeacon)\b", javascript)
    assert 'fetch("/config"' in javascript
    assert 'fetch("/progress"' in javascript
    assert "Authorization: `Bearer ${setupToken}`" in javascript
    assert "window.location.hash.slice(1)" in javascript
    assert "window.history.replaceState" in javascript
    assert "window.location.search).get(\"token\")" not in javascript
    assert not re.search(r"src=[\"']https?://", html)
    external_links = re.findall(r"href=[\"'](https?://[^\"']+)", html)
    assert external_links == [
        "https://github.com/NLINEA/SubDuet/issues/new?template=beta_report.yml",
        "https://github.com/NLINEA/SubDuet/issues/new?template=beta_report.yml",
    ]
    assert html.count('rel="noopener noreferrer"') == 2


def test_visual_setup_javascript_only_references_existing_elements() -> None:
    root = _setup_root()
    html = (root / "index.html").read_text(encoding="utf-8")
    javascript = (root / "setup.js").read_text(encoding="utf-8")
    parser = SetupParser()
    parser.feed(html)
    referenced_ids = set(re.findall(r'byId\("([A-Za-z0-9-]+)"\)', javascript))

    assert referenced_ids <= set(parser.ids)
    assert "docker compose --env-file paircue.env run --rm core subduet doctor" in javascript
    assert "subduet learn" in javascript
    assert "docker compose --env-file paircue.env" in javascript
    assert "Your bilingual subtitle is ready" in javascript
    assert 'fetch("/context"' in javascript
    assert 'fetch("/test-platform"' in javascript
    assert 'fetch("/choose-folder"' in javascript
    assert 'fetch(\n      `/quick-pair?order=' in javascript
    assert "`/demo-pair?order=" in javascript
    assert "Try safe demo" in html
    assert "Which language should appear on top?" in html
    assert "No Docker or terminal command required." in javascript
    assert 'id="quick-feedback" class="feedback-prompt" hidden' in html
    assert 'id="next-feedback" class="feedback-prompt" hidden' in html
    assert javascript.count("feedback.hidden = false;") == 3
    assert "github.com" not in javascript
    assert "Nothing is sent automatically." in html
    assert 'configLine("PAIRCUE_CLEAN_SOURCE_OUTPUT", "false")' in javascript
    assert 'id="translation-final-check-enabled" type="checkbox" checked' in html
    assert '"PAIRCUE_TRANSLATION_FINAL_CHECK_ENABLED"' in javascript
    assert "never sees\n                        your video or file paths" in html
    assert "automatic: { search: true, translation: true, transcription: true }" in javascript


def test_visual_setup_asks_for_platform_before_starting_mode() -> None:
    html = (_setup_root() / "index.html").read_text(encoding="utf-8")

    assert html.index('aria-label="Media platform"') < html.index(
        'aria-label="Starting point"'
    )
    assert html.index("Where will you watch?") < html.index(
        "What should SubDuet do first?"
    )
    assert 'id="journey-stage" hidden' in html
    assert 'id="details-stage" hidden' in html
    assert html.index('id="continue-platform"') < html.index('id="continue-journey"')
    assert "Kodi, Infuse, VLC, NAS" in html
