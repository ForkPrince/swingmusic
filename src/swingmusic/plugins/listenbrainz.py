import json
import time
import requests
from typing import Any

from swingmusic.config import UserConfig
from swingmusic.models.track import Track
from swingmusic.settings import Paths
from swingmusic.utils.threading import background
from swingmusic.plugins import Plugin, plugin_method

from swingmusic.logger import log


class ListenBrainzPlugin(Plugin):
    UPLOADING_DUMPS = False

    def __init__(self, current_userid: int):
        self.config = UserConfig()
        self.current_userid = current_userid
        super().__init__("listenbrainz", "ListenBrainz scrobbler")
        token = self.config.listenbrainzTokens.get(str(self.current_userid), "")
        self.set_active(bool(token))

    def _get_headers(self) -> dict[str, str]:
        token = self.config.listenbrainzTokens.get(str(self.current_userid), "")
        return {
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        }

    def _get_base_url(self) -> str:
        return self.config.listenbrainzBaseUrl.rstrip("/")

    def validate_token(self, token: str, base_url: str | None = None) -> bool:
        url = f"{(base_url or self._get_base_url()).rstrip('/')}/1/validate-token"
        try:
            res = requests.get(
                url, headers={"Authorization": f"Token {token}"}, timeout=10
            )
            data = res.json()
            return data.get("valid") is True or res.status_code == 200 and data.get("code") == 200
        except Exception as e:
            log.warn(f"ListenBrainz validate error: {e}")
            return False

    def _build_payload(self, track: Track, timestamp: int | None, listen_type: str) -> dict[str, Any]:
        track_metadata: dict[str, Any] = {
            "artist_name": track.artists[0]["name"] if track.artists else "Unknown Artist",
            "track_name": track.title,
        }
        if track.album:
            track_metadata["release_name"] = track.album

        additional_info: dict[str, Any] = {}
        if track.duration:
            additional_info["duration"] = int(track.duration)
        if track.albumartists and track.albumartists[0].get("name"):
            additional_info["release_artist_name"] = track.albumartists[0]["name"]
        if track.track:
            additional_info["tracknumber"] = track.track
        additional_info["submission_client"] = "SwingMusic"
        additional_info["media_player"] = "SwingMusic"
        if additional_info:
            track_metadata["additional_info"] = additional_info

        payload: dict[str, Any] = {"track_metadata": track_metadata}
        if listen_type == "single":
            payload["listened_at"] = int(timestamp) if timestamp else int(time.time())

        return {
            "listen_type": listen_type,
            "payload": [payload],
        }

    def _post_listens(self, payload: dict[str, Any]) -> bool:
        url = f"{self._get_base_url()}/1/submit-listens"
        try:
            res = requests.post(url, headers=self._get_headers(), json=payload, timeout=15)
        except Exception as e:
            log.warn(f"ListenBrainz request error: {e}")
            return False

        if res.status_code in (200, 202):
            try:
                data = res.json()
                if data.get("status") == "ok":
                    return True
            except Exception:
                pass
            return True

        try:
            body = res.json()
        except Exception:
            body = res.text

        log.error(f"ListenBrainz submit error {res.status_code}: {body}")

        if res.status_code == 401:
            return False

        return False

    @plugin_method
    @background
    def scrobble(self, track: Track, timestamp: int):
        payload = self._build_payload(track, timestamp, "single")
        success = self._post_listens(payload)

        if not success:
            self.dump_scrobble(payload)
        else:
            self.upload_dumps()

        return success

    @plugin_method
    @background
    def submit_playing_now(self, track: Track):
        payload = self._build_payload(track, None, "playing_now")
        try:
            self._post_listens(payload)
        except Exception as e:
            log.warn(f"ListenBrainz playing_now error: {e}")

    def dump_scrobble(self, data: dict[str, Any]):
        dump_dir = Paths().plugins_path / "listenbrainz"
        if not dump_dir.exists():
            dump_dir.mkdir(parents=True, exist_ok=True)

        path = dump_dir / f"{int(time.time() * 1000)}.json"
        path.write_text(json.dumps(data))

    def upload_dumps(self):
        if self.UPLOADING_DUMPS:
            return

        self.UPLOADING_DUMPS = True
        dump_dir = Paths().plugins_path / "listenbrainz"

        if not dump_dir.exists():
            self.UPLOADING_DUMPS = False
            return

        try:
            for file in sorted(dump_dir.iterdir()):
                if file.suffix != ".json":
                    continue
                try:
                    with open(file, "r") as f:
                        data = json.load(f)
                except Exception:
                    file.unlink(missing_ok=True)
                    continue

                success = self._post_listens(data)
                if success:
                    file.unlink(missing_ok=True)
                else:
                    break
        finally:
            self.UPLOADING_DUMPS = False
