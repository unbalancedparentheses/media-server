#!/usr/bin/env bash
# Checks and fixes for every imported file (scripts/postimport.py, a
# launchd agent). It reads these settings each round, so a change needs no
# restart.

configure_postimport() {
  info "Configuring the checks after each download..."
  local file="$STATE_DIR/postimport/settings.json" settings current
  settings=$(jq -n \
    --argjson check "$(cfg_bool .library.check_downloads true)" \
    --argjson stereo "$(cfg_bool .library.stereo_audio true)" \
    --argjson ocr "$(cfg_bool .library.ocr_subtitles true)" \
    --argjson tries "$(cfg '.library.max_replacements // 3')" \
    --argjson dubs "$(cfg_bool .quality.anime_block_dubs true)" \
    --arg audio "$(cfg '.playback.audio_language // ""')" \
    --argjson subs "$(jq -c '.subtitles.languages // ["en"]' <<< "$CONFIG_JSON")" \
    --arg want "$(cfg '.subtitles.want // "first"')" \
    --argjson min "$(cfg '.disk.min_free_gb // 10')" \
    --argjson warn "$(cfg '.disk.warn_free_gb // 50')" \
    --arg movies "$MOVIES_DIR" --arg tv "$TV_DIR" --arg anime "$ANIME_DIR" '
    {check_downloads: $check, stereo_audio: $stereo, ocr_subtitles: $ocr, max_replacements: $tries,
     block_dubs: $dubs, audio_language: $audio, subtitle_languages: $subs, want: $want,
     min_free_gb: $min, warn_free_gb: $warn, library_dirs: [$movies, $tv, $anime]}')
  current=$(jq -S . "$file" 2>/dev/null || true)
  if [ "$current" != "$(jq -S . <<< "$settings")" ]; then
    write_atomic "$file" "$settings"
    ok "Settings written"
  fi
  ok "After each download: $(jq -r '[(if .check_downloads then "bad downloads replaced" else empty end),
    (if .stereo_audio then "stereo audio added" else empty end),
    (if .ocr_subtitles then "picture subtitles read into text" else empty end)]
    | if length > 0 then join(", ") else "nothing (all off in [library])" end' <<< "$settings")"
}
