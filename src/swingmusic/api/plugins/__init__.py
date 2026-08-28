from flask_openapi3 import Tag
from flask_openapi3 import APIBlueprint
from pydantic import BaseModel, Field
from swingmusic.api.auth import admin_required
from swingmusic.config import UserConfig
from swingmusic.db.userdata import PluginTable
from swingmusic.plugins.lastfm import LastFmPlugin
from swingmusic.plugins.listenbrainz import ListenBrainzPlugin
from swingmusic.utils.auth import get_current_userid

bp_tag = Tag(name="Plugins", description="Manage plugins")
api = APIBlueprint("plugins", __name__, url_prefix="/plugins", abp_tags=[bp_tag])


@api.get("/")
def get_all_plugins():
    """
    List all plugins
    """
    plugins = PluginTable.get_all()
    return {"plugins": plugins}


class PluginBody(BaseModel):
    plugin: str = Field(description="The plugin name", example="lyrics")


class PluginActivateBody(PluginBody):
    active: bool = Field(
        description="New plugin active state", example=False, default=False
    )


@api.post("/setactive")
@admin_required()
def activate_deactivate_plugin(body: PluginActivateBody):
    """
    Activate/Deactivate plugin
    """
    name = body.plugin
    PluginTable.activate(name, body.active)

    return {"message": "OK"}, 200


class PluginSettingsBody(PluginBody):
    settings: dict = Field(
        description="The new plugin settings", example={"key": "value"}
    )


@api.post("/settings")
@admin_required()
def update_plugin_settings(body: PluginSettingsBody):
    """
    Update plugin settings
    """
    plugin = body.plugin
    settings = body.settings

    if not plugin or not settings:
        return {"error": "Missing plugin or settings"}, 400

    PluginTable.update_settings(plugin, settings)
    plugin = PluginTable.get_by_name(plugin)

    return {"status": "success", "settings": plugin.settings}


class LastFmSessionBody(BaseModel):
    token: str = Field(description="The token to use to create the session")


@api.post("/lastfm/session/create")
def create_lastfm_session(body: LastFmSessionBody):
    """
    Create a Last.fm session
    """
    if not body.token:
        return {"error": "Missing token"}, 400

    lastfm = LastFmPlugin(current_userid=get_current_userid())
    session_key = lastfm.get_session_key(body.token)

    if session_key:
        config = UserConfig()
        current_user = get_current_userid()
        config.lastfmSessionKeys[str(current_user)] = session_key
        config.lastfmSessionKeys = config.lastfmSessionKeys

    return {"status": "success", "session_key": session_key}


@api.post("/lastfm/session/delete")
def delete_lastfm_session():
    """
    Delete the Last.fm session
    """
    config = UserConfig()
    current_user = get_current_userid()
    config.lastfmSessionKeys[str(current_user)] = ""
    config.lastfmSessionKeys = config.lastfmSessionKeys

    return {"status": "success"}


class ListenBrainzTokenBody(BaseModel):
    token: str = Field(description="ListenBrainz user token")
    base_url: str | None = Field(
        default=None, description="Custom ListenBrainz API base URL"
    )


@api.post("/listenbrainz/token")
def set_listenbrainz_token(body: ListenBrainzTokenBody):
    """
    Save ListenBrainz token for current user
    """
    if not body.token:
        return {"error": "Missing token"}, 400

    base_url = body.base_url.strip().rstrip("/") if body.base_url else None

    config = UserConfig()
    current_user = get_current_userid()

    if base_url:
        config.listenbrainzBaseUrl = base_url

    plugin = ListenBrainzPlugin(current_userid=current_user)
    if not plugin.validate_token(body.token, base_url):
        return {"error": "Invalid token"}, 401

    config.listenbrainzTokens[str(current_user)] = body.token
    config.listenbrainzTokens = config.listenbrainzTokens

    return {"status": "success"}


@api.delete("/listenbrainz/token")
def delete_listenbrainz_token():
    """
    Delete ListenBrainz token for current user
    """
    config = UserConfig()
    current_user = get_current_userid()
    try:
        config.listenbrainzTokens.pop(str(current_user))
    except KeyError:
        pass
    config.listenbrainzTokens = config.listenbrainzTokens

    return {"status": "success"}


@api.get("/listenbrainz/status")
def listenbrainz_status():
    """
    Check ListenBrainz connectivity for current user
    """
    config = UserConfig()
    current_user = get_current_userid()
    token = config.listenbrainzTokens.get(str(current_user), "")
    if not token:
        return {"connected": False}

    plugin = ListenBrainzPlugin(current_userid=current_user)
    valid = plugin.validate_token(token)
    return {"connected": valid, "base_url": config.listenbrainzBaseUrl}
