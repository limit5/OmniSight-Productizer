"""SC.9 — Per-jurisdiction privacy-notice templates for generated apps.

Small framework-agnostic privacy notice generator intended for generated
web/service templates.  SC.9.1 covers GDPR notice text; SC.9.2 covers
CCPA notice text.  PIPL, LGPD, PIPEDA, SDK inference, and DSAR workflow
scaffolding are separate SC.9 / SC.10 rows.

Security boundary:

  * This module emits a legal-review-ready markdown template, not legal
    advice and not an automated compliance decision.
  * The GDPR data-subject rights covered here are access, portability,
    erasure, and objection.
  * The CCPA consumer rights covered here are know/access, delete,
    correct, opt-out of sale/sharing, limit sensitive personal
    information, and non-discrimination.
  * Runtime request handling, identity verification, SLA timers, and
    export/delete endpoints are owned by SC.10.

All module-level state is immutable constants.  Cross-worker safety
follows SOP Step 1 answer #1: each uvicorn worker derives identical
notice text from the same source code; there is no shared cache,
singleton, or runtime mutation.
"""

from __future__ import annotations

from dataclasses import dataclass


JURISDICTION_GDPR = "gdpr"
JURISDICTION_CCPA = "ccpa"
GDPR_RESPONSE_DEADLINE = "one month"
CCPA_RESPONSE_DEADLINE = "45 calendar days"
CCPA_OPT_OUT_LIMIT_DEADLINE = "15 business days"


@dataclass(frozen=True)
class DataSubjectRight:
    """One GDPR data-subject right represented in the notice."""

    id: str
    label: str
    article: str
    summary: str
    request_prompt: str


@dataclass(frozen=True)
class PrivacyNoticeSection:
    """One markdown section in a jurisdiction privacy notice."""

    id: str
    title: str
    body: str


@dataclass(frozen=True)
class PrivacyNoticeTemplate:
    """Rendered privacy notice plus machine-readable section metadata."""

    jurisdiction: str
    title: str
    sections: tuple[PrivacyNoticeSection, ...]
    rights: tuple[DataSubjectRight, ...]

    @property
    def markdown(self) -> str:
        """Return the rendered notice body as markdown."""

        parts = [f"# {self.title}"]
        parts.extend(f"## {section.title}\n{section.body}" for section in self.sections)
        return "\n\n".join(parts) + "\n"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation for API callers."""

        return {
            "jurisdiction": self.jurisdiction,
            "title": self.title,
            "markdown": self.markdown,
            "sections": [
                {
                    "id": section.id,
                    "title": section.title,
                    "body": section.body,
                }
                for section in self.sections
            ],
            "rights": [
                {
                    "id": right.id,
                    "label": right.label,
                    "article": right.article,
                    "summary": right.summary,
                    "request_prompt": right.request_prompt,
                }
                for right in self.rights
            ],
        }


GDPR_RIGHTS = (
    DataSubjectRight(
        id="access",
        label="Right of access",
        article="GDPR Article 15",
        summary=(
            "Individuals may request confirmation that personal data is "
            "processed and receive a copy of that data."
        ),
        request_prompt=(
            "Submit an access request through the privacy contact or DSAR "
            "intake path."
        ),
    ),
    DataSubjectRight(
        id="portability",
        label="Right to data portability",
        article="GDPR Article 20",
        summary=(
            "Individuals may request personal data they provided in a "
            "structured, commonly used, machine-readable format."
        ),
        request_prompt=(
            "Submit a portability request and specify the account or service "
            "scope to export."
        ),
    ),
    DataSubjectRight(
        id="erasure",
        label="Right to erasure",
        article="GDPR Article 17",
        summary=(
            "Individuals may request deletion of personal data where GDPR "
            "grounds for erasure apply."
        ),
        request_prompt=(
            "Submit an erasure request; the service may retain records needed "
            "for legal, security, billing, or audit obligations."
        ),
    ),
    DataSubjectRight(
        id="objection",
        label="Right to object",
        article="GDPR Article 21",
        summary=(
            "Individuals may object to processing based on legitimate "
            "interests or direct marketing."
        ),
        request_prompt=(
            "Submit an objection request and identify the processing activity "
            "being challenged."
        ),
    ),
)


CCPA_RIGHTS = (
    DataSubjectRight(
        id="know",
        label="Right to know and access",
        article="CCPA",
        summary=(
            "California consumers may request the categories and specific "
            "pieces of personal information collected about them, the "
            "sources, purposes, and disclosure categories."
        ),
        request_prompt=(
            "Submit a request to know through the California privacy request "
            "path."
        ),
    ),
    DataSubjectRight(
        id="delete",
        label="Right to delete",
        article="CCPA",
        summary=(
            "California consumers may request deletion of personal "
            "information collected from them, subject to CCPA exceptions."
        ),
        request_prompt=(
            "Submit a deletion request; the service may retain records needed "
            "for legal, security, billing, dispute, or audit obligations."
        ),
    ),
    DataSubjectRight(
        id="correct",
        label="Right to correct",
        article="CCPA as amended by CPRA",
        summary=(
            "California consumers may request correction of inaccurate "
            "personal information maintained by the business."
        ),
        request_prompt=(
            "Submit a correction request and identify the inaccurate account "
            "or service data."
        ),
    ),
    DataSubjectRight(
        id="opt_out_sale_sharing",
        label="Right to opt out of sale or sharing",
        article="CCPA as amended by CPRA",
        summary=(
            "California consumers may direct a business not to sell personal "
            "information or share it for cross-context behavioral advertising."
        ),
        request_prompt=(
            "Use the Do Not Sell or Share link, supported privacy signal, or "
            "privacy request path."
        ),
    ),
    DataSubjectRight(
        id="limit_sensitive_pi",
        label="Right to limit sensitive personal information",
        article="CCPA as amended by CPRA",
        summary=(
            "California consumers may limit use and disclosure of sensitive "
            "personal information to purposes permitted by CCPA."
        ),
        request_prompt=(
            "Use the Limit the Use of My Sensitive Personal Information link "
            "or privacy request path where sensitive data is collected."
        ),
    ),
    DataSubjectRight(
        id="non_discrimination",
        label="Right to non-discrimination",
        article="CCPA",
        summary=(
            "California consumers may exercise CCPA rights without unlawful "
            "discrimination or retaliation."
        ),
        request_prompt=(
            "Contact the privacy team if exercising a CCPA right changes "
            "service access, pricing, or quality."
        ),
    ),
)


def build_gdpr_privacy_notice(
    *,
    controller_name: str = "{{ controller_name }}",
    privacy_contact: str = "{{ privacy_contact }}",
    dpo_contact: str = "{{ dpo_contact }}",
    dsar_endpoint: str = "{{ dsar_endpoint }}",
    effective_date: str = "{{ effective_date }}",
) -> PrivacyNoticeTemplate:
    """Build the GDPR privacy-notice markdown template."""

    sections = (
        PrivacyNoticeSection(
            id="scope",
            title="Scope",
            body=(
                f"This notice explains how {controller_name} processes "
                "personal data for users in the European Economic Area, the "
                "United Kingdom, and Switzerland. It is a template and must "
                "be reviewed against the generated application's actual data "
                "flows before publication.\n\n"
                f"Effective date: {effective_date}."
            ),
        ),
        PrivacyNoticeSection(
            id="controller",
            title="Controller and Contacts",
            body=(
                f"Controller: {controller_name}.\n\n"
                f"Privacy contact: {privacy_contact}.\n\n"
                f"Data Protection Officer or EU representative: {dpo_contact}."
            ),
        ),
        PrivacyNoticeSection(
            id="categories",
            title="Personal Data We Process",
            body=(
                "The generated application should list each personal-data "
                "category it collects, including account profile data, "
                "authentication identifiers, billing records, support "
                "messages, device or log data, and integration data received "
                "from connected third-party services."
            ),
        ),
        PrivacyNoticeSection(
            id="purposes",
            title="Purposes and Legal Bases",
            body=(
                "Document each processing purpose and legal basis, including "
                "contract performance, legitimate interests, consent where "
                "required, compliance with legal obligations, fraud and "
                "security monitoring, service analytics, and customer support."
            ),
        ),
        PrivacyNoticeSection(
            id="rights",
            title="Your GDPR Rights",
            body=_rights_markdown(GDPR_RIGHTS),
        ),
        PrivacyNoticeSection(
            id="requests",
            title="How to Exercise Your Rights",
            body=(
                f"Submit GDPR requests through {dsar_endpoint} or by contacting "
                f"{privacy_contact}. We verify requester identity before "
                "disclosing, exporting, deleting, or restricting personal "
                f"data. We normally respond within {GDPR_RESPONSE_DEADLINE}, "
                "unless an extension is permitted by GDPR for complex or "
                "multiple requests."
            ),
        ),
        PrivacyNoticeSection(
            id="retention",
            title="Retention",
            body=(
                "List retention periods for each data category. Personal data "
                "should be retained only as long as needed for the stated "
                "purposes, unless longer retention is required for legal, "
                "security, billing, dispute, or audit obligations."
            ),
        ),
        PrivacyNoticeSection(
            id="transfers",
            title="International Transfers and Processors",
            body=(
                "List processors and third-party services that receive "
                "personal data. For transfers outside the EEA, document the "
                "transfer mechanism, such as an adequacy decision, Standard "
                "Contractual Clauses, or another GDPR-recognised safeguard."
            ),
        ),
    )
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_GDPR,
        title="GDPR Privacy Notice Template",
        sections=sections,
        rights=GDPR_RIGHTS,
    )


def build_ccpa_privacy_notice(
    *,
    business_name: str = "{{ business_name }}",
    privacy_contact: str = "{{ privacy_contact }}",
    ccpa_request_endpoint: str = "{{ ccpa_request_endpoint }}",
    do_not_sell_or_share_link: str = "{{ do_not_sell_or_share_link }}",
    limit_sensitive_pi_link: str = "{{ limit_sensitive_pi_link }}",
    effective_date: str = "{{ effective_date }}",
) -> PrivacyNoticeTemplate:
    """Build the CCPA privacy-notice markdown template."""

    sections = (
        PrivacyNoticeSection(
            id="scope",
            title="Scope",
            body=(
                f"This notice explains how {business_name} collects, uses, "
                "discloses, sells, or shares personal information about "
                "California consumers. It is a template and must be reviewed "
                "against the generated application's actual data flows before "
                "publication.\n\n"
                f"Effective date: {effective_date}."
            ),
        ),
        PrivacyNoticeSection(
            id="business",
            title="Business and Contact",
            body=(
                f"Business: {business_name}.\n\n"
                f"Privacy contact: {privacy_contact}.\n\n"
                f"California privacy request path: {ccpa_request_endpoint}."
            ),
        ),
        PrivacyNoticeSection(
            id="categories",
            title="Personal Information We Collect",
            body=(
                "List each CCPA personal-information category collected in "
                "the last 12 months, including identifiers, account profile "
                "data, commercial information, internet or network activity, "
                "geolocation data if collected, professional information if "
                "collected, inferences, and sensitive personal information "
                "where applicable."
            ),
        ),
        PrivacyNoticeSection(
            id="sources",
            title="Sources and Purposes",
            body=(
                "Document the categories of sources for personal information "
                "and the business or commercial purposes for collection, use, "
                "retention, disclosure, sale, or sharing."
            ),
        ),
        PrivacyNoticeSection(
            id="disclosures",
            title="Disclosure, Sale, and Sharing",
            body=(
                "List the categories of personal information disclosed for a "
                "business purpose, sold, or shared in the last 12 months, and "
                "the categories of third parties or service providers that "
                "received each category. If the application does not sell or "
                "share personal information, state that explicitly."
            ),
        ),
        PrivacyNoticeSection(
            id="rights",
            title="Your California Privacy Rights",
            body=_rights_markdown(CCPA_RIGHTS),
        ),
        PrivacyNoticeSection(
            id="requests",
            title="How to Exercise Your Rights",
            body=(
                f"Submit CCPA requests through {ccpa_request_endpoint} or by "
                f"contacting {privacy_contact}. We verify requester identity "
                "before disclosing, deleting, or correcting personal "
                f"information. We normally respond to know, delete, and "
                f"correct requests within {CCPA_RESPONSE_DEADLINE}; permitted "
                "extensions may apply. We process opt-out of sale/sharing and "
                "limit-sensitive-information requests as soon as feasibly "
                f"possible and no later than {CCPA_OPT_OUT_LIMIT_DEADLINE}."
            ),
        ),
        PrivacyNoticeSection(
            id="opt_out_links",
            title="Opt-Out and Sensitive Information Links",
            body=(
                "Do Not Sell or Share My Personal Information: "
                f"{do_not_sell_or_share_link}.\n\n"
                "Limit the Use of My Sensitive Personal Information: "
                f"{limit_sensitive_pi_link}.\n\n"
                "If the generated application honors browser or device "
                "privacy signals, document that handling here."
            ),
        ),
        PrivacyNoticeSection(
            id="retention",
            title="Retention",
            body=(
                "List retention periods or criteria for each personal "
                "information category. Personal information should be retained "
                "only as long as reasonably necessary and proportionate for "
                "the disclosed purposes, unless longer retention is required "
                "for legal, security, billing, dispute, or audit obligations."
            ),
        ),
    )
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_CCPA,
        title="CCPA Privacy Notice Template",
        sections=sections,
        rights=CCPA_RIGHTS,
    )


def _rights_markdown(rights: tuple[DataSubjectRight, ...]) -> str:
    lines: list[str] = []
    for right in rights:
        lines.append(
            f"- **{right.label} ({right.article})**: "
            f"{right.summary} {right.request_prompt}"
        )
    return "\n".join(lines)


__all__ = (
    "CCPA_OPT_OUT_LIMIT_DEADLINE",
    "CCPA_RESPONSE_DEADLINE",
    "CCPA_RIGHTS",
    "GDPR_RESPONSE_DEADLINE",
    "GDPR_RIGHTS",
    "JURISDICTION_CCPA",
    "JURISDICTION_GDPR",
    "DataSubjectRight",
    "PrivacyNoticeSection",
    "PrivacyNoticeTemplate",
    "build_ccpa_privacy_notice",
    "build_gdpr_privacy_notice",
)
