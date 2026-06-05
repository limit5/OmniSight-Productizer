SUMMARY = "OmniSight UVC extension-unit userspace dispatcher"
DESCRIPTION = "Builds the uvcvideo-xu-dispatcher userspace daemon from the P0.B source tree."
LICENSE = "CLOSED"

OMNISIGHT_UVC_XU_DISPATCHER_EXTERNALSRC ?= "${THISDIR}/../../../../src/embedded/uvc-xu-dispatcher"

SRC_URI = ""

S = "${EXTERNALSRC}"
EXTERNALSRC = "${OMNISIGHT_UVC_XU_DISPATCHER_EXTERNALSRC}"
EXTERNALSRC_BUILD = "${WORKDIR}/build"

DEPENDS = "cmake-native"

inherit cmake externalsrc
