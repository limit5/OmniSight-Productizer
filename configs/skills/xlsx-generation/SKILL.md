---
name: xlsx-generation
description: Create Excel spreadsheets with structured data. Use when tasks mention spreadsheet, Excel, xlsx, data export, or tabular report.
keywords: [xlsx, excel, spreadsheet, tabular, export, openpyxl, csv, data-table]
---

# Excel Spreadsheet Generation

Create structured Excel spreadsheets from test results, metrics, or tracking data.

## Workflow

### Phase 1: Data Collection
- Gather source data: simulation results, token usage, NPI milestones, test metrics
- Normalize into rows and columns with consistent types
- Identify which sheets are needed (summary, details, raw data)

### Phase 2: Structure Design
- Sheet 1: Summary dashboard (key metrics, totals, status)
- Sheet 2+: Detailed data tables per category
- Define column headers, data types, and formatting

### Phase 3: Generation
- Primary: `openpyxl` (full Excel support with formatting)
- Fallback: CSV export (universal, no dependency)
- Apply: column widths, header formatting, number formats

### Phase 4: Save & Register
- Save to `.artifacts/` with descriptive filename
- Register as artifact (type: "xlsx" or "csv")

## Common Use Cases
- Simulation results matrix (module × platform × status)
- Token usage report (model × cost × request count)
- NPI milestone tracker (phase × track × completion %)
- Test coverage report (module × test count × pass rate)

## Guidelines
- First row must be headers (bold, frozen pane)
- Numbers must be stored as numbers, not strings
- Include a "Generated" timestamp in cell A1 of summary sheet
