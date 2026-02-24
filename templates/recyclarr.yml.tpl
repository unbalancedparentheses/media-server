sonarr:
  main:
    base_url: {{SONARR_INTERNAL}}
    api_key: {{SONARR_KEY}}
    replace_existing_custom_formats: true
    quality_definition:
      type: series
    quality_profiles:
      - name: {{SONARR_PROFILE}}
        reset_unmatched_scores:
          enabled: true
    custom_formats:
      - trash_ids:
          - 32b367365729d530ca1c124a0b180c64
          - 82d40da2bc6923f41e14394075dd4b03
          - e1a997ddb54e3ecbfe06341ad323c458
          - 06d66ab109d4d2eddb2794d21526d140
        assign_scores_to:
          - name: {{SONARR_PROFILE}}
  anime:
    base_url: {{SONARR_ANIME_INTERNAL}}
    api_key: {{ANIME_KEY}}
    quality_definition:
      type: anime
    quality_profiles:
      - name: {{SONARR_ANIME_PROFILE}}
        reset_unmatched_scores:
          enabled: true
radarr:
  main:
    base_url: {{RADARR_INTERNAL}}
    api_key: {{RADARR_KEY}}
    replace_existing_custom_formats: true
    quality_definition:
      type: movie
    quality_profiles:
      - name: {{RADARR_PROFILE}}
        reset_unmatched_scores:
          enabled: true
    custom_formats:
      - trash_ids:
          - ed38b889b31be83fda192888e2286d83
          - 90cedc1fea7ea5d11298bebd3d1d3223
          - b8cd450cbfa689c0259a01d9e29ba3d6
        assign_scores_to:
          - name: {{RADARR_PROFILE}}
