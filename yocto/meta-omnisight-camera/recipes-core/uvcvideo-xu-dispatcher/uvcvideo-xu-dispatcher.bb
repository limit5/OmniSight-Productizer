SUMMARY = "OmniSight UVC extension-unit userspace dispatcher"
DESCRIPTION = "Builds the uvcvideo-xu-dispatcher userspace daemon from the P0.B source tree."
LICENSE = "CLOSED"

SRC_URI = "file://uvcvideo-xu-dispatcher"

S = "${WORKDIR}/uvcvideo-xu-dispatcher"

DEPENDS = "cmake-native"

inherit cmake
