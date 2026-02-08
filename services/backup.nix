{ pkgs, ... }:

let
  home = "/Users/claudiabottasera";
  configBase = "${home}/media/config";
  backupDir = "${home}/media/backups";

  # Backs up all service configs daily. Keeps 7 daily + 4 weekly snapshots.
  # Uses restic for deduplication and encryption.
  #
  # First-time setup:
  #   restic -r ~/media/backups init
  #   (choose a password — store it somewhere safe)
  #
  # To restore:
  #   restic -r ~/media/backups restore latest --target ~/media/config-restore
  backupScript = pkgs.writeShellScript "media-backup" ''
    export PATH="${pkgs.restic}/bin:$PATH"
    export RESTIC_REPOSITORY="${backupDir}"
    export RESTIC_PASSWORD_FILE="${configBase}/backup-password"

    # Skip if repo not initialized yet
    if [ ! -f "${configBase}/backup-password" ]; then
      echo "No backup password file at ${configBase}/backup-password"
      echo "Create it with: echo 'your-password' > ${configBase}/backup-password && chmod 600 ${configBase}/backup-password"
      echo "Then init the repo: RESTIC_PASSWORD_FILE=${configBase}/backup-password restic -r ${backupDir} init"
      exit 0
    fi

    if ! restic cat config >/dev/null 2>&1; then
      echo "Restic repo not initialized. Run: RESTIC_PASSWORD_FILE=${configBase}/backup-password restic -r ${backupDir} init"
      exit 0
    fi

    echo "Starting backup of media configs..."
    restic backup \
      --exclude "*/logs/*" \
      --exclude "*/tmp/*" \
      --exclude "*/cache/*" \
      --exclude "*/Cache/*" \
      --exclude "*/MediaCover/*" \
      ${configBase}

    echo "Pruning old snapshots..."
    restic forget \
      --keep-daily 7 \
      --keep-weekly 4 \
      --prune

    echo "Backup complete."
  '';

in
{
  # Runs daily at 4 AM (after recyclarr at 3 AM)
  launchd.user.agents.media-backup = {
    serviceConfig = {
      ProgramArguments = [ "${backupScript}" ];
      StartCalendarInterval = [{ Hour = 4; Minute = 0; }];
      WorkingDirectory = backupDir;
      StandardOutPath = "${configBase}/recyclarr/logs/backup-stdout.log";
      StandardErrorPath = "${configBase}/recyclarr/logs/backup-stderr.log";
      EnvironmentVariables.HOME = home;
    };
  };
}
