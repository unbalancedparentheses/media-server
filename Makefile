.PHONY: install switch check status logs log clean backup-init

FLAKE := ~/media-server

# First-time install
install: switch
	@echo ""
	@echo "=== Setup Complete ==="
	@echo "Next steps:"
	@echo "  1. Start each service's web UI and grab API keys"
	@echo "  2. Set up restic backups: make backup-init"
	@echo "  3. Edit recyclarr API keys in Sonarr/Radarr settings"
	@echo "  4. Open http://localhost to see the dashboard"

# Rebuild and activate
switch:
	sudo darwin-rebuild switch --flake $(FLAKE)

# Dry-run build
check:
	darwin-rebuild build --flake $(FLAKE)

# Show all media services and port status
status:
	@echo "=== launchd Services ==="
	@launchctl list | head -1
	@launchctl list | grep media || echo "  (none running)"
	@echo ""
	@echo "=== Port Check ==="
	@for pair in "Jellyfin:8096" "Sonarr:8989" "Sonarr-Anime:8990" "Radarr:7878" \
	             "Prowlarr:9696" "Bazarr:6767" "SABnzbd:8080" "qBittorrent:8081" \
	             "Nginx:80"; do \
		name=$${pair%%:*}; port=$${pair##*:}; \
		if curl -s -o /dev/null -w "" --connect-timeout 1 http://localhost:$$port; then \
			printf "  ✓ %-15s (port %s)\n" "$$name" "$$port"; \
		else \
			printf "  ✗ %-15s (port %s)\n" "$$name" "$$port"; \
		fi; \
	done

# Initialize restic backup repository
backup-init:
	@if [ ! -f ~/media/config/backup-password ]; then \
		read -sp "Choose a backup password: " pw && echo "$$pw" > ~/media/config/backup-password; \
		chmod 600 ~/media/config/backup-password; \
		echo ""; \
	fi
	RESTIC_PASSWORD_FILE=~/media/config/backup-password restic -r ~/media/backups init

# Tail all service logs
logs:
	@tail -f ~/media/config/*/logs/*.log

# Tail logs for a specific service: make log SVC=sonarr
log:
	@tail -f ~/media/config/$(SVC)/logs/*.log
