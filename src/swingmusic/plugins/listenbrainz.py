import json
import re
import time
import uuid
import requests
from pathlib import Path
from typing import Any

from swingmusic.config import UserConfig
from swingmusic.models.track import Track
from swingmusic.settings import Paths
from swingmusic.utils.threading import background
from swingmusic.plugins import Plugin, plugin_method

from swingmusic.logger import log

from importlib.metadata import version as _pkg_version

_MBID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_VERSION: str | None = None


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
        artist_names = [a.get("name") for a in (track.artists or []) if a.get("name")]
        artist_names = [n.strip() for n in artist_names if n and n.strip()]
        artist_name = ", ".join(artist_names) if artist_names else "Unknown Artist"

        track_metadata: dict[str, Any] = {
            "artist_name": artist_name,
            "track_name": track.title or track.og_title or "Unknown Title",
        }
        if track.album and track.album.strip() and track.album.strip().lower() != "unknown":
            track_metadata["release_name"] = track.album.strip()

        additional_info: dict[str, Any] = {}
        if track.duration:
            try:
                additional_info["duration_ms"] = int(round(float(track.duration) * 1000))
            except Exception:
                pass
        if track.track:
            additional_info["tracknumber"] = str(track.track)
        if track.disc:
            try:
                disc = int(track.disc)
                if disc > 0:
                    additional_info["discnumber"] = str(disc)
            except Exception:
                pass
        if track.albumartists:
            ra_names = [a.get("name", "").strip() for a in track.albumartists if a.get("name")]
            ra_names = [n for n in ra_names if n]
            if ra_names:
                additional_info["release_artist_name"] = ", ".join(ra_names)
        if track.genres:
            try:
                if isinstance(track.genres, list):
                    tags = [g.get("name", "").strip() for g in track.genres if isinstance(g, dict) and g.get("name")]
                    tags = [t for t in tags if t][:50]
                    if tags:
                        additional_info["tags"] = tags
                elif isinstance(track.genres, str) and track.genres.strip():
                    additional_info["tags"] = [track.genres.strip()]
            except Exception:
                pass
        for k, v in self._extract_extra_info(track).items():
            if k not in additional_info and v not in (None, "", [], {}):
                additional_info[k] = v
        ver = self._get_version()
        additional_info["submission_client"] = "SwingMusic"
        additional_info["media_player"] = "SwingMusic"
        if ver:
            additional_info["submission_client_version"] = ver
            additional_info["media_player_version"] = ver
        if additional_info:
            track_metadata["additional_info"] = additional_info

        payload: dict[str, Any] = {"track_metadata": track_metadata}
        if listen_type == "single":
            payload["listened_at"] = int(timestamp) if timestamp else int(time.time())

        return {
            "listen_type": listen_type,
            "payload": [payload],
        }

    def _get_version(self) -> str:
        global _VERSION
        if _VERSION is not None:
            return _VERSION
        try:
            _VERSION = _pkg_version("swingmusic")
        except Exception:
            try:
                from swingmusic import __version__ as _v  # type: ignore

                _VERSION = str(_v)
            except Exception:
                _VERSION = ""
        return _VERSION

    def _is_valid_mbid(self, v: str) -> bool:
        try:
            uuid.UUID(v)
            return bool(_MBID_RE.match(v))
        except Exception:
            return False

    def _extract_extra_info(self, track: Track) -> dict[str, Any]:
        extra = getattr(track, "extra", None) or {}
        file_mbids = self._extract_mbids_from_file(track.filepath) if getattr(track, "filepath", None) else {}
        norm: dict[str, Any] = {}
        for k, v in extra.items():
            nk = re.sub(r"[\s_\-]+", "", str(k).lower())
            norm[nk] = v
        for k, v in file_mbids.items():
            nk = re.sub(r"[\s_\-]+", "", str(k).lower())
            if nk not in norm or not norm[nk]:
                norm[nk] = v

        def _first_val(raw: Any) -> str:
            if isinstance(raw, list):
                for item in raw:
                    if item:
                        return str(item).strip()
                return ""
            if raw is None:
                return ""
            return str(raw).strip()

        def _all_vals(raw: Any) -> list[str]:
            if isinstance(raw, list):
                out: list[str] = []
                for item in raw:
                    s = str(item).strip()
                    if s:
                        out.extend([p.strip() for p in re.split(r"[;,]", s) if p.strip()])
                return out
            if raw is None:
                return []
            s = str(raw).strip()
            if not s:
                return []
            return [p.strip() for p in re.split(r"[;,]", s) if p.strip()]

        def lookup(*cands: str) -> str:
            for c in cands:
                nc = re.sub(r"[\s_\-]+", "", c.lower())
                if nc in norm:
                    v = _first_val(norm[nc])
                    if v:
                        return v
                for nk, nv in norm.items():
                    if nc in nk or nk in nc:
                        v = _first_val(nv)
                        if v:
                            return v
            return ""

        def lookup_list(*cands: str) -> list[str]:
            for c in cands:
                nc = re.sub(r"[\s_\-]+", "", c.lower())
                if nc in norm:
                    vals = _all_vals(norm[nc])
                    if vals:
                        return vals
                for nk, nv in norm.items():
                    if nc in nk or nk in nc:
                        vals = _all_vals(nv)
                        if vals:
                            return vals
            return []

        out: dict[str, Any] = {}
        isrc = lookup("isrc", "tsrc")
        if isrc:
            out["isrc"] = isrc.upper() if re.match(r"^[A-Za-z0-9\-]+$", isrc) else isrc
        spotify_id = lookup("spotifyid", "spotifytrackid", "spotifyurl", "spotify_id")
        if spotify_id:
            if spotify_id.startswith("http"):
                out["spotify_id"] = spotify_id
                m = re.search(r"track/([a-zA-Z0-9]+)", spotify_id)
                if m and "spotify_id" not in out:
                    out["spotify_id"] = f"https://open.spotify.com/track/{m.group(1)}"
            elif re.match(r"^[a-zA-Z0-9]{22}$", spotify_id):
                out["spotify_id"] = f"https://open.spotify.com/track/{spotify_id}"
            else:
                out["spotify_id"] = spotify_id
        origin = lookup("originurl", "origin_url", "url", "website")
        if origin and origin.startswith("http") and "spotify_id" not in out:
            out["origin_url"] = origin
        for lb_key, cands in [
            ("recording_mbid", ["musicbrainztrackid", "musicbrainzrecordingid", "recordingmbid"]),
            ("release_mbid", ["musicbrainzalbumid", "musicbrainzreleaseid", "releasembid"]),
            ("release_group_mbid", ["musicbrainzreleasegroupid", "releasegroupmbid"]),
            ("track_mbid", ["musicbrainzreleasetrackid", "trackmbid"]),
        ]:
            v = lookup(*cands)
            if v and self._is_valid_mbid(v):
                out[lb_key] = v.lower()
        for lb_key, cands in [
            ("artist_mbids", ["musicbrainzartistid", "artistmbids", "musicbrainzalbumartistid"]),
            ("work_mbids", ["musicbrainzworkid", "workmbids"]),
        ]:
            vals = lookup_list(*cands)
            vals = [v.lower() for v in vals if self._is_valid_mbid(v)]
            if vals:
                out[lb_key] = list(dict.fromkeys(vals))
        return out

    def _extract_mbids_from_file(self, filepath: str) -> dict[str, Any]:
        try:
            p = Path(filepath)
            if not p.exists() or not p.is_file():
                return {}
            try:
                from mutagen import File as MutagenFile  # type: ignore
            except Exception:
                return {}
            audio = MutagenFile(filepath)
            if audio is None or not getattr(audio, "tags", None):
                return {}
            tags = audio.tags  # type: ignore
            out: dict[str, Any] = {}
            def add(k: str, v: Any):
                if v:
                    out[k] = v
            for key, val in tags.items():
                lk = str(key).lower()
                vals = val if isinstance(val, list) else [val]
                vals = [str(x).strip() for x in vals if str(x).strip()]
                if not vals:
                    continue
                v0 = vals[0]
                if "musicbrainz_trackid" in lk or "ufid:http://musicbrainz.org" in lk:
                    add("musicbrainz_trackid", v0)
                elif "musicbrainz_albumid" in lk:
                    add("musicbrainz_albumid", v0)
                elif "musicbrainz_releasegroupid" in lk:
                    add("musicbrainz_releasegroupid", v0)
                elif "musicbrainz_artistid" in lk:
                    add("musicbrainz_artistid", ";".join(vals))
                elif "musicbrainz_workid" in lk:
                    add("musicbrainz_workid", ";".join(vals))
                elif lk == "isrc" or lk.endswith(":isrc"):
                    add("isrc", v0)
                elif "spotify" in lk:
                    add("spotify_id", v0)
            return out
        except Exception:
            return {}

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
