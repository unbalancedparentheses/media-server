#!/usr/bin/env bash
# Download clients: qBittorrent, SABnzbd, Unpackerr.

configure_qbittorrent() {
  info "Configuring qBittorrent..."

  # Try configured password first, then temp password
  QBIT_COOKIE=""
  for try_pass in "$QBIT_PASS" "$QBIT_TEMP_PASS"; do
    QBIT_COOKIE=$(api_retry curl -sf -c - "$QBIT_URL/api/v2/auth/login" \
      --data-urlencode "username=$QBIT_USER" --data-urlencode "password=$try_pass" 2>/dev/null | extract_cookie SID || echo "")
    [ -n "$QBIT_COOKIE" ] && break
  done

  if [ -n "$QBIT_COOKIE" ]; then
    ok "Logged in"

    # Set permanent password + preferences. Only nginx's own requests (the
    # landing page widgets) may skip the login; the *arr apps log in.
    # Reverse-proxy support makes qBittorrent judge requests that come
    # through the qbittorrent.media.local vhost by the real client address
    # (nginx sets X-Forwarded-For), so they still need a login.
    QBIT_PREFS=$(jq -nc \
      --arg user "$QBIT_USER" --arg pass "$QBIT_PASS" \
      --arg save "$DL_COMPLETE" --arg temp "$DL_INCOMPLETE" \
      --argjson ratio "$SEED_RATIO" --argjson seed_time "$SEED_TIME" \
      --arg nginx_net "$NGINX_IP/32" --arg nginx_ip "$NGINX_IP" \
      '{web_ui_username:$user, web_ui_password:$pass,
        save_path:$save, temp_path:$temp, temp_path_enabled:true,
        web_ui_port:8081, max_ratio:$ratio, max_seeding_time:$seed_time,
        up_limit:102400, web_ui_csrf_protection_enabled:true,
        bypass_auth_subnet_whitelist_enabled:true,
        bypass_auth_subnet_whitelist:$nginx_net,
        web_ui_reverse_proxy_enabled:true,
        web_ui_reverse_proxies_list:$nginx_ip}')
    curl -sf -o /dev/null "$QBIT_URL/api/v2/app/setPreferences" \
      -b "SID=$QBIT_COOKIE" \
      --data-urlencode "json=$QBIT_PREFS" 2>/dev/null && ok "Preferences + credentials set" || warn "Could not set preferences"

    for cat in sonarr sonarr-anime radarr; do
      curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/createCategory" \
        -b "SID=$QBIT_COOKIE" \
        -d "category=$cat&savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat" || \
      curl -sf -o /dev/null "$QBIT_URL/api/v2/torrents/editCategory" \
        -b "SID=$QBIT_COOKIE" \
        -d "category=$cat&savePath=$DL_COMPLETE/$cat" 2>/dev/null && ok "Category: $cat (updated)" || true
    done
  else
    warn "Could not log in"
  fi
}

configure_sabnzbd() {
  if [ -n "$SABNZBD_KEY" ]; then
    info "Configuring SABnzbd..."

    # Set download directories
    curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=complete_dir&value=/downloads/usenet/complete&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
    curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=download_dir&value=/downloads/usenet/incomplete&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
    ok "Directories: /downloads/usenet/{complete,incomplete}"

    # Create categories
    EXISTING_CATS=$(curl -sf "$SABNZBD_URL/api?mode=get_cats&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.categories[]' 2>/dev/null || echo "")
    for cat in sonarr sonarr-anime radarr; do
      if ! echo "$EXISTING_CATS" | grep -q "^${cat}$"; then
        curl -sf "$SABNZBD_URL/api?mode=set_config&section=categories&keyword=$cat&apikey=$SABNZBD_KEY&dir=$cat&output=json" >/dev/null 2>&1 && \
          ok "Category: $cat" || warn "Could not create category: $cat"
      else
        ok "Category: $cat"
      fi
    done
  fi
}

configure_usenet_providers() {
  PROVIDER_COUNT=$(cfg '.usenet_providers | length' 2>/dev/null || echo "0")
  [[ "$PROVIDER_COUNT" =~ ^[0-9]+$ ]] || PROVIDER_COUNT=0
  if [ "$PROVIDER_COUNT" -gt 0 ] && [ -n "$SABNZBD_KEY" ]; then
    info "Configuring SABnzbd usenet providers..."

    for i in $(seq 0 $((PROVIDER_COUNT - 1))); do
      PROV_ENABLED=$(cfg ".usenet_providers[$i].enable")
      [ "$PROV_ENABLED" != "true" ] && continue

      PROV_NAME=$(cfg ".usenet_providers[$i].name")
      PROV_HOST=$(cfg ".usenet_providers[$i].host")
      PROV_PORT=$(cfg ".usenet_providers[$i].port")
      PROV_SSL=$(cfg ".usenet_providers[$i].ssl")
      PROV_USER=$(cfg ".usenet_providers[$i].username")
      PROV_PASS=$(cfg ".usenet_providers[$i].password")
      PROV_CONN=$(cfg ".usenet_providers[$i].connections")

      [ "$PROV_SSL" = "true" ] && SSL_VAL=1 || SSL_VAL=0

      # SABnzbd server config via API
      curl -sf -o /dev/null "$SABNZBD_URL/api" \
        --data-urlencode "mode=config" \
        --data-urlencode "name=set_server" \
        --data-urlencode "apikey=$SABNZBD_KEY" \
        --data-urlencode "output=json" \
        --data-urlencode "keyword=$PROV_NAME" \
        --data-urlencode "host=$PROV_HOST" \
        --data-urlencode "port=$PROV_PORT" \
        --data-urlencode "ssl=$SSL_VAL" \
        --data-urlencode "username=$PROV_USER" \
        --data-urlencode "password=$PROV_PASS" \
        --data-urlencode "connections=$PROV_CONN" \
        --data-urlencode "enable=1" 2>/dev/null && \
        ok "$PROV_NAME ($PROV_HOST:$PROV_PORT)" || warn "Could not add $PROV_NAME"
    done
  fi
}

configure_sabnzbd_auth() {
  # SABnzbd auth — set username/password via API
  if [ -n "$SABNZBD_KEY" ]; then
    SAB_AUTH_USER=$(curl -sf "$SABNZBD_URL/api?mode=get_config&section=misc&apikey=$SABNZBD_KEY&output=json" 2>/dev/null | jq -r '.config.misc.username // empty' 2>/dev/null)
    if [ -z "$SAB_AUTH_USER" ]; then
      curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=username&value=$(urlencode "$JELLYFIN_USER")&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
      curl -sf "$SABNZBD_URL/api?mode=set_config&section=misc&keyword=password&value=$(urlencode "$JELLYFIN_PASS")&apikey=$SABNZBD_KEY&output=json" >/dev/null 2>&1
      ok "SABnzbd auth set: $JELLYFIN_USER"
    else
      ok "SABnzbd auth: $SAB_AUTH_USER"
    fi
  fi
}

configure_unpackerr() {
  info "Writing Unpackerr config..."

  UNPACKERR_CONF="$CONFIG_DIR/unpackerr/unpackerr.conf"
  mkdir -p "$(dirname "$UNPACKERR_CONF")"

  UNPACKERR_NEW=$(cat << UNPACKEOF
## Unpackerr — auto-generated by setup.sh

[[sonarr]]
url = "http://sonarr:8989"
api_key = "$SONARR_KEY"
paths = ["/downloads"]

[[sonarr]]
url = "http://sonarr-anime:8989"
api_key = "$SONARR_ANIME_KEY"
paths = ["/downloads"]

[[radarr]]
url = "http://radarr:7878"
api_key = "$RADARR_KEY"
paths = ["/downloads"]
UNPACKEOF
  )

  if [ ! -f "$UNPACKERR_CONF" ] || [ "$(cat "$UNPACKERR_CONF")" != "$UNPACKERR_NEW" ]; then
    printf '%s\n' "$UNPACKERR_NEW" > "$UNPACKERR_CONF"
    ok "Config written"
    docker restart unpackerr >/dev/null 2>&1 && ok "Unpackerr restarted with new config" || true
  else
    ok "Config unchanged"
  fi
}
