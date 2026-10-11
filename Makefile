# Goodix 27c6:55a2 TOD driver + bridge.
#
#   make                          build the TOD module
#   make install DESTDIR=...      install everything (packagers)
#
# Paths can be overridden, e.g. LIBEXECDIR=/usr/lib/goodix55a2 on Arch.

VERSION     := $(shell cat VERSION)
PREFIX      ?= /usr
LIBEXECDIR  ?= $(PREFIX)/libexec
UNITDIR     ?= $(PREFIX)/lib/systemd/system
TMPFILESDIR ?= $(PREFIX)/lib/tmpfiles.d
PKG_CONFIG  ?= pkg-config
TODDIR      ?= $(shell $(PKG_CONFIG) --variable=tod_driversdir libfprint-2-tod-1)

# Override TOD_CFLAGS / TOD_LIBS when the -devel files are not installed.
TOD_CFLAGS  ?= $(shell $(PKG_CONFIG) --cflags libfprint-2-tod-1 gio-unix-2.0)
TOD_LIBS    ?= $(shell $(PKG_CONFIG) --libs libfprint-2-tod-1 libfprint-2 gio-unix-2.0)

CFLAGS      ?= -O2 -g
CFLAGS      += -fPIC -Wall -Wno-unused-parameter
LDFLAGS     += -shared -Wl,--no-undefined

MODULE  = tod/libfprint-tod-goodix55a2.so
SERVICE = systemd/goodix55a2-bridge.service

all: $(MODULE) $(SERVICE)

$(MODULE): tod/goodix55a2.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(TOD_CFLAGS) $(LDFLAGS) -o $@ $< $(TOD_LIBS)

$(SERVICE): $(SERVICE).in VERSION
	sed -e 's|@LIBEXECDIR@|$(LIBEXECDIR)|g' $< > $@

install: all
	@test -n "$(TODDIR)" || { echo "TODDIR unknown: install libfprint-tod development files or set TODDIR"; exit 1; }
	install -Dm644 $(MODULE) $(DESTDIR)$(TODDIR)/libfprint-tod-goodix55a2.so
	install -Dm755 bridge/goodix55a2-bridge.py $(DESTDIR)$(LIBEXECDIR)/goodix55a2-bridge
	install -Dm644 $(SERVICE) $(DESTDIR)$(UNITDIR)/goodix55a2-bridge.service
	install -Dm644 systemd/goodix55a2.tmpfiles $(DESTDIR)$(TMPFILESDIR)/goodix55a2.conf

uninstall:
	rm -f $(DESTDIR)$(TODDIR)/libfprint-tod-goodix55a2.so \
	      $(DESTDIR)$(LIBEXECDIR)/goodix55a2-bridge \
	      $(DESTDIR)$(UNITDIR)/goodix55a2-bridge.service \
	      $(DESTDIR)$(TMPFILESDIR)/goodix55a2.conf

dist:
	git archive --format=tar.gz --prefix=goodix-55a2-linux-$(VERSION)/ -o goodix-55a2-linux-$(VERSION).tar.gz HEAD

clean:
	rm -f $(MODULE) $(SERVICE)

.PHONY: all install uninstall dist clean
