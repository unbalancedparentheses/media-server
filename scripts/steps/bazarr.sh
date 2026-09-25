#!/usr/bin/env bash

configure_bazarr() {
  info "Configuring Bazarr..."

  BAZARR_CONFIG=""
  for f in "$CONFIG_DIR/bazarr/config/config/config.yaml" "$CONFIG_DIR/bazarr/config/config.yaml"; do
    [ -f "$f" ] && BAZARR_CONFIG="$f" && break
  done

  if [ -n "$BAZARR_CONFIG" ]; then
    ok "Config: $BAZARR_CONFIG"

    # Use python3 to do targeted updates (preserves all existing config)
    if python3 - "$BAZARR_CONFIG" "$SONARR_KEY" "$RADARR_KEY" "$SUBTITLE_PROVIDERS" "$SUBTITLE_LANGS" "${ADMIN_BIND:-0.0.0.0}" << 'PYEOF'
import sys
import json

config_path = sys.argv[1]
sonarr_key = sys.argv[2]
radarr_key = sys.argv[3]
subtitle_providers = sys.argv[4] if len(sys.argv) > 4 else ''
subtitle_langs = sys.argv[5] if len(sys.argv) > 5 else ''
bind_address = sys.argv[6] if len(sys.argv) > 6 else '0.0.0.0'

with open(config_path, 'r') as f:
    lines = f.readlines()

# Parse into sections: { section_name: { key: line_index } }
sections = {}
current_section = None
for i, line in enumerate(lines):
    stripped = line.rstrip('\n')
    if stripped and not stripped[0].isspace() and stripped.endswith(':') and stripped != '---':
        current_section = stripped[:-1]
        sections[current_section] = {}
    elif current_section and stripped.startswith('  ') and ':' in stripped:
        key = stripped.split(':')[0].strip()
        sections[current_section][key] = i

def set_value(section, key, value):
    """Update an existing key or append to section."""
    if isinstance(value, bool):
        val_str = 'true' if value else 'false'
    elif isinstance(value, str):
        val_str = f"'{value}'" if value else "''"
    else:
        val_str = str(value)

    if section in sections and key in sections[section]:
        idx = sections[section][key]
        lines[idx] = f'  {key}: {val_str}\n'
    elif section in sections:
        # Find end of section to append
        sec_keys = sections[section]
        if sec_keys:
            last_idx = max(sec_keys.values())
        else:
            # Find section header line
            for j, l in enumerate(lines):
                if l.rstrip('\n') == f'{section}:':
                    last_idx = j
                    break
        lines.insert(last_idx + 1, f'  {key}: {val_str}\n')
        # Rebuild index for this section
        sections[section][key] = last_idx + 1

if sonarr_key:
    set_value('sonarr', 'ip', 'localhost')
    set_value('sonarr', 'port', 8989)
    set_value('sonarr', 'base_url', '/')
    set_value('sonarr', 'apikey', sonarr_key)
    set_value('sonarr', 'ssl', False)
    set_value('general', 'use_sonarr', True)

if radarr_key:
    set_value('radarr', 'ip', 'localhost')
    set_value('radarr', 'port', 7878)
    set_value('radarr', 'base_url', '/')
    set_value('radarr', 'apikey', radarr_key)
    set_value('radarr', 'ssl', False)
    set_value('general', 'use_radarr', True)

# Configure subtitle providers
if subtitle_providers:
    providers_list = json.dumps(subtitle_providers.split(','))
    set_value('general', 'enabled_providers', providers_list)

# Enable default language profiles for series and movies
if subtitle_langs:
    set_value('general', 'serie_default_enabled', True)
    set_value('general', 'movie_default_enabled', True)

# Subtitle quality: minimum score filters out mislabeled subs
set_value('general', 'minimum_score', 70)
set_value('general', 'minimum_score_movie', 70)

# Auto-upgrade subs when a higher-score match appears
set_value('general', 'upgrade_subs', True)
set_value('general', 'upgrade_frequency', 12)
set_value('general', 'days_to_upgrade_subs', 7)

# Prefer embedded subs (always correctly labeled)
set_value('general', 'use_embedded_subs', True)

# Listen where the other admin UIs do (network.admin_bind)
set_value('general', 'ip', bind_address)

with open(config_path, 'w') as f:
    f.writelines(lines)

print("OK")
PYEOF
    then
      ok "Sonarr + Radarr configured"
      svc_restart bazarr >/dev/null 2>&1 && ok "Bazarr restarted" || true
      wait_for "Bazarr" "$BAZARR_URL"
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
      PROFILE_COUNT=$(echo "$EXISTING_PROFILES" | jq 'length' 2>/dev/null || echo "0")

      if [ "$PROFILE_COUNT" = "0" ] || [ -z "$PROFILE_COUNT" ]; then
        # Build language items for the profile
        LANG_ITEMS="[]"
        IDX=0
        LANG_ENABLED_ARGS=()
        IFS=',' read -ra LANGS <<< "$SUBTITLE_LANGS"
        for lang in "${LANGS[@]}"; do
          LANG_ITEMS=$(echo "$LANG_ITEMS" | jq --arg code "$lang" --argjson idx "$IDX" \
            '. + [{"id": $idx, "language": $code, "hi": false, "forced": false}]')
          LANG_ENABLED_ARGS+=(-d "languages-enabled=$lang")
          IDX=$((IDX + 1))
        done

        PROFILE_JSON=$(jq -n --argjson items "$LANG_ITEMS" \
          '[{"profileId":1,"name":"Default","cutoff":null,"items":$items,"mustContain":"","mustNotContain":"","originalFormat":null}]')

        api_retry curl -sf -X POST "$BAZARR_URL/api/system/settings?apikey=$BAZARR_API_KEY_VAL" \
          "${LANG_ENABLED_ARGS[@]}" \
          --data-urlencode "languages-profiles=$PROFILE_JSON" \
          -d "settings-general-serie_default_profile=1" \
          -d "settings-general-movie_default_profile=1" >/dev/null 2>&1 && \
          ok "Language profile: Default ($(echo "$SUBTITLE_LANGS" | tr ',' ' '))" || warn "Could not create language profile"
      else
        ok "Language profiles already configured ($PROFILE_COUNT)"
      fi
    fi
  fi

  # Bazarr auth — set via config file and restart (must run after settings API to avoid being overwritten)
  if [ -n "$BAZARR_CONFIG" ]; then
    BAZARR_AUTH_TYPE=$(sed -n '/^auth:/,/^[^ ]/{s/^  type: *//p;}' "$BAZARR_CONFIG" 2>/dev/null | head -1)
    if [ -z "$BAZARR_AUTH_TYPE" ] || [ "$BAZARR_AUTH_TYPE" = "null" ] || [ "$BAZARR_AUTH_TYPE" = "''" ]; then
      if python3 - "$BAZARR_CONFIG" "$JELLYFIN_USER" "$JELLYFIN_PASS" << 'PYEOF'
import sys
config_path, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
with open(config_path, 'r') as f:
    lines = f.readlines()
# Find or create auth section
auth_idx = None
for i, line in enumerate(lines):
    if line.strip() == 'auth:':
        auth_idx = i
        break
if auth_idx is None:
    lines.append('\nauth:\n')
    auth_idx = len(lines) - 1
# Remove existing auth keys and rewrite
new_lines = []
in_auth = False
for i, line in enumerate(lines):
    if line.strip() == 'auth:':
        in_auth = True
        new_lines.append(line)
        new_lines.append("  type: 'forms'\n")
        new_lines.append(f"  username: '{user}'\n")
        new_lines.append(f"  password: '{password}'\n")
        continue
    if in_auth:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not line[0].isspace():
            in_auth = False
            new_lines.append(line)
        elif stripped.split(':')[0].strip() in ('type', 'username', 'password'):
            continue
        else:
            new_lines.append(line)
    else:
        new_lines.append(line)
with open(config_path, 'w') as f:
    f.writelines(new_lines)
print("OK")
PYEOF
      then
        ok "Bazarr auth set: $JELLYFIN_USER"
        svc_restart bazarr >/dev/null 2>&1 || true
        wait_for "Bazarr" "$BAZARR_URL"
      else
        warn "Could not set Bazarr auth"
      fi
    else
      ok "Bazarr auth already configured"
    fi
  fi
}
