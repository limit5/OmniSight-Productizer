DESCRIPTION = "OmniSight camera reference image with UVC-XU dispatcher + v4l-utils"
LICENSE = "MIT"

IMAGE_INSTALL:append = " uvcvideo-xu-dispatcher v4l-utils kernel-modules openssh-sftp-server udev"
IMAGE_FEATURES:append = " ssh-server-openssh debug-tweaks"
IMAGE_LINGUAS = "en-us"

IMAGE_INSTALL:append:rk3588 = " kernel-modules"

inherit core-image
require recipes-core/images/core-image-minimal.bb
