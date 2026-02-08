.PHONY: install switch check logs status clean

# First-time install (bootstrap nix-darwin)
install:
	@echo "Creating log directories..."
	@for svc in jellyfin sonarr sonarr-anime radarr prowlarr bazarr sabnzbd qbittorrent jellyseerr; do \
		mkdir -p ~/media/config/$$svc/logs; \
	done
	@echo "Building and switching nix-darwin configuration..."
	darwin-rebuild switch --flake ~/media-server

# Rebuild and switch (after first install)
switch:
	darwin-rebuild switch --flake ~/media-server

# Dry-run build to check for errors without applying
check:
	darwin-rebuild check --flake ~/media-server

# Show status of all media services
status:
	@echo "=== Media Services ==="
	@launchctl list | grep media || echo "No media services running"
	@echo ""
	@echo "=== Port Check ==="
	@for pair in "Jellyfin:8096" "Sonarr:8989" "Sonarr-Anime:8990" "Radarr:7878" "Prowlarr:9696" "Bazarr:6767" "SABnzbd:8080" "qBittorrent:8081" "Jellyseerr:5055"; do \
		name=$${pair%%:*}; port=$${pair##*:}; \
		if curl -s -o /dev/null -w "" --connect-timeout 1 http://localhost:$$port; then \
			echo "  ✓ $$name (port $$port)"; \
		else \
			echo "  ✗ $$name (port $$port)"; \
		fi; \
	done

# Tail logs for all services
logs:
	@tail -f ~/media/config/*/logs/*.log

# Tail logs for a specific service: make log SVC=sonarr
log:
	@tail -f ~/media/config/$(SVC)/logs/*.log

# Remove all generated configs (keeps media files)
clean:
	@echo "This will remove all service configs in ~/media/config/."
	@echo "Media files in ~/media/{movies,tv,anime,downloads} are NOT affected."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] && \
		rm -rf ~/media/config/*/logs/ || echo "Aborted."
