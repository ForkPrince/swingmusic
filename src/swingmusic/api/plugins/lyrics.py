from flask_openapi3 import Tag
from flask_openapi3 import APIBlueprint
from pydantic import Field
from swingmusic.api.apischemas import TrackHashSchema
from swingmusic.config import UserConfig
from swingmusic.lib.lyrics import Lyrics as Lyrics_class

from swingmusic.plugins.lyrics import Lyrics, LRCLIBProvider
from swingmusic.premium import CloudError, LicenseError
from swingmusic.settings import Defaults
from swingmusic.utils.hashing import create_hash

bp_tag = Tag(name="Lyrics Plugin", description="Lyrics plugin (LRCLIB + Musixmatch)")
api = APIBlueprint(
    "lyricsplugin", __name__, url_prefix="/plugins/lyrics", abp_tags=[bp_tag]
)


class LyricsSearchBody(TrackHashSchema):
    title: str = Field(description="The track title ", example=Defaults.API_TRACKNAME)
    artist: str = Field(
        description="The track artist ", example=Defaults.API_ARTISTNAME
    )
    album: str = Field(
        description="The track track album ", example=Defaults.API_ALBUMNAME
    )
    filepath: str = Field(
        description="Track filepath to save the lyrics file relative to",
        example="/home/cwilvx/temp/crazy song.mp3",
    )


@api.post("/search")
def search_lyrics(body: LyricsSearchBody):
    """
    Search for lyrics by title and artist

    Tries LRCLIB first (free, no API key), then falls back to Musixmatch.
    """
    title = body.title
    artist = body.artist
    album = body.album
    filepath = body.filepath
    trackhash = body.trackhash

    # 1. Try LRCLIB first (free, best synced lyrics coverage)
    lrclib = LRCLIBProvider()
    lrclib_result = lrclib.get_lyrics(title, artist, album)

    if lrclib_result:
        synced = lrclib_result.get("syncedLyrics")
        plain = lrclib_result.get("plainLyrics")

        if synced:
            lyrics = Lyrics_class(synced)
            return {
                "trackhash": trackhash,
                "lyrics": lyrics.format_synced_lyrics(),
                "source": "lrclib",
            }, 200
        elif plain:
            return {
                "trackhash": trackhash,
                "lyrics": plain,
                "source": "lrclib",
            }, 200

    # 2. Try premium cloud lyrics (if available)
    try:
        from swingmusic.store.tracks import TrackStore
        from swingmusic.premium.plugins.lyrics import CloudLyricsPlugin

        track = TrackStore.get_tracks_by_filepaths([body.filepath])[0]

        lrc, _ = CloudLyricsPlugin().get_lyrics(track)
        if lrc:
            return {
                "trackhash": trackhash,
                "lyrics": lrc.format_synced_lyrics(),
                "source": "cloud",
            }, 200

    except CloudError as e:
        print(f"Error getting lyrics from cloud server: {e}")

        if e.status_code == 404 and UserConfig().trustCloudLyrics:
            return {"trackhash": trackhash, "lyrics": None, "source": "cloud"}, 404
        else:
            pass
    except LicenseError as e:
        print("Error getting lyrics from cloud server: ", e)
        pass
    except ModuleNotFoundError:
        # Premium module not available in this build
        pass

    # 3. Fallback to Musixmatch
    finder = Lyrics()
    data = finder.search_lyrics_by_title_and_artist(title, artist)

    if not data:
        return {"trackhash": trackhash, "lyrics": None}

    perfect_match = data[0]

    for track in data:
        i_title = track["title"]
        i_album = track["album"]

        if create_hash(i_title) == create_hash(title) and create_hash(
            i_album
        ) == create_hash(album):
            perfect_match = track

    track_id = perfect_match["track_id"]
    lrc = finder.download_lyrics(track_id, filepath)

    if lrc is not None:
        lyrics = Lyrics_class(lrc)
        return {"trackhash": trackhash, "lyrics": lyrics.format_synced_lyrics()}, 200

    return {"trackhash": trackhash, "lyrics": lrc}, 200
