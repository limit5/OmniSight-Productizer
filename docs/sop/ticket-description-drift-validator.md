# Ticket Description Drift Validator

`scripts/ticket-description-drift-validator.py` is the filing gate for
SP-B-X follow-up tickets filed after SP-B-X-015 closes. It compares filed
JIRA ticket descriptions with the matching parent-spec child section and
writes `docs/audit/sprint-sp-b-x-drift-validator-{ts}.md`.

## Usage

```bash
python3 scripts/ticket-description-drift-validator.py PARENT_SPEC.md OP-1101 OP-1102
```

The script reads each ticket with `GET /rest/api/3/issue/{key}`. Exit `0`
means no drift; exit `1` means at least one checked ticket drifted.

## Checks

- select parent section by the `SP-B-X-NNN` identifier in summary or
  description;
- extract Boundaries YAML from parent and child markdown;
- compare `required_paths`, `forbidden_paths`, `loc_delta_max`,
  `files_touched_max`, `class`, `l1_exclusive_reason`, `l2_reason`,
  `tag_type`, `authority_required`, `schema_version`, `external_systems`,
  `dependency_artifacts`, and `mutex_with`;
- run `governance_engine.schema.forbidden_combinations.validate_forbidden_combinations()`;
- detect count-claim drift such as `9 categories` vs `8 categories`.

## META AC

SP-B-X-META must include: "Before any SP-B-X-NNN-followup child filed after
SP-B-X-015 closes is created, `scripts/ticket-description-drift-validator.py`
must pass against the parent spec and all filed child ticket keys."

SP-B-X-015 and SP-B-X-001 through SP-B-X-014 are bootstrap exceptions.
