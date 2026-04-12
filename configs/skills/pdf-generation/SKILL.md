---
name: pdf-generation
description: Generate PDF documents from structured data. Use when tasks mention PDF, report generation, compliance document, or printable output.
keywords: [pdf, report, document, compliance, generate, printable, weasyprint, pandoc]
---

# PDF Document Generation

Generate professional PDF documents from markdown or structured data.

## Workflow

### Phase 1: Content Assembly
- Gather data from task context, simulation results, or test reports
- Structure into markdown sections with headings, tables, lists
- Include metadata: title, date, author, version

### Phase 2: Template Selection
- Use Jinja2 templates from `configs/templates/` if available
- For custom layouts, create inline markdown with CSS styling
- Support: compliance reports, test summaries, NPI status reports

### Phase 3: Conversion
- Primary: `weasyprint` (HTML/CSS → PDF, best for styled reports)
- Fallback: `pandoc` (Markdown → PDF, requires LaTeX)
- Last resort: Save as `.md` artifact for manual conversion

### Phase 4: Artifact Registration
- Save to `.artifacts/` directory
- Register via `generate_artifact_report` tool
- Include file size, page count in metadata

## Templates Available
- `compliance_report.md.j2` — FCC/CE/RoHS compliance documentation
- `test_summary.md.j2` — Test execution summary with pass/fail metrics

## Guidelines
- Always include page headers with document title and date
- Tables must have headers and consistent column widths
- Use monospace font for code snippets and log excerpts
