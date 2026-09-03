#!/usr/bin/env bash
# lib_scron.sh — install / remove ONE managed scrontab block, by name.  Sourced by enable_probe.sh
# and disable_probe.sh.  (enable_nightly.sh / disable_nightly.sh predate this file and carry the
# same logic inline; they can switch to it later.)  A managed block is
#     # <name>-BEGIN
#     #SCRON <slurm options>
#     <cron schedule> <command>
#     # <name>-END
# The sed range touches only the named block, so every other entry passes through untouched.
# Note: `scrontab -` re-registers every entry, so every scron job on the account (the nightly
# included) gets a new job id and hence a new log filename.  Harmless.

scron_block_install() {   # $1=name  $2=slurm options  $3=cron schedule  $4=command
  local B="# $1-BEGIN" E="# $1-END" BLOCK CUR
  BLOCK="$(printf '%s\n#SCRON %s\n%s %s\n%s' "$B" "$2" "$3" "$4" "$E")"
  CUR="$(scrontab -l 2>/dev/null | sed "/$B/,/$E/d")" || CUR=""
  { [ -n "$CUR" ] && printf '%s\n' "$CUR"; printf '%s\n' "$BLOCK"; } | scrontab -
}

scron_block_remove() {    # $1=name ; returns 1 when no such block existed
  local B="# $1-BEGIN" E="# $1-END" CUR
  CUR="$(scrontab -l 2>/dev/null)" || CUR=""
  printf '%s\n' "$CUR" | grep -qF "$B" || return 1
  printf '%s\n' "$CUR" | sed "/$B/,/$E/d" | scrontab -
}

scron_block_present() {   # $1=name
  scrontab -l 2>/dev/null | grep -qF "# $1-BEGIN"
}
