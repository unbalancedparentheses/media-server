#!/usr/bin/env bash
# Moonbase: the Jellyfin plugin behind the Moonfin apps. It serves the Moonfin
# web app at /Moonfin/Web/ and connects Moonfin to Seerr for requests.

MOONBASE_REPO_URL="https://raw.githubusercontent.com/Moonfin-Client/Plugin/refs/heads/master/manifest.json"
MOONBASE_GUID="8c5d0e91-4f2a-4b6d-9e3f-1a7c8d9e0f2b"

configure_moonbase() {
  info "Configuring Moonbase (Moonfin)..."
  if [ -z "${JELLYFIN_TOKEN:-}" ]; then
    warn "Skipping: not logged in to Jellyfin"
    return 0
  fi
  local JT repos plugin_id
  JT=$(jf_auth "$JELLYFIN_TOKEN")

  plugin_id=$(moonbase_plugin_id)
  if [ -z "$plugin_id" ]; then
    repos=$(api GET "$JELLYFIN_URL/Repositories" -H "$JT" || echo "[]")
    if ! jq -e --arg u "$MOONBASE_REPO_URL" 'any(.[]; .Url == $u)' <<< "$repos" >/dev/null; then
      repos=$(jq -c --arg u "$MOONBASE_REPO_URL" '. + [{Name: "Moonbase", Url: $u, Enabled: true}]' <<< "$repos")
      api POST "$JELLYFIN_URL/Repositories" -H "$JT" -d "$repos" >/dev/null || { warn "Could not add the Moonbase plugin repository"; return 0; }
      ok "Plugin repository added"
    fi
    api POST "$JELLYFIN_URL/Packages/Installed/Moonbase?assemblyGuid=$MOONBASE_GUID&repositoryUrl=$(urlencode "$MOONBASE_REPO_URL")" -H "$JT" >/dev/null || \
      { warn "Could not install Moonbase"; return 0; }

    # Installation is asynchronous; the plugin loads on the next restart
    local i=0
    until compgen -G "$CONFIG_DIR/jellyfin/data/plugins/Moonbase*" >/dev/null || [ "$i" -ge 90 ]; do
      sleep 1
      i=$((i + 1))
    done
    compgen -G "$CONFIG_DIR/jellyfin/data/plugins/Moonbase*" >/dev/null || { warn "Moonbase download didn't finish; re-run setup"; return 0; }
    ok "Moonbase installed; restarting Jellyfin"
    svc_restart jellyfin
    sleep 3
    wait_for "Jellyfin" "$JELLYFIN_URL/health"
    jellyfin_login
    JT=$(jf_auth "$JELLYFIN_TOKEN")
    plugin_id=$(moonbase_plugin_id)
    [ -n "$plugin_id" ] || { warn "Moonbase didn't load after restart (see $LOG_DIR/jellyfin.log)"; return 0; }
  fi
  ok "Moonbase loaded"

  # Point Moonfin's requests at Seerr (Moonbase proxies to it server-side)
  local conf updated
  conf=$(api GET "$JELLYFIN_URL/Plugins/$plugin_id/Configuration" -H "$JT" || echo "")
  if [ -n "$conf" ]; then
    updated=$(jq -c --arg url "$SEERR_URL" '.SeerrEnabled = true | .SeerrUrl = $url' <<< "$conf")
    if [ "$updated" != "$conf" ]; then
      api POST "$JELLYFIN_URL/Plugins/$plugin_id/Configuration" -H "$JT" -d "$updated" >/dev/null && \
        ok "Seerr connected ($SEERR_URL)" || warn "Could not set the Seerr URL in Moonbase"
    else
      ok "Seerr connected ($SEERR_URL)"
    fi
  fi

  # Upstream: the web app needs the "Moonfin Startup" task once after install
  local task_id
  task_id=$(api GET "$JELLYFIN_URL/ScheduledTasks" -H "$JT" | jq -r '.[] | select(.Name | test("Moonfin Startup"; "i")) | .Id' | head -1 || true)
  if [ -n "$task_id" ]; then
    api POST "$JELLYFIN_URL/ScheduledTasks/Running/$task_id" -H "$JT" >/dev/null && ok "Moonfin web app prepared" || true
  fi

  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" "$JELLYFIN_URL/Moonfin/Web/" || true)
  case "$code" in
    2*|3*) ok "Moonfin web app: http://localhost:8096/Moonfin/Web/" ;;
    *) warn "Moonfin web app not answering yet (HTTP $code); it may need a minute" ;;
  esac
}

moonbase_plugin_id() {
  api GET "$JELLYFIN_URL/Plugins" -H "$(jf_auth "$JELLYFIN_TOKEN")" | \
    jq -r --arg g "$MOONBASE_GUID" '.[] | select((.Id | ascii_downcase | gsub("-"; "")) == ($g | gsub("-"; ""))) | .Id' | head -1 || true
}
