# ATK-DLRK3588 Type-C Controller Spike

**Ticket:** OP-1983
**Date:** 2026-06-03
**Scope:** Verify the likely USB Type-C port-controller silicon for the ATK-DLRK3588
reference EVK, map it to upstream Linux `drivers/usb/typec/` coverage, and record
whether C5.C.1 needs vendor driver porting before PD3.0 work starts.

## Summary

This spike could verify the public board-level Type-C topology and the upstream Linux
driver coverage, but could not prove the exact IC populated on the ATK-DLRK3588 bench
unit because this runner has no physical board, schematic image extraction, or vendor
BOM access.

- ALIENTEK's public ATK-DLRK3588B manual documents two USB 3.1 Type-C OTG connectors
  attached to the RK3588 Type-C interfaces; both support device/host mode and DP output.
- The strongest reference-design hypothesis remains `fusb302`: RK3588 reference
  material commonly uses FUSB302/HUSB311-class external CC/PD controllers, and the Case
  5 design doc already records `fusb302` as the assumed RK3588 path.
- Upstream Linux has first-class TCPM coverage for FUSB302 via
  `drivers/usb/typec/tcpm/fusb302.c`, `CONFIG_TYPEC_FUSB302`, and OF compatible
  `fcs,fusb302`.
- Upstream Linux also has a `wusb3801` Type-C controller driver, but that driver is a
  Type-C attach/orientation path, not a TCPM PD engine like FUSB302.
- No upstream mainline `drivers/usb/typec/` or `drivers/usb/typec/tcpm/` coverage was
  found for a named `HUSB311` or `FUSB304` driver in the inspected mainline tree.
- UCSI is present upstream as a separate `drivers/usb/typec/ucsi/` framework, but the
  ATK-DLRK3588 path should be treated as direct TCPM/typec exposure unless board firmware
  or an embedded controller is shown to present a UCSI PPM.

No driver code was written.

## Board Topology Findings

| Check | Result |
| --- | --- |
| ATK-DLRK3588B has USB-C OTG ports | Verified: ALIENTEK hardware manual lists two USB 3.1 Type-C OTG connectors with firmware flashing/device communication use and DP display support. |
| Ports attach to RK3588 Type-C interfaces | Verified: ALIENTEK section 3.17 says the two USB Type-C OTG connectors connect to RK3588's two Type-C interfaces. |
| J16 default role | Verified: ALIENTEK section 3.17 says factory software defaults J16 to system flashing and ADB debug. |
| Exact Type-C controller IC marking | Not verified: public text confirms connectors and topology, but does not name the CC/PD controller IC. |
| Physical inspection | Not exercised: no ATK-DLRK3588 board is available in this runner. |

Source references:

- ALIENTEK ATK-DLRK3588B board resources:
  `https://wiki.alientek.com/docs/Boards/Linux/DLRK3588/DLRK3588%20%E7%A1%AC%E4%BB%B6%E5%8F%82%E8%80%83%E6%89%8B%E5%86%8C/resource/1.1resource/`
- ALIENTEK ATK-DLRK3588B USB OTG Type-C section:
  `https://wiki.alientek.com/docs/Boards/Linux/DLRK3588/DLRK3588%20%E7%A1%AC%E4%BB%B6%E5%8F%82%E8%80%83%E6%89%8B%E5%86%8C/describe/3.17/`

## Upstream Linux Coverage

| Candidate IC | Upstream coverage | Evidence | Porting implication |
| --- | --- | --- | --- |
| FUSB302 / FUSB302B | Green | Mainline `drivers/usb/typec/tcpm/Kconfig` defines `CONFIG_TYPEC_FUSB302`; `drivers/usb/typec/tcpm/Makefile` builds `fusb302.o`; `fusb302.c` matches `fcs,fusb302` and registers a TCPM port. | No vendor driver port should be needed if the ATK board uses FUSB302 and DTS supplies the I2C node, interrupt GPIO, connector node, role-switch/mux wiring, and regulators. |
| HUSB311 | Yellow/red | No named HUSB311 driver was found in inspected mainline `drivers/usb/typec/` or `drivers/usb/typec/tcpm/`. Some RK designs describe HUSB311 as an alternate external controller, but software compatibility must be proven from the board vendor DTS/driver tree. | Treat as vendor-porting risk until a compatible string or register-compatible path is confirmed. Do not assume FUSB302 driver compatibility without bench I2C probing or vendor source. |
| FUSB304 | Red | No named FUSB304 driver was found in inspected mainline `drivers/usb/typec/` or TCPM Kconfig/Makefile. | Likely requires vendor driver, a compatible TCPCI/TCPM mapping, or a board-specific replacement plan. |
| WUSB3801 | Partial | Mainline `drivers/usb/typec/Kconfig` defines `CONFIG_TYPEC_WUSB3801`, `Makefile` builds `wusb3801.o`, and `wusb3801.c` matches `willsemi,wusb3801`. | Mainline has attach/orientation support, but this is not equivalent to FUSB302-class USB PD policy-engine coverage. If the board uses WUSB3801, C5.C.1 must verify whether PD3.0 contract negotiation is even available through that silicon. |
| TCPCI-compatible controller | Green if actually TCPCI | Mainline `drivers/usb/typec/tcpm/Kconfig` includes `CONFIG_TYPEC_TCPCI` plus several TCPCI child drivers. | No vendor driver if the IC is standards-compliant TCPCI and has correct DTS bindings; otherwise port vendor glue. |

Kernel source references:

- TCPM tree:
  `https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux.git/+/master/drivers/usb/typec/tcpm/`
- TCPM Kconfig:
  `https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux.git/+/master/drivers/usb/typec/tcpm/Kconfig`
- FUSB302 driver:
  `https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux.git/+/master/drivers/usb/typec/tcpm/fusb302.c`
- Type-C Kconfig:
  `https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux.git/+/master/drivers/usb/typec/Kconfig`
- WUSB3801 driver:
  `https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux.git/+/master/drivers/usb/typec/wusb3801.c`

## Expected Linux Exposure Path

For the FUSB302 hypothesis, the expected mainline path is:

1. Board DTS declares the I2C-attached FUSB302 node with compatible `fcs,fusb302`,
   interrupt GPIO, `vbus` regulator, and a child `connector` node.
2. `CONFIG_TYPEC`, `CONFIG_TYPEC_TCPM`, and `CONFIG_TYPEC_FUSB302` are enabled.
3. `fusb302.c` probes over I2C, initializes the chip, and calls `tcpm_register_port()`.
4. TCPM drives Type-C/PD state and registers the port through the kernel Type-C connector
   class.
5. Userspace observes connector state under `/sys/class/typec/`, with partner/cable/altmode
   children when PD discovery succeeds.

For a UCSI path, the expected mainline path is different:

1. Firmware or an embedded controller exposes a UCSI Platform Policy Manager over ACPI,
   I2C, ChromeOS EC, Qualcomm PMIC GLINK, STM32G0, Cypress CCGx, or another supported
   transport.
2. `CONFIG_TYPEC_UCSI` plus the transport-specific driver probes.
3. `typec_ucsi` registers the Type-C ports and role-swap operations.

No public ATK-DLRK3588 evidence found in this spike indicates a UCSI PPM. Therefore C5.C.1
should default to direct TCPM/typec integration for RK3588/FUSB302 unless vendor BSP DTS or
bench logs show a UCSI provider.

## Vendor-Porting Decision

| Board reality found in C5.C.1 bring-up | Decision |
| --- | --- |
| DTS has `fcs,fusb302` and I2C probe succeeds | Use upstream `fusb302`; porting effort is DTS/config integration only. |
| I2C scan shows FUSB302 address but DTS lacks node | Add board DTS wiring; no driver port expected. |
| DTS or silk/BOM shows HUSB311 | Stop and inspect vendor BSP. If no upstream-compatible binding exists, file vendor-driver porting follow-up before PD daemon work. |
| DTS or silk/BOM shows FUSB304 | Treat as out-of-tree risk; file follow-up for driver availability or controller substitution. |
| DTS or silk/BOM shows WUSB3801 | Verify PD requirements. Mainline WUSB3801 can expose Type-C attach/orientation, but may not satisfy PD3.0 contract negotiation scope. |
| `/sys/class/typec` appears from UCSI instead of TCPM | Document the UCSI transport and adjust C5.C.2 to bind to UCSI-provided ports rather than a direct FUSB302/TCPM port. |

## Manual Verification Plan for Real Hardware

Run these on the ATK-DLRK3588 target before C5.C.1 exits green:

```sh
find /sys/bus/i2c/devices -maxdepth 2 -type f -name name -print -exec cat {} \;
dmesg | grep -Ei 'fusb|tcpm|typec|ucsi|wusb|husb|fusb304'
find /sys/class/typec -maxdepth 3 -print
grep -R . /proc/device-tree 2>/dev/null | grep -Ei 'fusb|tcpm|typec|ucsi|wusb|husb|fusb304'
```

Record the controller marking from either a board photo, vendor schematic, or DTS node.
The acceptance signal is not just "driver loaded"; it is:

- controller identity known;
- relevant upstream or vendor driver identified;
- `/sys/class/typec/port*` exists;
- attach/detach event updates the port state;
- source/sink or DRP behavior matches the product requirement;
- DP-altmode behavior is separately verified if C5.C later depends on display routing.

## Spike Sanity Check

Documentation-only smoke performed for this file:

```sh
test -f docs/audit/2026-06-XX-atk-dlrk3588-typec-controller-spike.md
rg -n 'FUSB302|HUSB311|FUSB304|WUSB3801|UCSI|/sys/class/typec|Vendor-Porting' \
  docs/audit/2026-06-XX-atk-dlrk3588-typec-controller-spike.md
```

No pytest target applies because this ticket adds one markdown audit artifact and no product
code, test harness, backend module, or tooling entry point.

## Conclusion

**Yellow-green for C5.C.1 readiness.**

If the ATK-DLRK3588 board uses FUSB302, upstream Linux coverage is sufficient and the next
leaf should focus on kernel config, DTS wiring, and a real-board `/sys/class/typec` smoke.
Vendor-out-of-tree driver porting is not expected for the FUSB302 path.

The unresolved risk is silicon identity. Public ATK documentation proves the two Type-C OTG
ports and RK3588 connection, but not the controller IC. C5.C.1 must begin with board/DTS
confirmation. If the populated controller is HUSB311, FUSB304, or a WUSB3801-style cost-down
part, the implementation should pause long enough to reclassify the driver path before PD3.0
daemon or role-swap work proceeds.
