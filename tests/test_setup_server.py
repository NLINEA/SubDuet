import stat
import threading
from pathlib import Path

import httpx
import pytest

import paircue
from paircue.setup_server import (
    QuickPairResult,
    SetupConnectionError,
    SetupHTTPServer,
    parse_config_values,
)

VALID_CONFIG = """PAIRCUE_PLATFORM="filesystem"
PAIRCUE_SOURCE_LANGUAGE="en"
PAIRCUE_TARGET_LANGUAGE="ja"
"""

SINGLE_JELLYFIN_CONFIG = """PAIRCUE_PLATFORM="jellyfin"
PAIRCUE_SOURCE_LANGUAGE="en"
PAIRCUE_TARGET_LANGUAGE="ja"
"""


@pytest.mark.parametrize("approval", ["", "https://approved.example"])
@pytest.mark.parametrize("endpoint", ["/config", "/test-platform"])
def test_unapproved_ai_never_saves_or_calls_connection_test(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, approval: str, endpoint: str,
) -> None:
    output = tmp_path / "paircue.env"
    attempts = []
    server = SetupHTTPServer(
        Path(paircue.__file__).with_name("setup"), output, desktop=True,
        connection_test=lambda config: attempts.append(config) or "must not run",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = VALID_CONFIG + '\n'.join([
        'PAIRCUE_TRANSLATION_ENABLED="true"',
        'PAIRCUE_TRANSLATION_BASE_URL="https://unapproved.example/v1"',
        f'PAIRCUE_TRANSLATION_APPROVED_ORIGIN="{approval}"',
        'PAIRCUE_TRANSLATION_API_KEY="test-only-secret-do-not-render"',
        'PAIRCUE_TRANSLATION_MODEL="model"',
    ])
    try:
        response = httpx.post(
            server.origin + endpoint,
            headers={"Origin": server.origin, "Authorization": f"Bearer {server.token}"},
            json={"config": config, "mode": "library"},
        )
        assert response.status_code == 400
        assert not output.exists()
        assert not attempts
        assert not server.state.saved.is_set()
        assert "test-only-secret-do-not-render" not in response.text + caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_setup_server_serves_local_assets_and_saves_with_backup(tmp_path: Path) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    output = tmp_path / "paircue.env"
    output.write_text("old configuration\n", encoding="utf-8")
    server = SetupHTTPServer(assets, output)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            page = client.get("/")
            favicon = client.get("/favicon.svg")
            readiness = client.get("/readiness")
            context = client.get("/context")
            forbidden = client.post(
                "/config",
                headers={"Origin": server.origin, "Authorization": "Bearer wrong"},
                json={"config": VALID_CONFIG},
            )
            saved = client.post(
                "/config",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
                json={"config": VALID_CONFIG, "mode": "single"},
            )
            wrong_progress = client.get(
                "/progress", headers={"Authorization": "Bearer wrong"}
            )
            pending_progress = client.get(
                "/progress", headers={"Authorization": f"Bearer {server.token}"}
            )
            completed_output = tmp_path / "Private Movie.mul.srt"
            server.state.update_progress(
                "completed",
                "created bilingual subtitles",
                (completed_output,),
            )
            completed_progress = client.get(
                "/progress", headers={"Authorization": f"Bearer {server.token}"}
            )
            repeated = client.post(
                "/config",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
                json={"config": VALID_CONFIG, "mode": "single"},
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert page.status_code == 200
    assert "SubDuet Setup" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in favicon.content
    assert readiness.status_code == 200
    assert set(readiness.json()) == {"ready", "ffmpeg", "ffprobe"}
    assert context.json() == {"desktop": False}
    assert forbidden.status_code == 403
    assert saved.status_code == 200
    assert saved.json()["saved"] is True
    assert saved.json()["location"] == str(tmp_path)
    assert wrong_progress.status_code == 403
    assert pending_progress.json()["phase"] == "saved"
    assert pending_progress.json()["terminal"] is False
    assert completed_progress.json() == {
        "phase": "completed",
        "message": "created bilingual subtitles",
        "outputs": ["Private Movie.mul.srt"],
        "action_url": "",
        "terminal": True,
    }
    assert str(tmp_path) not in completed_progress.text
    assert repeated.status_code == 409
    assert output.read_text(encoding="utf-8") == VALID_CONFIG
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    backup = server.state.backup_path
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "old configuration\n"
    assert server.state.saved.is_set()
    assert server.state.delivered.is_set()
    assert server.state.mode == "single"


def test_setup_server_reports_desktop_context_without_exposing_secrets(tmp_path: Path) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    server = SetupHTTPServer(assets, tmp_path / "paircue.env", desktop=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            response = client.get("/context")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.json() == {"desktop": True}
    assert server.token not in response.text


def test_desktop_platform_is_checked_before_configuration_is_saved(tmp_path: Path) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    output = tmp_path / "paircue.env"
    attempts = 0

    def connection_test(config: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SetupConnectionError("The server did not accept that token.")
        assert parse_config_values(config)["PAIRCUE_PLATFORM"] == "filesystem"
        return "Connected to the media folder."

    server = SetupHTTPServer(
        assets,
        output,
        desktop=True,
        connection_test=connection_test,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {
        "Origin": server.origin,
        "Authorization": f"Bearer {server.token}",
    }
    try:
        with httpx.Client(base_url=server.origin) as client:
            rejected = client.post(
                "/test-platform",
                headers=headers,
                json={"config": VALID_CONFIG, "mode": "library"},
            )
            accepted = client.post(
                "/test-platform",
                headers=headers,
                json={"config": VALID_CONFIG, "mode": "library"},
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert rejected.status_code == 400
    assert rejected.json() == {"ok": False, "message": "The server did not accept that token."}
    assert not output.exists()
    assert accepted.json() == {"ok": True, "message": "Connected to the media folder."}
    assert not output.exists()


def test_desktop_folder_chooser_requires_setup_origin_and_returns_selected_path(
    tmp_path: Path,
) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    media = tmp_path / "Media"
    media.mkdir()
    server = SetupHTTPServer(
        assets,
        tmp_path / "paircue.env",
        desktop=True,
        choose_folder=lambda: media,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            rejected = client.post(
                "/choose-folder",
                headers={"Authorization": f"Bearer {server.token}"},
            )
            selected = client.post(
                "/choose-folder",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert rejected.status_code == 403
    assert selected.json() == {"selected": True, "path": str(media)}


def test_desktop_quick_pair_is_origin_protected_and_returns_only_the_output_name(
    tmp_path: Path,
) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    output = tmp_path / "Private Library" / "Movie.mul.srt"
    observed_orders: list[str] = []

    def quick_pair(order: str) -> QuickPairResult:
        observed_orders.append(order)
        return QuickPairResult(output, 0.95, 0.9)

    server = SetupHTTPServer(
        assets,
        tmp_path / "paircue.env",
        desktop=True,
        quick_pair=quick_pair,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            rejected = client.post(
                "/quick-pair?order=target-first",
                headers={"Authorization": f"Bearer {server.token}"},
            )
            invalid = client.post(
                "/quick-pair?order=unknown",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
            )
            completed = client.post(
                "/quick-pair?order=target-first",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert rejected.status_code == 403
    assert invalid.status_code == 400
    assert completed.json() == {
        "completed": True,
        "filename": "Movie.mul.srt",
        "message": "Created a bilingual subtitle (95%/90% matched).",
    }
    assert str(tmp_path) not in completed.text
    assert observed_orders == ["target-first"]
    assert server.state.quick_pair_output == output
    assert server.state.quick_pair_completed.is_set()


def test_desktop_safe_demo_uses_a_separate_origin_protected_action(tmp_path: Path) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    output = tmp_path / "SubDuet Demo.mul.srt"
    observed_orders: list[str] = []

    def demo_pair(order: str) -> QuickPairResult:
        observed_orders.append(order)
        return QuickPairResult(output, 1, 1)

    server = SetupHTTPServer(
        assets,
        tmp_path / "paircue.env",
        desktop=True,
        demo_pair=demo_pair,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            rejected = client.post(
                "/demo-pair?order=target-first",
                headers={"Authorization": f"Bearer {server.token}"},
            )
            completed = client.post(
                "/demo-pair?order=target-first",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert rejected.status_code == 403
    assert completed.json() == {
        "completed": True,
        "filename": "SubDuet Demo.mul.srt",
        "message": "Created a bilingual subtitle (100%/100% matched).",
    }
    assert observed_orders == ["target-first"]
    assert server.state.quick_pair_output == output


def test_setup_server_rejects_cross_origin_and_oversized_requests(tmp_path: Path) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    output = tmp_path / "paircue.env"
    server = SetupHTTPServer(assets, output)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            cross_origin = client.post(
                "/config",
                headers={
                    "Origin": "https://example.com",
                    "Authorization": f"Bearer {server.token}",
                },
                json={"config": "unsafe\n"},
            )
            invalid_config = client.post(
                "/config",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
                json={
                    "config": 'PAIRCUE_SOURCE_LANGUAGE="en"\n'
                    'PAIRCUE_TARGET_LANGUAGE="en"\n'
                },
            )
            oversized = client.post(
                "/config",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                    "Content-Type": "application/json",
                },
                content=b"x" * (64 * 1024 + 1),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert cross_origin.status_code == 403
    assert invalid_config.status_code == 400
    assert oversized.status_code == 413
    assert not output.exists()


def test_single_video_setup_remembers_platform_without_requiring_server_credentials(
    tmp_path: Path,
) -> None:
    assets = Path(paircue.__file__).with_name("setup")
    output = tmp_path / "paircue.env"
    server = SetupHTTPServer(assets, output)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=server.origin) as client:
            saved = client.post(
                "/config",
                headers={
                    "Origin": server.origin,
                    "Authorization": f"Bearer {server.token}",
                },
                json={"config": SINGLE_JELLYFIN_CONFIG, "mode": "single"},
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert saved.status_code == 200
    assert output.read_text(encoding="utf-8") == SINGLE_JELLYFIN_CONFIG
