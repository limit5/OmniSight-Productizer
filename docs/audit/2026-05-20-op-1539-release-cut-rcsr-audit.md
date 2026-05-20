# OP-1539 release-cut RCSR audit

Generated: 2026-05-20 03:43:25Z

## Scope

This report enumerates recent merged `develop -> main` release-cut changes in Gerrit
and records observed hashtags, topics, owner, and submit-record / submit-requirement
status. The empirical check is the FINDING-4 claim that the release path is
fail-safe/over-strict rather than permissive: missing or over-specific metadata must
not let an already-submitted release cut bypass the intended human/merger review
intent.

Evidence source: `scripts/audit_release_cuts.py` queried merged Gerrit changes on
`branch:main`, filtered release-cut subjects, then fetched REST submit requirements
where the local Gerrit HTTP credential was available.

## Summary

- Release cuts enumerated: 15
- Intent bypass anomalies: 0
- Metadata anomalies: 9
- Verdict: no already-submitted release cut bypassed human submit intent; every
  enumerated cut carries `Code-Review+2` and `SUBM` by `sora`.

## Release-Cut Inventory

| Change | Submitted UTC | Version | Topic | Hashtags | Owner | Author | Submit status | SR status | Human/submit evidence | Notes |
|---|---:|---|---|---|---|---|---|---|---|---|
| [711](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/711) `3967b980` | 2026-05-16 15:53:17Z | manual-2026-05-16 | release-cut-2026-05-16 | OP-1182, release-cut, milestone:R3-fastforward | sora | claude-bot | MERGED; submitRecords=OK | Merger-Plus-2=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE | CR+2=sora; SUBM=sora | metadata present |
| [967](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/967) `ef4483d0` | 2026-05-17 17:37:20Z | v0.5.0-rc2 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [971](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/971) `d0b9799f` | 2026-05-17 22:46:06Z | v0.5.0-rc2-hotfix1 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [974](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/974) `c135c2c2` | 2026-05-17 23:57:56Z | v0.5.0-rc2-hotfix2 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [975](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/975) `c5b11c42` | 2026-05-18 00:01:32Z | v0.5.0-rc2-hotfix3 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [979](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/979) `98b7ecc4` | 2026-05-18 01:00:19Z | v0.5.0-rc2-hotfix4 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [982](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/982) `7747d2f8` | 2026-05-18 01:14:48Z | v0.5.0-rc2-hotfix5 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [984](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/984) `e4305dde` | 2026-05-18 01:28:16Z | v0.5.0-rc2-hotfix6 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [986](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/986) `ed58698e` | 2026-05-18 01:47:29Z | v0.5.0-rc2-hotfix7 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [988](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/988) `8e844971` | 2026-05-18 02:00:38Z | v0.5.0-rc2-hotfix8 | `<missing>` | `<missing>` | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata anomaly: missing topic, hashtags |
| [999](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/999) `bd44cad6` | 2026-05-18 07:42:41Z | v0.5.0-rc3 | v0.5.0-rc3-release-cut | R3-fastforward | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata present |
| [1017](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/1017) `4218e1c7` | 2026-05-18 10:24:32Z | v0.5.0-rc4 | v0.5.0-rc4-release-cut | R3-fastforward | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata present |
| [1022](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/1022) `9538ea0e` | 2026-05-18 13:25:47Z | v0.5.0-rc4-hotfix1 | v0.5.0-rc4-hotfix1-release-cut | R3-fastforward | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata present |
| [1024](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/1024) `293d8c86` | 2026-05-18 13:47:10Z | v0.5.0-rc4-hotfix2 | v0.5.0-rc4-hotfix2-release-cut | R3-fastforward | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata present |
| [1026](https://sora.services:29420/c/omnisight/OmniSight-Productizer/+/1026) `524419b3` | 2026-05-18 14:03:32Z | v0.5.0-rc4-hotfix3 | v0.5.0-rc4-hotfix3-release-cut | R3-fastforward | sora | sora | MERGED; submitRecords=OK | MainFastForwardMergerPlus2=NOT_APPLICABLE<br>Merger-Plus-2=NOT_APPLICABLE<br>release-cut-promote=NOT_APPLICABLE<br>Verified=NOT_APPLICABLE<br>Human-Plus-2=SATISFIED<br>No-Unresolved-Comments=NOT_APPLICABLE<br>Code-Review=SATISFIED<br>No-Veto=SATISFIED | CR+2=sora; SUBM=sora | metadata present |

## FINDING-4 Cross-Check

FINDING-4 is supported by the live sample across all recent submitted cuts:

- The fail-safe side is visible in metadata drift: older/manual cuts lack the
  newer `R3-fastforward` hashtag or release-cut topic, and the current Gerrit
  submit-requirement set often marks `release-cut-promote` or
  `MainFastForwardMergerPlus2` as `NOT_APPLICABLE` for these already-merged
  changes.
- The non-bypass side is also visible: despite that metadata drift, each release
  cut was merged only after `sora` cast `Code-Review+2` and submitted the
  change. No row shows bot-only submission or missing human approval.
- Therefore the observed behavior is over-strict/fallback-to-human, not
  permissive. Metadata mismatches reduce automation applicability; they do not
  waive the human/submit gate.

## Anomalies Flagged

- Metadata drift: older release cuts are missing topic and/or release-cut
  hashtags. This should be treated as reporting/automation metadata drift, not
  as a shipped-code bypass because each affected row still has human CR+2/SUBM.
  - Change 967 (v0.5.0-rc2): missing topic, hashtags.
  - Change 971 (v0.5.0-rc2-hotfix1): missing topic, hashtags.
  - Change 974 (v0.5.0-rc2-hotfix2): missing topic, hashtags.
  - Change 975 (v0.5.0-rc2-hotfix3): missing topic, hashtags.
  - Change 979 (v0.5.0-rc2-hotfix4): missing topic, hashtags.
  - Change 982 (v0.5.0-rc2-hotfix5): missing topic, hashtags.
  - Change 984 (v0.5.0-rc2-hotfix6): missing topic, hashtags.
  - Change 986 (v0.5.0-rc2-hotfix7): missing topic, hashtags.
  - Change 988 (v0.5.0-rc2-hotfix8): missing topic, hashtags.
- Intent anomaly: none.

## Reproduction

```bash
python3 scripts/audit_release_cuts.py --markdown-out /tmp/op-1539-release-cuts.md
```
