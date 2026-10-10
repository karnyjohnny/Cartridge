"""Render the Add-game dialog offscreen against a MOCK provider (temporary)."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from cartridge.app_state import AppState  # noqa: E402
from cartridge.core.secret_store import SecretStore  # noqa: E402
from cartridge.providers.base import HttpSettings, HttpClient  # noqa: E402
from cartridge.providers.ratelimit import TEST_POLICY, RateLimiter  # noqa: E402
from cartridge.providers.service import MetadataService  # noqa: E402
from cartridge.ui.theme import apply_theme  # noqa: E402
from tests._png import gradient_png_bytes  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "igdb_search.json",
)


def main() -> int:
    import tempfile

    app = QApplication(sys.argv[:1])
    apply_theme(app)

    tmp = tempfile.mkdtemp(prefix="cartridge-dialog-shot-")
    state = AppState(
        db_path=os.path.join(tmp, "c.db"),
        secret_store=SecretStore(directory=os.path.join(tmp, "s"), preferred="memory"),
    )
    state.open_database(os.path.join(tmp, "c.db"))
    root = os.path.join(tmp, "Gry")
    os.makedirs(os.path.join(root, "Wiedzmin 2"))
    state.repo.add_root(root)

    payload = json.load(open(FIXTURE, encoding="utf-8"))["payload"]

    def handler(request):
        url = str(request.url)
        if "id.twitch.tv" in url:
            return httpx.Response(200, json={"access_token": "tok0123456789abcdef", "expires_in": 3600})
        if "images.igdb.com" in url or "media.rawg.io" in url:
            return httpx.Response(200, content=gradient_png_bytes(120, 160))
        if "api.igdb.com" in url:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"results": []})

    client = HttpClient(
        settings=HttpSettings(max_retries=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        limiter=RateLimiter(TEST_POLICY, sleep=lambda _s: None),
    )
    state.http = client
    state.service = MetadataService(client=client, limiters=state.limiters)
    state.service.set_credentials("x" * 30, "y" * 30, "")

    from cartridge.ui.dialogs.add_game import AddGameDialog

    dialog = AddGameDialog(state, None, suggested_folder=os.path.join(root, "Wiedzmin 2"))
    dialog.resize(980, 700)
    dialog.show()
    for _ in range(8):
        app.processEvents()
        time.sleep(0.02)
    dialog._set_page("search")
    dialog.search_edit.setText("The Witcher 2")
    dialog._on_search()
    deadline = time.time() + 8.0
    while time.time() < deadline and (dialog._search_handle is not None or not dialog._rows):
        app.processEvents()
        time.sleep(0.02)
    deadline = time.time() + 8.0
    while time.time() < deadline and len(dialog._thumb_paths) < 2:
        app.processEvents()
        time.sleep(0.05)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)

    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "artifacts", "screenshots", "cartridge-addgame-mockprovider.png",
    )
    dialog.grab().save(out, "PNG")
    print("wrote", out)
    print("rows:", len(dialog._rows), "thumbs:", sorted(dialog._thumb_paths))
    dialog.reject()
    state.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
