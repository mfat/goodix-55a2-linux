%undefine _debugsource_packages

Name:           libfprint-tod-goodix-55a2
Version:        0.1.1
Release:        1%{?dist}
Summary:        Goodix 27c6:55a2 fingerprint reader support for fprintd

License:        MIT
URL:            https://github.com/mfat/goodix-55a2-linux
Source0:        %{url}/archive/refs/tags/v%{version}/goodix-55a2-linux-%{version}.tar.gz

BuildRequires:  gcc
BuildRequires:  make
BuildRequires:  pkgconfig(libfprint-2-tod-1)
BuildRequires:  pkgconfig(gio-unix-2.0)
BuildRequires:  systemd-rpm-macros

# libfprint-tod comes from the grahamwhiteuk/libfprint-tod COPR
Requires:       libfprint-tod
Requires:       fprintd
Requires:       python3
Requires:       python3-pyusb
Requires:       python3-numpy
Requires:       python3-opencv
%{?systemd_requires}

%description
libfprint TOD driver and userspace bridge for the Goodix 27c6:55a2 USB
fingerprint reader. The bridge drives the sensor over USB and TLS-PSK and
matches touches with SIFT + RANSAC; fprintd uses it through the TOD module.

On first use the bridge overwrites the sensor's TLS pairing key (PSK).

%prep
%autosetup -n goodix-55a2-linux-%{version}

%build
%make_build LIBEXECDIR=%{_libexecdir}

%install
%make_install LIBEXECDIR=%{_libexecdir} UNITDIR=%{_unitdir} TMPFILESDIR=%{_tmpfilesdir}

%post
%systemd_post goodix55a2-bridge.service
%tmpfiles_create goodix55a2.conf
if [ $1 -eq 1 ]; then
    systemctl enable --now goodix55a2-bridge.service >/dev/null 2>&1 || :
fi
systemctl try-restart fprintd.service >/dev/null 2>&1 || :

%preun
%systemd_preun goodix55a2-bridge.service

%postun
%systemd_postun_with_restart goodix55a2-bridge.service

%files
%license LICENSE
%doc README.md
%{_libdir}/libfprint-2/tod-1/libfprint-tod-goodix55a2.so
%{_libexecdir}/goodix55a2-bridge
%{_unitdir}/goodix55a2-bridge.service
%{_tmpfilesdir}/goodix55a2.conf

%changelog
* Sun Oct 11 2026 mFat <newmfat@gmail.com> - 0.1.1-1
- Patch the chip config with per-sensor OTP calibration like the Windows driver

* Sun Oct 11 2026 mFat <newmfat@gmail.com> - 0.1.0-1
- Initial package
