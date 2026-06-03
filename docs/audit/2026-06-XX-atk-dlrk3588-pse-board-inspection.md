# ATK-DLRK3588 PSE Board Inspection

**Ticket:** OP-1982
**Date:** 2026-06-03
**Scope:** Case 5 / C5.B.0 audit of the ALIENTEK ATK-DLRK3588 bench target for
Power-over-Ethernet Power Sourcing Equipment (PoE PSE) silicon.

## Summary

This audit verifies the documentation side of the C5.B.0 hypothesis and records
the current bench-evidence gap.

- ALIENTEK's public ATK-DLRK3588B hardware reference lists two gigabit Ethernet
  interfaces and describes them as standard 10/100/1000M network ports through
  external gigabit PHY devices.
- The same public resource list does not advertise PoE, PSE, TPS23861, or an
  alternate PSE controller as an onboard resource.
- No operator bench photo, silkscreen close-up, schematic, or part-number note
  for the physical unit was present in this workspace at audit time.
- Result: the Plan-agent C1 hypothesis, "PSE controller likely absent on bench
  unit," is supported by public product documentation but is not physically
  confirmed from the bench unit.

No production code, backend, CI, database, frontend, Gerrit, security, test, or
tooling files were changed.

## Scope Lock

This is a documentation-only C5.B.0 spike. It does not:

- add or modify a PoE driver;
- modify `configs/skills/connectivity/scaffolds/ethernet_vlan_poe.c`;
- change Case 5 QEMU or real-board smoke scripts;
- update the frozen `HANDOFF.md`;
- infer silicon presence from missing evidence.

The follow-up C5.B.1 stub-comment work and any full PSE driver work remain
outside this ticket.

## Local Design Context

The Case 5 design doc v3 scopes C5.B as initially deferred because the operator
chose to drop PSE from the first software/firmware delivery and procure a PoE
daughtercard long-term. It defines C5.B.0 as the inspection spike and C5.B.1 as
a separate stub-comment update.

Relevant local anchors:

| Anchor | Meaning |
| --- | --- |
| `docs/architecture/2026-06-03-case5-conference-device-epic-design.md` §0 | Q1 picks ATK-DLRK3588; D1 drops PSE initially and keeps daughtercard procurement as the long path. |
| `docs/architecture/2026-06-03-case5-conference-device-epic-design.md` §2 / A9 | Full PSE driver work defers to follow-up C5.B' when daughtercard hardware arrives. |
| `docs/architecture/2026-06-03-case5-conference-device-epic-design.md` §3 / C5.B | C5.B.0 is the physical PSE-silicon inspection/documentation spike. |

## Official Product Evidence

Public ALIENTEK documentation checked on 2026-06-03:

| Source | Evidence | PSE implication |
| --- | --- | --- |
| ALIENTEK wiki, "1.1 ATK-DLRK3588B开发板底板资源" | Lists standard board resources including USB Type-C, HDMI, CAN, SATA, RS232/RS485, 5V/3.3V headers, Wi-Fi/BT, M.2 SSD, and states the core board exposes gigabit Ethernet among many interfaces. URL: `https://wiki.alientek.com/docs/Boards/Linux/DLRK3588/DLRK3588%20%E7%A1%AC%E4%BB%B6%E5%8F%82%E8%80%83%E6%89%8B%E5%86%8C/resource/1.1resource/` | The resource list does not advertise onboard PoE or PSE controller silicon. This is negative documentation evidence only, not a physical absence proof. |
| ALIENTEK wiki, "2 ATK-DLRK3588B开发板硬件资源详细说明" | Describes ETH0 as one onboard 1000M Ethernet interface, says RK3588 has two 10/100/1000M MAC peripherals, and says external 1000M PHY chips implement gigabit wired networking; describes ETH1 as the other 1000M Ethernet interface. URL: `https://wiki.alientek.com/docs/Boards/Linux/DLRK3588/DLRK3588%20%E7%A1%AC%E4%BB%B6%E5%8F%82%E8%80%83%E6%89%8B%E5%86%8C/notice/notice/` | The Ethernet section names MAC + external PHY, but not PoE magnetics, PSE controller, 48V input, powered-pair injection, TPS23861, or equivalent PSE circuitry. |
| TI TPS23861 product page | Identifies TPS23861 as a 2-pair, type-2, 4-channel PoE PSE controller with autonomous mode and optional I2C monitoring/control. URL: `https://www.ti.com/product/TPS23861/part-details/TPS23861PWR` | This establishes the reference PSE silicon family the C5 design was checking for; it does not establish that the ALIENTEK board has the part. |

Search terms used for public evidence:

- `ALIENTEK ATK-DLRK3588 product specifications PoE PSE TPS23861`
- `ATK-DLRK3588 official product page specifications Ethernet PoE`
- `正点原子 ATK-DLRK3588 开发板 参数 千兆以太网 PoE`

## Bench Inspection Record

Physical bench evidence available to this runner:

| Evidence item | Status | Result |
| --- | --- | --- |
| Operator bench photo of Ethernet / power area | Not available in workspace | Cannot identify or rule out a PSE controller by package marking. |
| Operator close-up of top-side IC markings | Not available in workspace | No part number recorded. |
| Operator close-up of bottom-side IC markings | Not available in workspace | No part number recorded. |
| Vendor schematic / board PDF with PSE block | Not found in local repo; public wiki page found, but not a PSE schematic | No schematic-level confirmation of PSE presence or absence. |
| Local image attachments under this checkout | `find . -maxdepth 4 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) -print` returned no files | No bench image evidence to inspect. |

The physical inspection result is therefore:

| Question | Answer |
| --- | --- |
| Is a TPS23861 physically verified on the bench ATK-DLRK3588 unit? | No. |
| Is an alternate PSE controller physically verified? | No. |
| Is PSE silicon physically verified absent? | No; absence cannot be claimed without board photos, schematic, or measured board inspection. |
| Does public product documentation advertise onboard PSE? | No evidence found in the checked ALIENTEK public wiki pages. |

## Plan-Agent C1 Disposition

Plan-agent C1 stated that PSE is likely absent on the ATK-DLRK3588 bench unit.

Disposition: **partially supported, not bench-confirmed**.

- Supported: the public ALIENTEK resource and hardware-detail pages describe
  dual gigabit Ethernet through RK3588 MACs and external 1000M PHY chips, with
  no PoE/PSE feature called out.
- Not confirmed: this runner had no operator-side physical photo or schematic
  evidence showing the Ethernet power-injection area, PSE-controller package,
  or relevant silkscreen.

C5.B should continue to treat PSE as deferred unless the operator supplies a
bench photo or schematic that identifies a PSE controller on the actual unit.

## Daughtercard Path If PSE Is Absent

Use a standalone PSE evaluation board or reference-design daughtercard path
instead of assuming the ATK-DLRK3588 carrier has onboard PSE circuitry.

| Option | Vendor / part | Current public procurement signal | Fit for C5.B' |
| --- | --- | --- | --- |
| Preferred engineering path | Texas Instruments `TPS23861EVM-612` evaluation module | TI describes it as a quad IEEE 802.3at PoE PSE evaluation module. TI's product page reports TI.com ordering unavailable/out of stock, while distributor ordering must be checked separately. URL: `https://www.ti.com/tool/TPS23861EVM-612` | Best match to the existing Case 5 TPS23861 hypothesis; supports autonomous mode and optional I2C/GUI paths for later driver bring-up. |
| Reference-design path | TI `TIDA-00290`, made from motherboard `TPS23861EVM-612` plus daughter board `TPS23861EVM-613` | TI documents a fully autonomous IEEE 802.3at Type-2 30W quad-port PSE reference design and states the daughter board contains two TPS23861 PSE controllers. URL: `https://www.ti.com/tool/TIDA-00290` | Best path when the follow-up needs schematics, BOM, Gerbers, and conformance-tested design collateral rather than only a purchasable EVM. |
| Distributor buy path | DigiKey `296-37852-ND` / TI `TPS23861EVM-612` | DigiKey page checked on 2026-06-03 listed 3 in stock, 333.53 EUR unit price before VAT, 403.5713 EUR with VAT, and 12-week standard manufacturer lead time. URL: `https://www.digikey.es/es/products/detail/texas-instruments/TPS23861EVM-612/4878476` | Practical short-term purchase route if the operator accepts distributor region/pricing and confirms shipping to the bench location. |

Operator procurement notes:

- Order the PSE EVM/daughtercard as a lab fixture, not as an ATK-DLRK3588
  carrier-board retrofit.
- Budget for a separate 44-57V DC supply if the selected PSE board requires it;
  the ALIENTEK carrier's documented 12V input is not enough evidence for a PSE
  power rail.
- Record the purchased vendor, SKU, price, and actual lead time in the C5.B'
  follow-up ticket when hardware is ordered.

## Recommended Follow-Up Evidence

For a definitive bench result, attach the following to the C5.B' or real-hardware
META ticket:

1. Top-side photo covering both RJ45 connectors, magnetics, and nearby ICs.
2. Bottom-side photo of the same Ethernet/power-injection region.
3. Close-up of any IC marking matching `TPS23861`, `TPS2388`, `Si345x`,
   `MAX59xx`, `LTC42xx`, or other PoE PSE families.
4. If available, the ALIENTEK schematic page covering ETH0/ETH1 magnetics and
   power injection.
5. Multimeter note showing whether any 44-57V PSE rail exists on the board.

## Sanity Checks

The document was sanity-checked with:

```bash
test -f docs/audit/2026-06-XX-atk-dlrk3588-pse-board-inspection.md
rg -n "TPS23861|ALIENTEK|DigiKey|C5\\.B\\.0|Plan-agent C1" docs/audit/2026-06-XX-atk-dlrk3588-pse-board-inspection.md
git diff --name-only develop
```

Expected diff scope: this file only.
