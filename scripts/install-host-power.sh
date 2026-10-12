#!/usr/bin/env bash
# Run once as administrator on the Pi; application service retains NoNewPrivileges.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run with sudo: sudo bash scripts/install-host-power.sh <service-user>'; exit 1; }
user=${1:-m}
[[ "$user" =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 1
id "$user" >/dev/null
install -d -m 0755 /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/49-photon-host-power.rules <<EOF
polkit.addRule(function(action, subject) {
  if (subject.user === "$user" &&
      (action.id === "org.freedesktop.login1.reboot" ||
       action.id === "org.freedesktop.login1.reboot-multiple-sessions" ||
       action.id === "org.freedesktop.login1.power-off" ||
       action.id === "org.freedesktop.login1.power-off-multiple-sessions")) {
    return polkit.Result.YES;
  }
});
EOF
chmod 0644 /etc/polkit-1/rules.d/49-photon-host-power.rules
chown root:root /etc/polkit-1/rules.d/49-photon-host-power.rules
echo 'Installed narrow Photon reboot and shutdown permissions.'
