#!/usr/bin/env bash
# Navidrome, Immich, Janitorr and the landing-page API proxy.

configure_navidrome() {
  info "Configuring Navidrome..."

  ND_CHECK=$(curl -s -o /dev/null -w "%{http_code}" "$NAVIDROME_URL/auth/createAdmin" 2>/dev/null)
  if [ "$ND_CHECK" = "200" ]; then
    ND_RESULT=$(curl -s -X POST "$NAVIDROME_URL/auth/createAdmin" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg u "$JELLYFIN_USER" --arg p "$JELLYFIN_PASS" '{username:$u,password:$p}')" 2>/dev/null || true)
    if echo "$ND_RESULT" | jq -e '.id' >/dev/null 2>&1; then
      ok "Admin user created: $JELLYFIN_USER"
    else
      warn "Could not create admin user"
    fi
  else
    ok "Already configured"
  fi
}

configure_immich() {
  info "Configuring Immich..."

  IM_CHECK=$(curl -s "$IMMICH_URL/api/server/ping" 2>/dev/null || true)
  if [ -n "$IM_CHECK" ]; then
    IM_RESULT=$(curl -s -X POST "$IMMICH_URL/api/auth/admin-sign-up" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg p "$JELLYFIN_PASS" --arg n "$JELLYFIN_USER" '{email:"admin@media.local",password:$p,name:$n}')" 2>/dev/null || true)
    if echo "$IM_RESULT" | jq -e '.id' >/dev/null 2>&1; then
      ok "Admin user created: $JELLYFIN_USER (admin@media.local)"
    else
      ok "Already configured"
    fi
  else
    warn "Immich not responding"
  fi
}

configure_janitorr() {
  info "Configuring Janitorr..."

  JANITORR_CONFIG="$CONFIG_DIR/janitorr/application.yml"
  if [ ! -f "$JANITORR_CONFIG" ]; then
    mkdir -p "$CONFIG_DIR/janitorr/logs"
    mkdir -p "$MEDIA_DIR/leaving-soon"
    write_janitorr_config_from_template
    ok "Config written (dry-run enabled — edit application.yml to activate)"
    docker restart janitorr >/dev/null 2>&1 || true
  else
    ok "Already configured"
  fi
}

write_api_proxy() {
  info "Generating API proxy config for landing page..."

  # Re-read Jellyseerr key (may have been created during Jellyseerr setup above)
  [ -z "$JELLYSEERR_KEY" ] && [ -f "$CONFIG_DIR/jellyseerr/settings.json" ] && \
    JELLYSEERR_KEY=$(jq -r '.main.apiKey // empty' "$CONFIG_DIR/jellyseerr/settings.json" 2>/dev/null)

  API_PROXY="$CONFIG_DIR/nginx/api-proxy.conf"
  write_api_proxy_from_template

  ok "api-proxy.conf written"

  # Reload nginx to pick up the new proxy config
  if docker exec media-nginx nginx -t >/dev/null 2>&1; then
    docker exec media-nginx nginx -s reload >/dev/null 2>&1 && \
      ok "nginx reloaded" || warn "Could not reload nginx (will apply on next restart)"
  else
    warn "nginx config test failed — skipping reload"
  fi
}

# Immich v3 no longer supports pgvecto.rs. A database created with the old
# tensorchord/pgvecto-rs image still has the "vectors" extension; Immich
# v2.7.5 (the last release that can) moves it to VectorChord and drops it.
# Runs right after the stack starts, before anything waits on Immich.
IMMICH_MIGRATION_VERSION="v2.7.5"
migrate_immich_vectors() {
  local has_vectors dump override i=0
  has_vectors=$(docker exec immich-postgres psql -U postgres -d immich -tAc \
    "SELECT 1 FROM pg_extension WHERE extname = 'vectors'" 2>/dev/null || true)
  [ "$has_vectors" = "1" ] || return 0

  info "Migrating Immich database from pgvecto.rs to VectorChord..."
  mkdir -p "$BACKUP_DIR"
  dump="$BACKUP_DIR/immich-pre-vectorchord_$(date +%Y%m%d_%H%M%S).sql.gz"
  docker exec immich-postgres pg_dumpall --clean --if-exists -U postgres | gzip > "$dump"
  chmod 600 "$dump"
  ok "Database dump: $dump"

  override="$TMPDIR_SETUP/immich-migrate.yml"
  cat > "$override" << YML
services:
  immich:
    image: ghcr.io/immich-app/immich-server:$IMMICH_MIGRATION_VERSION
  immich-machine-learning:
    image: ghcr.io/immich-app/immich-machine-learning:$IMMICH_MIGRATION_VERSION
YML
  local files=(-f "$COMPOSE_FILE")
  [ -f "$OVERRIDE_FILE" ] && files+=(-f "$OVERRIDE_FILE")
  docker compose "${files[@]}" -f "$override" up -d immich immich-machine-learning

  printf "   Waiting for Immich %s to migrate (can take several minutes)..." "$IMMICH_MIGRATION_VERSION"
  until curl -sf --connect-timeout 2 http://localhost:2283/api/server/ping >/dev/null 2>&1; do
    i=$((i + 5))
    [ "$i" -ge 1800 ] && break
    sleep 5
  done
  echo ""

  has_vectors=$(docker exec immich-postgres psql -U postgres -d immich -tAc \
    "SELECT 1 FROM pg_extension WHERE extname = 'vectors'" 2>/dev/null || true)
  if [ "$has_vectors" = "1" ]; then
    err "Immich migration did not finish; check 'docker logs immich' and re-run setup (dump: $dump)"
  fi
  ok "Immich database migrated to VectorChord"

  dc up -d immich immich-machine-learning
  ok "Immich back on $(docker inspect -f '{{.Config.Image}}' immich 2>/dev/null | sed 's/.*://')"
}
