Name: photon-commander
Version: %{photon_version}
Release: %{photon_release}%{?dist}
Summary: Observatory control and imaging automation for smart telescopes
License: GPL-3.0-only
URL: https://photoncommander.com
BuildArch: %{photon_arch}
Requires: systemd
Requires: xdg-utils

%global debug_package %{nil}

%description
Photon Commander provides planning, capture, calibration, live stacking,
streaming, scheduling, and safety automation from a browser-based interface.

%prep

%build

%install
mkdir -p %{buildroot}
cp -a "%{photon_stage}/." "%{buildroot}/"

%pre
getent group photon-commander >/dev/null || groupadd --system photon-commander
getent passwd photon-commander >/dev/null || useradd --system \
    --gid photon-commander --home-dir /var/lib/photon-commander \
    --no-create-home --shell /sbin/nologin photon-commander

%post
install -d -o photon-commander -g photon-commander -m 0770 /var/lib/photon-commander
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable photon-commander.service >/dev/null 2>&1 || true
systemctl try-restart photon-commander.service >/dev/null 2>&1 \
    || systemctl start photon-commander.service >/dev/null 2>&1 || true

%preun
if [ "$1" -eq 0 ]; then
    systemctl stop photon-commander.service >/dev/null 2>&1 || true
    systemctl disable photon-commander.service >/dev/null 2>&1 || true
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || true

%files
%dir /opt/photon-commander
/opt/photon-commander/app
/opt/photon-commander/web
%config(noreplace) /etc/photon-commander/environment
/usr/lib/systemd/system/photon-commander.service
/usr/share/applications/photon-commander.desktop
/usr/share/icons/hicolor/192x192/apps/photon-commander.png
/usr/share/doc/photon-commander

%changelog
* Wed Sep 23 2026 Photon Commander <maintainer@example.org> - %{photon_version}-%{photon_release}
- Native x86_64 Linux package
