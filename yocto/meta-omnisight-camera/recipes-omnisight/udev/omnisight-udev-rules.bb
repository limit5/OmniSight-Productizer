SUMMARY = "OmniSight bench USB udev rules"
DESCRIPTION = "Installs OmniSight bench udev rules for flashing and serial console access."
LICENSE = "CLOSED"

SRC_URI = "file://99-omnisight-bench.rules"

inherit allarch

do_install() {
	install -d ${D}${sysconfdir}/udev/rules.d
	install -m 0644 ${WORKDIR}/99-omnisight-bench.rules ${D}${sysconfdir}/udev/rules.d/
}

FILES:${PN} = "${sysconfdir}/udev/rules.d/99-omnisight-bench.rules"
