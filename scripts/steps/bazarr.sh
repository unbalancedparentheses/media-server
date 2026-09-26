#!/usr/bin/env bash

configure_bazarr() {
  info "Configuring Bazarr..."

  BAZARR_CONFIG=""
  for f in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
    [ -f "$f" ] && BAZARR_CONFIG="$f" && break
  done

  if [ -n "$BAZARR_CONFIG" ]; then
    ok "Config: $BAZARR_CONFIG"

    # Edit with a YAML parser: line-based edits corrupted the file when a
    # value's shape changed (e.g. a one-line list becoming a block list)
    local bazarr_result
    if bazarr_result=$(python3 - "$BAZARR_CONFIG" "$SONARR_KEY" "$RADARR_KEY" "$SUBTITLE_PROVIDERS" "$SUBTITLE_LANGS" "${ADMIN_BIND:-0.0.0.0}" << 'PYEOF'
import copy, sys, yaml

path, sonarr_key, radarr_key, providers, langs, bind = sys.argv[1:7]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
before = copy.deepcopy(cfg)
general = cfg.setdefault("general", {})

for app, key, port in (("sonarr", sonarr_key, 8989), ("radarr", radarr_key, 7878)):
    if key:
        # base_url "": Bazarr rewrites "/" to it, which would count as a change every run
        cfg.setdefault(app, {}).update(ip="localhost", port=port, base_url="", apikey=key, ssl=False)
        general[f"use_{app}"] = True

if providers:
    general["enabled_providers"] = [p.strip() for p in providers.split(",") if p.strip()]
if langs:
    general["serie_default_enabled"] = True
    general["movie_default_enabled"] = True

general.update(
    # Minimum score filters out mislabeled subs
    minimum_score=70, minimum_score_movie=70,
    # Upgrade subs when a better match appears
    upgrade_subs=True, upgrade_frequency=12, days_to_upgrade_subs=7,
    # Prefer embedded subs (always correctly labeled)
    use_embedded_subs=True,
    # Listen where the other admin UIs do (network.admin_bind)
    ip=bind,
)

# Only rewrite (and restart Bazarr) when something changed
if cfg == before:
    print("unchanged")
else:
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    print("changed")
PYEOF
); then
      ok "Sonarr + Radarr configured"
      if [ "$bazarr_result" = changed ]; then
        svc_restart bazarr >/dev/null 2>&1 && ok "Bazarr restarted with the new settings" || true
        wait_for "Bazarr" "$BAZARR_URL"
      fi
    else
      warn "Could not update Bazarr config"
    fi
  else
    warn "Bazarr config file not found"
  fi

  # Configure Bazarr language profiles via API (form-data POST to settings endpoint)
  # Run BEFORE auth setup since the settings API may rewrite config.yaml
  if [ -n "$BAZARR_CONFIG" ] && [ -n "$SUBTITLE_LANGS" ]; then
    BAZARR_API_KEY_VAL=$(sed -n '/^auth:/,/^[^ ]/{s/^  apikey: *//p;}' "$BAZARR_CONFIG" 2>/dev/null | head -1)
    if [ -n "$BAZARR_API_KEY_VAL" ]; then
      wait_for "Bazarr" "$BAZARR_URL"
      EXISTING_PROFILES=$(curl -sf "$BAZARR_URL/api/system/languages/profiles?apikey=$BAZARR_API_KEY_VAL" 2>/dev/null || echo "[]")
      # The "Default" profile carries subtitles.languages: create it, or
      # update it when the list changed. Other profiles are kept as they are.
      local want_langs profiles_json
      want_langs=$(jq -Rc 'split(",") | map(select(. != ""))' <<< "$SUBTITLE_LANGS")
      # subtitles.want = "first": done once any listed language is there
      # (cutoff 65535 = "any"), searched in list order; "all": every language
      local cutoff=null
      [ "$(cfg '.subtitles.want // "first"')" = first ] && cutoff=65535
      profiles_json=$(jq -c --argjson langs "$want_langs" --argjson cutoff "$cutoff" '
        ($langs | to_entries | map({id: .key, language: .value, hi: "False", forced: "False", audio_exclude: "False", audio_only_include: "False"})) as $items
        # Bazarr compares these flags as the strings "True"/"False"; booleans
        # (written by older versions of this script) never match the cutoff
        | if any(.[]; .name == "Default") then
            if ([.[] | select(.name == "Default") | .items][0] == $items
                and ([.[] | select(.name == "Default") | .cutoff][0] == $cutoff)) then empty
            else map(if .name == "Default" then .items = $items | .cutoff = $cutoff else . end) end
          else
            . + [{profileId: ((map(.profileId) | max // 0) + 1), name: "Default", cutoff: $cutoff,
                  items: $items, mustContain: [], mustNotContain: [], originalFormat: null}]
          end' <<< "$EXISTING_PROFILES" 2>/dev/null || true)
      if [ -z "$profiles_json" ]; then
        ok "Language profile: Default ($(echo "$SUBTITLE_LANGS" | tr ',' ' '); $(cfg '.subtitles.want // "first"'))"
      else
        local default_id lang
        default_id=$(jq -r '.[] | select(.name == "Default") | .profileId' <<< "$profiles_json")
        # Every language a profile uses has to be enabled
        LANG_ENABLED_ARGS=()
        while IFS= read -r lang; do
          [ -n "$lang" ] && LANG_ENABLED_ARGS+=(-d "languages-enabled=$lang")
        done < <(jq -r '[.[].items[].language] | unique | .[]' <<< "$profiles_json")
        api_retry curl -sf -X POST "$BAZARR_URL/api/system/settings?apikey=$BAZARR_API_KEY_VAL" \
          "${LANG_ENABLED_ARGS[@]}" \
          --data-urlencode "languages-profiles=$profiles_json" \
          -d "settings-general-serie_default_profile=$default_id" \
          -d "settings-general-movie_default_profile=$default_id" >/dev/null 2>&1 && \
          ok "Language profile: Default ($(echo "$SUBTITLE_LANGS" | tr ',' ' '), updated)" || warn "Could not set the language profile"
        # Bazarr recomputes what's missing only when it re-indexes
        local task
        for task in series_full_scan_subtitles movies_full_scan_subtitles; do
          curl -sf -o /dev/null -X POST "$BAZARR_URL/api/system/tasks?apikey=$BAZARR_API_KEY_VAL" --data-urlencode "taskid=$task" 2>/dev/null || true
        done
      fi
    fi
  fi

  # Bazarr auth — set via config file and restart (must run after settings API to avoid being overwritten)
  if [ -n "$BAZARR_CONFIG" ]; then
    # Bazarr accepts only "form" or "basic" (anything else, like the "forms"
    # an older version of this script wrote, is reset to null = no login)
    # and stores the password as an MD5 hash
    local bazarr_auth
    bazarr_auth=$(python3 - "$BAZARR_CONFIG" "$JELLYFIN_USER" "$JELLYFIN_PASS" << 'PYEOF'
import hashlib, sys, yaml
path, user, password = sys.argv[1:4]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
auth = cfg.setdefault("auth", {})
want = {"type": "form", "username": user, "password": hashlib.md5(password.encode()).hexdigest()}
if all(auth.get(k) == v for k, v in want.items()):
    print("unchanged")
else:
    auth.update(want)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    print("updated")
PYEOF
) || bazarr_auth="failed"
    if [ "$bazarr_auth" = "updated" ]; then
      svc_restart bazarr >/dev/null 2>&1 || true
      wait_for "Bazarr" "$BAZARR_URL"
    elif [ "$bazarr_auth" = "failed" ]; then
      warn "Could not set the Bazarr login"
    fi
    if [ "$bazarr_auth" != failed ]; then
      if bazarr_login_works "$JELLYFIN_USER" "$JELLYFIN_PASS"; then
        creds_match bazarr "$JELLYFIN_USER" "$JELLYFIN_PASS" || creds_set bazarr "$JELLYFIN_USER" "$JELLYFIN_PASS"
        ok "Bazarr login: $JELLYFIN_USER"
      else
        warn "Bazarr's login doesn't work yet (retried next run)"
      fi
    fi
  fi
}

# Bazarr's login API: 204 when the login works
bazarr_login_works() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$BAZARR_URL/api/system/account?action=login" \
    --data-urlencode "username=$1" --data-urlencode "password=$2" 2>/dev/null)" = "204" ]
}
