"""Helpers shared by the Sonarr, Radarr and Prowlarr steps (the *arr apps
have the same API shape)."""
from __future__ import annotations

from typing import Any

from mediaserver import api, creds, logins
from mediaserver.api import ApiError
from mediaserver.config import Config
from mediaserver.ui import ok, warn


ALLOWED_HOSTS = "localhost,127.0.0.1"


def set_login(cfg: Config, label: str, url: str, key: str, version: str, service: str) -> None:
    """The web login (forms) as config.toml's Jellyfin login: required, or
    not asked on this Mac when the admin pages answer only there
    (network.admin_bind = "127.0.0.1"). The API key allows setting it
    without the old password. Applied when the record or the app's settings
    differ, then checked by logging in; only a working login is recorded."""
    h = {"X-Api-Key": key}
    user, password = cfg.jellyfin_user, cfg.jellyfin_pass
    required = "disabledForLocalAddresses" if cfg.admin_local_only else "enabled"
    how = " (not asked on this Mac)" if cfg.admin_local_only else ""
    # Newer versions want the host names they answer to when the login can
    # be skipped (which also refuses a page that renamed itself localhost)
    hosts = {"allowedHosts": ALLOWED_HOSTS} if cfg.admin_local_only else {}
    try:
        host = api.get(f"{url}/api/{version}/config/host", h)
    except ApiError:
        warn(f"{label}: could not read its login settings")
        return
    if creds.match(cfg.paths.state, service, user, password) and host.get("username") == user \
            and host.get("authenticationMethod") == "forms" and host.get("authenticationRequired") == required \
            and all(host.get(k) == v for k, v in hosts.items() if k in host):
        ok(f"{label} login: {user}{how}")
        return
    body = dict(host, authenticationMethod="forms", authenticationRequired=required, username=user,
                password=password, passwordConfirmation=password, **{k: v for k, v in hosts.items() if k in host})
    try:
        api.call("PUT", f"{url}/api/{version}/config/host/{host['id']}", h, body=body)
    except ApiError:
        warn(f"{label}: could not set its login (retried next run)")
        return
    if logins.arr(url, user, password):
        creds.record(cfg.paths.state, service, user, password)
        ok(f"{label} login set: {user}{how}")
    else:
        warn(f"{label}: the new login doesn't work yet (retried next run)")


def sync_fields(label: str, resource_url: str, resource_id: Any, fields: dict, key: str) -> None:
    """Set named fields on an existing resource (a download client, an
    application...) so changed passwords, API keys and URLs reach it.
    Secrets read back masked, so the update is sent every run."""
    h = {"X-Api-Key": key}
    try:
        current = api.get(f"{resource_url}/{resource_id}", h)
    except ApiError:
        warn(f"{label}: could not read its settings")
        return
    current["fields"] = [dict(f, value=fields[f["name"]]) if fields.get(f.get("name")) is not None else f
                         for f in current.get("fields") or []]
    try:
        api.call("PUT", f"{resource_url}/{resource_id}?forceSave=true", h, body=current)
    except ApiError:
        warn(f"{label}: could not update its settings")
