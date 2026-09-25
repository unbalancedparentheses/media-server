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
# v2.7.5 (the last release that can) creates VectorChord, reindexes, and
# drops "vectors". Called from start_stack before the rest of the stack
# starts. Re-running while a migration is still going just keeps waiting.
IMMICH_MIGRATION_VERSION="v2.7.5"
immich_has_pgvectors() {
  [ "$(docker exec immich-postgres psql -U postgres -d immich -tAc \
    "SELECT 1 FROM pg_extension WHERE extname = 'vectors'" 2>/dev/null || true)" = "1" ]
}
migrate_immich_vectors() {
  local dump override current i=0

  # Only Postgres; on a fresh install this just initialises an empty database
  dc up -d --wait immich-postgres >/dev/null
  immich_has_pgvectors || return 0

  info "Migrating Immich database from pgvecto.rs to VectorChord..."
  current=$(docker inspect -f '{{.Config.Image}}' immich 2>/dev/null || true)
  if [ "${current##*:}" != "$IMMICH_MIGRATION_VERSION" ]; then
    mkdir -p "$BACKUP_DIR"
    dump="$BACKUP_DIR/immich-pre-vectorchord_$(date +%Y%m%d_%H%M%S).sql.gz"
    (umask 077 && docker exec immich-postgres pg_dumpall --clean --if-exists -U postgres | gzip > "$dump")
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
  else
    ok "Immich $IMMICH_MIGRATION_VERSION is already running the migration"
  fi

  printf "   Waiting for the migration (can take a while on large libraries)..."
  while immich_has_pgvectors; do
    i=$((i + 10))
    if [ "$i" -ge 1800 ]; then
      echo ""
      err "Immich is still migrating after 30 minutes (see 'docker logs -f immich'). Re-run setup to keep waiting; it won't interrupt the migration."
    fi
    sleep 10
  done
  echo " done"
  ok "Immich database migrated to VectorChord (Immich $(docker inspect -f '{{.Config.Image}}' immich 2>/dev/null | sed 's/.*://') → v3 next)"
}
