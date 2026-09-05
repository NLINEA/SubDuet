from typing import Literal

from fastapi.testclient import TestClient

from paircue.api import create_core_app
from paircue.config import PairCueSettings
from paircue.runtime import RuntimeSnapshot
from paircue.services.state import RecentMediaState

TOKEN = "a" * 32


class DummyRuntime:
    def __init__(self) -> None:
        self.started = False
        self.rating_keys: list[str] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def scan_now(self) -> int:
        return 3

    def submit_rating_key(self, rating_key: str) -> bool:
        self.rating_keys.append(rating_key)
        return True

    def submit_item_id(self, item_id: str) -> bool:
        self.rating_keys.append(item_id)
        return True

    def status_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            pending=2,
            queued=1,
            results={"completed": 7, "failed": 1},
            recent=(
                RecentMediaState(
                    media_name="Movie.mkv",
                    status="completed",
                    message="generated and translated",
                    updated_at="2026-08-18T09:00:00+00:00",
                ),
            ),
            scan_status="ready",
            scan_message="Latest scan queued 3 items.",
        )


class DummyDesktopControl:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def request(self, action: Literal["stop", "edit"]) -> None:
        self.actions.append(action)


def _client(runtime: DummyRuntime) -> TestClient:
    settings = PairCueSettings(
        api_token=TOKEN,
        webhook_enabled=True,
        trusted_hosts="testserver",
    )
    return TestClient(create_core_app(settings, runtime))  # type: ignore[arg-type]


def _jellyfin_client(runtime: DummyRuntime) -> TestClient:
    settings = PairCueSettings(
        platform="jellyfin",
        server_url="http://jellyfin:8096",
        server_token="server-token",
        server_user_id="user-id",
        server_path_prefix="/media",
        api_token=TOKEN,
        webhook_enabled=True,
        trusted_hosts="testserver",
    )
    return TestClient(create_core_app(settings, runtime))  # type: ignore[arg-type]


def test_health_is_public_but_scan_is_protected() -> None:
    runtime = DummyRuntime()
    with _client(runtime) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/scan").status_code == 401
        response = client.post("/v1/scan", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.json() == {"queued": True, "message": "queued 3 item(s)"}


def test_local_dashboard_is_packaged_and_never_embeds_the_api_token() -> None:
    runtime = DummyRuntime()
    with _client(runtime) as client:
        page = client.get("/")
        javascript = client.get("/dashboard.js")

    assert page.status_code == 200
    assert "Your bilingual subtitle queue" in page.text
    assert TOKEN not in page.text
    assert javascript.status_code == 200
    assert TOKEN not in javascript.text
    assert "connect-src 'self'" in page.headers["content-security-policy"]


def test_status_is_protected_and_hides_full_media_path() -> None:
    runtime = DummyRuntime()
    with _client(runtime) as client:
        assert client.get("/v1/status").status_code == 401
        response = client.get(
            "/v1/status",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["results"] == {"completed": 7, "failed": 1}
    assert response.json()["scan_status"] == "ready"
    assert response.json()["recent"][0]["media_name"] == "Movie.mkv"
    assert "/media/" not in response.text


def test_dashboard_context_and_desktop_controls_are_protected() -> None:
    runtime = DummyRuntime()
    control = DummyDesktopControl()
    settings = PairCueSettings(
        api_token=TOKEN,
        trusted_hosts="testserver",
        source_language="ja",
        target_language="en",
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(
        create_core_app(settings, runtime, desktop_control=control)  # type: ignore[arg-type]
    ) as client:
        assert client.get("/v1/dashboard-context").status_code == 401
        context = client.get("/v1/dashboard-context", headers=headers)
        stopped = client.post("/v1/desktop/stop", headers=headers)
        edited = client.post("/v1/desktop/edit", headers=headers)

    assert context.json() == {
        "platform": "plex",
        "source_language": "ja",
        "target_language": "en",
        "desktop": True,
    }
    assert stopped.json()["message"] == "SubDuet is stopping"
    assert edited.json()["message"] == "SubDuet is reopening setup"
    assert control.actions == ["stop", "edit"]


def test_webhook_validates_payload_and_queues_rating_key() -> None:
    runtime = DummyRuntime()
    with _client(runtime) as client:
        response = client.post(
            "/v1/webhooks/plex",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"event": "library.new", "Metadata": {"ratingKey": "123"}},
        )

    assert response.status_code == 200
    assert runtime.rating_keys == ["123"]


def test_webhook_rejects_unknown_rating_key_shape() -> None:
    runtime = DummyRuntime()
    with _client(runtime) as client:
        response = client.post(
            "/v1/webhooks/plex",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"event": "library.new", "Metadata": {"ratingKey": "../etc/passwd"}},
        )

    assert response.status_code == 400
    assert runtime.rating_keys == []


def test_jellyfin_webhook_queues_added_movie() -> None:
    runtime = DummyRuntime()
    with _jellyfin_client(runtime) as client:
        response = client.post(
            "/v1/webhooks/jellyfin",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "NotificationType": "ItemAdded",
                "ItemId": "a1b2-c3d4",
                "ItemType": "Movie",
            },
        )

    assert response.status_code == 200
    assert runtime.rating_keys == ["a1b2-c3d4"]


def test_inactive_platform_webhook_is_hidden() -> None:
    runtime = DummyRuntime()
    with _client(runtime) as client:
        response = client.post(
            "/v1/webhooks/jellyfin",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "NotificationType": "ItemAdded",
                "ItemId": "a1b2",
                "ItemType": "Movie",
            },
        )

    assert response.status_code == 404
    assert runtime.rating_keys == []
