# UVC-XU Userspace Dispatch Spike

**Ticket:** OP-1931  
**Date:** 2026-06-02  
**Scope:** Verify whether the Phase 0 P0.B userspace daemon pivot can rely on upstream
`UVCIOC_CTRL_MAP` for the FT-C600 vendor extension-unit GUID before P0.B.1 invests in a daemon skeleton.

## Summary

This runner could verify the Linux UAPI side of the hypothesis but could not execute the
FT-C600 ioctl path because the host exposed no V4L2 video node.

- `/usr/include/linux/uvcvideo.h` provides `struct uvc_xu_control_mapping` and `UVCIOC_CTRL_MAP`.
- `/dev/video*` was absent on this host.
- `/sys/class/video4linux` was absent on this host.
- `lsusb` was not installed, so USB bus enumeration could not be used as a fallback.
- The GET-only C harness below passed `gcc -Wall -Wextra -Werror -fsyntax-only`.

No production daemon code was written. No `UVCIOC_CTRL_QUERY` SET command was issued.

## Probe Harness

The intended host probe is deliberately small and only maps the control. It does not issue an XU SET
request or change FT-C600 state.

```c
#include <errno.h>
#include <fcntl.h>
#include <linux/uvcvideo.h>
#include <linux/videodev2.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    const char *node = argc > 1 ? argv[1] : "/dev/video0";
    struct uvc_xu_control_mapping mapping = {
        .id = 1,
        .entity = { 0x96, 0x9e, 0x20, 0x20, 0xf1, 0x90, 0x40, 0xa5,
                    0x90, 0x75, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x05 },
        .selector = 1,
        .size = 1,
        .offset = 0,
        .v4l2_type = V4L2_CTRL_TYPE_BOOLEAN,
        .data_type = UVC_CTRL_DATA_TYPE_BOOLEAN,
    };
    int fd;
    int rc;

    snprintf((char *)mapping.name, sizeof(mapping.name), "FILL_LIGHT");
    fd = open(node, O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return errno;
    rc = ioctl(fd, UVCIOC_CTRL_MAP, &mapping);
    if (rc < 0)
        rc = errno;
    close(fd);
    return rc;
}
```

Notes:

- GUID `{20209E96-90F1-A540-9075-F93D7ABA9D05}` is encoded in UVC little-endian entity-byte
  order as `96 9e 20 20 f1 90 40 a5 90 75 f9 3d 7a ba 9d 05`.
- `selector = 1`, `id = 1`, and `name = "FILL_LIGHT"` match the ticket's example control entry.
- The harness returns `errno` for both `open(2)` and `ioctl(2)` failures so a hardware run can record
  the exact kernel response.

## Evidence

| Check | Result |
| --- | --- |
| UVC UAPI has mapping struct | Verified: `/usr/include/linux/uvcvideo.h:46-61` |
| UVC UAPI has `UVCIOC_CTRL_MAP` | Verified: `/usr/include/linux/uvcvideo.h:72` |
| C harness syntax | Verified: `gcc -Wall -Wextra -Werror -fsyntax-only -x c -` exited `0` |
| FT-C600 V4L2 node | Blocked: `ls -l /dev/video*` returned `No such file or directory` |
| V4L2 sysfs class | Blocked: `find /sys/class/video4linux ...` returned `No such file or directory` |
| USB enumeration fallback | Blocked: `lsusb` returned `command not found` |

## Runtime Observations

- `UVCIOC_CTRL_MAP` return code: not observed, because no FT-C600 `/dev/videoN` node was present.
- `errno`: not observed for the ioctl path; `open("/dev/video0")` would fail with missing node in this
  environment.
- Latency: not measurable in this run. The required `<= 1 ms` control-plane RTT remains unverified.
- Auth/permissions: not reached. No conclusion can be drawn about `CAP_SYS_RAWIO`, group membership, or
  read/write access on a real FT-C600 node.

## Conclusion

**RED for P0.B.1 readiness in this run.**

The upstream userspace API exists, so the design pivot is plausible at the UAPI level. However, the actual
acceptance question is whether `uvcvideo` accepts `UVCIOC_CTRL_MAP` for the FT-C600 XU GUID and whether the
round trip is `<= 1 ms`. That could not be verified on this host because the FT-C600 did not enumerate as a
V4L2 device. P0.B.1 should not treat this spike as a green hardware validation until the harness is rerun on
a Linux host where the FT-C600 appears under `/dev/videoN`.

Recommended next operator action: attach the FT-C600 to a Linux host with `uvcvideo` loaded, confirm the
device node via `/sys/class/video4linux`, run the harness against that node, and record `(rc, errno, RTT)` in
the follow-up ticket or JIRA comment before starting daemon implementation.
