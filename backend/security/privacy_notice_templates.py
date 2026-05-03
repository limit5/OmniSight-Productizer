"""SC.9 — Per-jurisdiction privacy-notice templates for generated apps.

Small framework-agnostic privacy notice generator intended for generated
web/service templates.  SC.9.1 covers GDPR notice text; SC.9.2 covers
CCPA notice text; SC.9.3 covers PIPL notice text; SC.9.4 covers LGPD
notice text; SC.9.5 covers PIPEDA notice text; SC.9.6 infers required
SDK / third-party clauses.  DSAR workflow scaffolding is a separate
SC.10 row.

Security boundary:

  * This module emits a legal-review-ready markdown template, not legal
    advice and not an automated compliance decision.
  * The GDPR data-subject rights covered here are access, portability,
    erasure, and objection.
  * The CCPA consumer rights covered here are know/access, delete,
    correct, opt-out of sale/sharing, limit sensitive personal
    information, and non-discrimination.
  * The PIPL individual rights covered here are know/decide,
    restrict/refuse, access/copy, portability, correction, deletion,
    explanation, and deceased close-relative rights.
  * The LGPD data-subject rights covered here are confirmation/access,
    correction, anonymization/blocking/deletion, portability, deletion
    of consent-based data, information about sharing, consent refusal
    information, consent revocation, petitioning the ANPD, objection,
    and review of automated decisions.
  * The PIPEDA individual rights covered here are access, correction,
    withdrawal of consent, and challenging compliance.
  * The SDK / third-party clause inference covered here is a deterministic
    static mapping from known dependency identifiers to notice clauses;
    unknown dependencies still require human legal / privacy review.
  * Runtime request handling, identity verification, SLA timers, and
    export/delete endpoints are owned by SC.10.

All module-level state is immutable constants.  Cross-worker safety
follows SOP Step 1 answer #1: each uvicorn worker derives identical
notice text from the same source code; there is no shared cache,
singleton, or runtime mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


JURISDICTION_GDPR = "gdpr"
JURISDICTION_CCPA = "ccpa"
JURISDICTION_PIPL = "pipl"
JURISDICTION_LGPD = "lgpd"
JURISDICTION_PIPEDA = "pipeda"
GDPR_RESPONSE_DEADLINE = "one month"
CCPA_RESPONSE_DEADLINE = "45 calendar days"
CCPA_OPT_OUT_LIMIT_DEADLINE = "15 business days"
LGPD_ACCESS_RESPONSE_DEADLINE = "15 days"
PIPEDA_ACCESS_RESPONSE_DEADLINE = "30 calendar days"
PIPEDA_ACCESS_EXTENSION_DEADLINE = "30 additional days"


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
class SdkPrivacyClause:
    """One inferred SDK / third-party clause for generated notices."""

    id: str
    title: str
    body: str
    matched_dependencies: tuple[str, ...]


@dataclass(frozen=True)
class SdkPrivacyClauseRule:
    """Static dependency matcher for one SDK / third-party clause."""

    id: str
    title: str
    identifiers: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class PrivacyNoticeTemplate:
    """Rendered privacy notice plus machine-readable section metadata."""

    jurisdiction: str
    title: str
    sections: tuple[PrivacyNoticeSection, ...]
    rights: tuple[DataSubjectRight, ...]
    inferred_clauses: tuple[SdkPrivacyClause, ...] = ()

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
            "inferred_clauses": [
                {
                    "id": clause.id,
                    "title": clause.title,
                    "body": clause.body,
                    "matched_dependencies": list(clause.matched_dependencies),
                }
                for clause in self.inferred_clauses
            ],
        }


SDK_PRIVACY_CLAUSE_RULES = (
    SdkPrivacyClauseRule(
        id="analytics_measurement",
        title="Analytics and Product Measurement",
        identifiers=(
            "FirebaseAnalytics",
            "com.google.firebase:firebase-analytics",
            "com.google.firebase:firebase-analytics-ktx",
            "Mixpanel",
            "com.mixpanel.android:mixpanel-android",
            "Amplitude",
            "com.amplitude:analytics-android",
            "Analytics-Swift",
            "com.segment.analytics.kotlin:android",
        ),
        body=(
            "Disclose analytics SDK use, the event, device, usage, and "
            "identifier data collected, the measurement purposes, whether "
            "analytics data is linked to an account or device, retention "
            "periods, and any consent or opt-out control."
        ),
    ),
    SdkPrivacyClauseRule(
        id="crash_diagnostics",
        title="Crash Reporting and Diagnostics",
        identifiers=(
            "FirebaseCrashlytics",
            "com.google.firebase:firebase-crashlytics",
            "com.google.firebase:firebase-crashlytics-ktx",
            "Sentry",
            "io.sentry:sentry-android",
        ),
        body=(
            "Disclose crash and diagnostics SDK use, including device "
            "identifiers, stack traces, logs, performance data, and the "
            "support, security, reliability, and debugging purposes for "
            "processing."
        ),
    ),
    SdkPrivacyClauseRule(
        id="push_messaging",
        title="Push Notifications and Messaging",
        identifiers=(
            "FirebaseMessaging",
            "com.google.firebase:firebase-messaging",
            "OneSignal",
            "com.onesignal:OneSignal",
        ),
        body=(
            "Disclose push-notification SDK use, device tokens, notification "
            "interaction data, delivery analytics, the purpose for sending "
            "messages, and how users can change notification choices."
        ),
    ),
    SdkPrivacyClauseRule(
        id="oauth_identity",
        title="Third-Party Sign-In",
        identifiers=(
            "GoogleSignIn",
            "com.google.android.gms:play-services-auth",
            "FBSDKCoreKit",
            "FacebookCore",
            "com.facebook.android:facebook-android-sdk",
        ),
        body=(
            "Disclose third-party sign-in providers, account identifiers, "
            "profile fields received from the provider, authentication "
            "purposes, and how users can unlink or revoke connected accounts."
        ),
    ),
    SdkPrivacyClauseRule(
        id="payments_purchases",
        title="Payments and Purchases",
        identifiers=(
            "StripePayments",
            "StripePaymentSheet",
            "StripeCore",
            "com.stripe:stripe-android",
            "RevenueCat",
            "com.revenuecat.purchases:purchases",
        ),
        body=(
            "Disclose payment or subscription processors, purchase history, "
            "transaction identifiers, contact or billing data handled by the "
            "processor, fraud-prevention purposes, and processor retention or "
            "refund-support obligations."
        ),
    ),
    SdkPrivacyClauseRule(
        id="advertising_tracking",
        title="Advertising, Attribution, and Tracking",
        identifiers=(
            "Google-Mobile-Ads-SDK",
            "com.google.android.gms:play-services-ads",
            "AppLovinSDK",
            "com.applovin:applovin-sdk",
            "Branch-SDK",
            "io.branch.sdk.android:library",
            "FBSDKCoreKit",
            "FacebookCore",
            "com.facebook.android:facebook-android-sdk",
        ),
        body=(
            "Disclose advertising, attribution, or cross-context tracking "
            "SDKs, identifiers and location or usage data collected, ad or "
            "measurement purposes, sharing with advertising partners, and "
            "the applicable consent, App Tracking Transparency, Do Not Sell "
            "or Share, or opt-out controls."
        ),
    ),
)


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


PIPL_RIGHTS = (
    DataSubjectRight(
        id="know_decide",
        label="Right to know and decide",
        article="PIPL Article 44",
        summary=(
            "Individuals may know and decide how their personal information "
            "is processed, unless laws or administrative regulations provide "
            "otherwise."
        ),
        request_prompt=(
            "Submit a request identifying the processing activity, account, "
            "or service scope."
        ),
    ),
    DataSubjectRight(
        id="restrict_refuse",
        label="Right to restrict or refuse processing",
        article="PIPL Article 44",
        summary=(
            "Individuals may restrict or refuse processing of their personal "
            "information, unless laws or administrative regulations provide "
            "otherwise."
        ),
        request_prompt=(
            "Submit a restriction or refusal request and identify the "
            "processing activity being challenged."
        ),
    ),
    DataSubjectRight(
        id="access_copy",
        label="Right to access and copy",
        article="PIPL Article 45",
        summary=(
            "Individuals may consult and copy their personal information, "
            "subject to exceptions under PIPL."
        ),
        request_prompt=(
            "Submit an access or copy request through the PIPL request path."
        ),
    ),
    DataSubjectRight(
        id="portability",
        label="Right to portability",
        article="PIPL Article 45",
        summary=(
            "Individuals may request transfer of personal information to a "
            "designated processor where PIPL and national cyberspace "
            "authority conditions are met."
        ),
        request_prompt=(
            "Submit a portability request and specify the recipient processor "
            "and service scope."
        ),
    ),
    DataSubjectRight(
        id="correction",
        label="Right to correction or supplementation",
        article="PIPL Article 46",
        summary=(
            "Individuals may request correction or supplementation when "
            "personal information is inaccurate or incomplete."
        ),
        request_prompt=(
            "Submit a correction request and identify the inaccurate or "
            "incomplete account or service data."
        ),
    ),
    DataSubjectRight(
        id="deletion",
        label="Right to deletion",
        article="PIPL Article 47",
        summary=(
            "Individuals may request deletion where PIPL deletion grounds "
            "apply and the processor has not proactively deleted the data."
        ),
        request_prompt=(
            "Submit a deletion request; the service may retain or restrict "
            "processing where retention is still required by law or technically "
            "necessary safeguards."
        ),
    ),
    DataSubjectRight(
        id="explanation",
        label="Right to request explanation",
        article="PIPL Article 48",
        summary=(
            "Individuals may request that a personal information processor "
            "explain its personal information processing rules."
        ),
        request_prompt=(
            "Submit an explanation request through the PIPL request path or "
            "privacy contact."
        ),
    ),
    DataSubjectRight(
        id="deceased_close_relative",
        label="Close-relative rights for deceased individuals",
        article="PIPL Article 49",
        summary=(
            "Close relatives may exercise consultation, copying, correction, "
            "and deletion rights over a deceased individual's relevant "
            "personal information for their own lawful and legitimate interests, "
            "unless the deceased individual arranged otherwise."
        ),
        request_prompt=(
            "Submit a close-relative request with documentation needed to "
            "verify identity, relationship, and lawful interest."
        ),
    ),
)


LGPD_RIGHTS = (
    DataSubjectRight(
        id="confirmation_access",
        label="Right to confirmation and access",
        article="LGPD Articles 18 and 19",
        summary=(
            "Data subjects may confirm whether personal data is processed "
            "and access personal data held by the controller."
        ),
        request_prompt=(
            "Submit a confirmation or access request through the LGPD request "
            "path."
        ),
    ),
    DataSubjectRight(
        id="correction",
        label="Right to correction",
        article="LGPD Article 18",
        summary=(
            "Data subjects may request correction of incomplete, inaccurate, "
            "or outdated personal data."
        ),
        request_prompt=(
            "Submit a correction request and identify the incomplete, "
            "inaccurate, or outdated account or service data."
        ),
    ),
    DataSubjectRight(
        id="anonymization_blocking_deletion",
        label="Right to anonymization, blocking, or deletion",
        article="LGPD Article 18",
        summary=(
            "Data subjects may request anonymization, blocking, or deletion "
            "of unnecessary, excessive, or unlawfully processed personal data."
        ),
        request_prompt=(
            "Submit an anonymization, blocking, or deletion request and "
            "identify the processing activity being challenged."
        ),
    ),
    DataSubjectRight(
        id="portability",
        label="Right to portability",
        article="LGPD Article 18",
        summary=(
            "Data subjects may request portability of personal data to "
            "another service or product provider, subject to ANPD regulation "
            "and commercial or industrial secrets."
        ),
        request_prompt=(
            "Submit a portability request and specify the recipient provider "
            "and service scope."
        ),
    ),
    DataSubjectRight(
        id="consent_based_deletion",
        label="Right to deletion of consent-based data",
        article="LGPD Articles 16 and 18",
        summary=(
            "Data subjects may request deletion of personal data processed "
            "with consent, subject to LGPD retention exceptions."
        ),
        request_prompt=(
            "Submit a consent-based deletion request; the service may retain "
            "records needed for legal, security, billing, dispute, or audit "
            "obligations."
        ),
    ),
    DataSubjectRight(
        id="sharing_information",
        label="Right to information about sharing",
        article="LGPD Article 18",
        summary=(
            "Data subjects may request information about public and private "
            "entities with which the controller shared personal data."
        ),
        request_prompt=(
            "Submit a sharing-information request and identify the relevant "
            "account, service, or processing activity."
        ),
    ),
    DataSubjectRight(
        id="consent_information",
        label="Right to consent refusal information",
        article="LGPD Article 18",
        summary=(
            "Data subjects may receive information about the possibility of "
            "refusing consent and the consequences of refusal."
        ),
        request_prompt=(
            "Review the consent prompt or contact the privacy team before "
            "deciding whether to provide optional consent."
        ),
    ),
    DataSubjectRight(
        id="consent_revocation",
        label="Right to revoke consent",
        article="LGPD Articles 8 and 18",
        summary=(
            "Data subjects may revoke consent for consent-based processing "
            "through a free and facilitated procedure."
        ),
        request_prompt=(
            "Use the consent-management path or submit a revocation request "
            "through the LGPD request path."
        ),
    ),
    DataSubjectRight(
        id="petition_anpd",
        label="Right to petition the ANPD",
        article="LGPD Article 18",
        summary=(
            "Data subjects may petition Brazil's national data protection "
            "authority regarding their personal data."
        ),
        request_prompt=(
            "Contact the privacy team first where appropriate, then petition "
            "the ANPD if the issue remains unresolved."
        ),
    ),
    DataSubjectRight(
        id="objection",
        label="Right to object",
        article="LGPD Article 18",
        summary=(
            "Data subjects may object to processing carried out without "
            "consent where the processing does not comply with LGPD."
        ),
        request_prompt=(
            "Submit an objection request and identify the non-compliant "
            "processing activity."
        ),
    ),
    DataSubjectRight(
        id="automated_decision_review",
        label="Right to review automated decisions",
        article="LGPD Article 20",
        summary=(
            "Data subjects may request review of decisions made solely on "
            "automated processing of personal data that affect their interests."
        ),
        request_prompt=(
            "Submit an automated-decision review request and identify the "
            "decision, account, and service context."
        ),
    ),
)


PIPEDA_RIGHTS = (
    DataSubjectRight(
        id="access",
        label="Right of access",
        article="PIPEDA Principle 9",
        summary=(
            "Individuals may request information about the existence, use, "
            "and disclosure of their personal information and receive access "
            "to that information, subject to PIPEDA exceptions."
        ),
        request_prompt=(
            "Submit an access request in writing through the PIPEDA request "
            "path or privacy contact."
        ),
    ),
    DataSubjectRight(
        id="correction",
        label="Right to correction or notation",
        article="PIPEDA Principle 9",
        summary=(
            "Individuals may challenge the accuracy and completeness of "
            "personal information and have it amended as appropriate; "
            "unresolved challenges should be recorded where appropriate."
        ),
        request_prompt=(
            "Submit a correction request and identify the incomplete, "
            "inaccurate, or outdated account or service data."
        ),
    ),
    DataSubjectRight(
        id="withdraw_consent",
        label="Right to withdraw consent",
        article="PIPEDA Principle 3",
        summary=(
            "Individuals may withdraw consent at any time, subject to legal "
            "or contractual restrictions and reasonable notice."
        ),
        request_prompt=(
            "Use the consent-management path or contact the privacy team; "
            "the service should explain the implications of withdrawal."
        ),
    ),
    DataSubjectRight(
        id="challenge_compliance",
        label="Right to challenge compliance",
        article="PIPEDA Principle 10",
        summary=(
            "Individuals may challenge an organization's compliance with "
            "PIPEDA's fair information principles through the person "
            "accountable for privacy compliance."
        ),
        request_prompt=(
            "Submit a compliance challenge to the privacy officer or contact "
            "the Office of the Privacy Commissioner of Canada where appropriate."
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
    sdk_dependencies: Iterable[str] = (),
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
    inferred_clauses = infer_privacy_notice_clauses(sdk_dependencies)
    sections = sections + _sdk_clauses_sections(inferred_clauses)
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_GDPR,
        title="GDPR Privacy Notice Template",
        sections=sections,
        rights=GDPR_RIGHTS,
        inferred_clauses=inferred_clauses,
    )


def build_ccpa_privacy_notice(
    *,
    business_name: str = "{{ business_name }}",
    privacy_contact: str = "{{ privacy_contact }}",
    ccpa_request_endpoint: str = "{{ ccpa_request_endpoint }}",
    do_not_sell_or_share_link: str = "{{ do_not_sell_or_share_link }}",
    limit_sensitive_pi_link: str = "{{ limit_sensitive_pi_link }}",
    effective_date: str = "{{ effective_date }}",
    sdk_dependencies: Iterable[str] = (),
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
    inferred_clauses = infer_privacy_notice_clauses(sdk_dependencies)
    sections = sections + _sdk_clauses_sections(inferred_clauses)
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_CCPA,
        title="CCPA Privacy Notice Template",
        sections=sections,
        rights=CCPA_RIGHTS,
        inferred_clauses=inferred_clauses,
    )


def build_pipl_privacy_notice(
    *,
    processor_name: str = "{{ processor_name }}",
    privacy_contact: str = "{{ privacy_contact }}",
    pipl_request_endpoint: str = "{{ pipl_request_endpoint }}",
    china_representative_contact: str = "{{ china_representative_contact }}",
    effective_date: str = "{{ effective_date }}",
    sdk_dependencies: Iterable[str] = (),
) -> PrivacyNoticeTemplate:
    """Build the PIPL privacy-notice markdown template."""

    sections = (
        PrivacyNoticeSection(
            id="scope",
            title="Scope",
            body=(
                f"This notice explains how {processor_name} processes "
                "personal information of individuals in the People's Republic "
                "of China under the Personal Information Protection Law. It "
                "is a template and must be reviewed against the generated "
                "application's actual data flows before publication.\n\n"
                f"Effective date: {effective_date}."
            ),
        ),
        PrivacyNoticeSection(
            id="processor",
            title="Personal Information Processor and Contacts",
            body=(
                f"Personal information processor: {processor_name}.\n\n"
                f"Privacy contact: {privacy_contact}.\n\n"
                "China representative or specialised agency contact, if "
                "required for an overseas processor: "
                f"{china_representative_contact}.\n\n"
                f"PIPL request path: {pipl_request_endpoint}."
            ),
        ),
        PrivacyNoticeSection(
            id="categories",
            title="Personal Information We Process",
            body=(
                "List each personal-information category processed, including "
                "account profile data, authentication identifiers, billing "
                "records, support messages, device or log data, integration "
                "data received from connected third-party services, and any "
                "sensitive personal information. For sensitive personal "
                "information, document the specific purpose, necessity, and "
                "impact on individual rights and interests."
            ),
        ),
        PrivacyNoticeSection(
            id="purposes_methods",
            title="Purposes, Methods, and Retention",
            body=(
                "Document each processing purpose, processing method, and "
                "retention period. Personal information should be retained "
                "for the shortest period necessary to achieve the processing "
                "purpose, unless a longer period is required by law or "
                "administrative regulation."
            ),
        ),
        PrivacyNoticeSection(
            id="legal_basis_consent",
            title="Legal Basis, Consent, and Separate Consent",
            body=(
                "Document the legal basis for each processing activity. Where "
                "PIPL requires consent or separate consent, including for "
                "sensitive personal information, public disclosure, certain "
                "third-party sharing, or cross-border transfer, record the "
                "consent path and withdrawal method."
            ),
        ),
        PrivacyNoticeSection(
            id="rights",
            title="Your PIPL Rights",
            body=_rights_markdown(PIPL_RIGHTS),
        ),
        PrivacyNoticeSection(
            id="requests",
            title="How to Exercise Your Rights",
            body=(
                f"Submit PIPL requests through {pipl_request_endpoint} or by "
                f"contacting {privacy_contact}. We verify requester identity "
                "before disclosing, copying, transferring, correcting, "
                "deleting, or explaining personal information processing "
                "rules. We respond within the applicable legal deadline and "
                "will explain the reason if a request is rejected."
            ),
        ),
        PrivacyNoticeSection(
            id="third_parties",
            title="Third-Party Sharing and Entrusted Processing",
            body=(
                "List entrusted processors, third-party recipients, and joint "
                "processing arrangements. For each recipient, document its "
                "name or category, contact method where required, processing "
                "purpose, processing method, and personal-information category."
            ),
        ),
        PrivacyNoticeSection(
            id="cross_border",
            title="Cross-Border Transfers",
            body=(
                "If personal information is provided outside mainland China, "
                "document the overseas recipient's name and contact method, "
                "processing purpose and method, personal-information "
                "categories, the method and procedure for individuals to "
                "exercise PIPL rights over the overseas recipient, the "
                "separate consent path, and the applicable transfer mechanism."
            ),
        ),
    )
    inferred_clauses = infer_privacy_notice_clauses(sdk_dependencies)
    sections = sections + _sdk_clauses_sections(inferred_clauses)
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_PIPL,
        title="PIPL Privacy Notice Template",
        sections=sections,
        rights=PIPL_RIGHTS,
        inferred_clauses=inferred_clauses,
    )


def build_lgpd_privacy_notice(
    *,
    controller_name: str = "{{ controller_name }}",
    privacy_contact: str = "{{ privacy_contact }}",
    dpo_contact: str = "{{ dpo_contact }}",
    lgpd_request_endpoint: str = "{{ lgpd_request_endpoint }}",
    effective_date: str = "{{ effective_date }}",
    sdk_dependencies: Iterable[str] = (),
) -> PrivacyNoticeTemplate:
    """Build the LGPD privacy-notice markdown template."""

    sections = (
        PrivacyNoticeSection(
            id="scope",
            title="Scope",
            body=(
                f"This notice explains how {controller_name} processes "
                "personal data of data subjects in Brazil under the Lei Geral "
                "de Protecao de Dados Pessoais (LGPD). It is a template and "
                "must be reviewed against the generated application's actual "
                "data flows before publication.\n\n"
                f"Effective date: {effective_date}."
            ),
        ),
        PrivacyNoticeSection(
            id="controller",
            title="Controller, DPO, and Contacts",
            body=(
                f"Controller: {controller_name}.\n\n"
                f"Privacy contact: {privacy_contact}.\n\n"
                f"Data protection officer contact: {dpo_contact}.\n\n"
                f"LGPD request path: {lgpd_request_endpoint}."
            ),
        ),
        PrivacyNoticeSection(
            id="categories",
            title="Personal Data We Process",
            body=(
                "List each personal-data category processed, including "
                "account profile data, authentication identifiers, billing "
                "records, support messages, device or log data, integration "
                "data received from connected third-party services, and any "
                "sensitive personal data. For sensitive personal data, "
                "document the specific purpose and applicable LGPD basis."
            ),
        ),
        PrivacyNoticeSection(
            id="purposes_legal_bases",
            title="Purposes and Legal Bases",
            body=(
                "Document each processing purpose and legal basis, including "
                "contract performance, consent where required, compliance "
                "with legal or regulatory obligations, regular exercise of "
                "rights, legitimate interests, fraud prevention, security "
                "monitoring, service analytics, and customer support."
            ),
        ),
        PrivacyNoticeSection(
            id="rights",
            title="Your LGPD Rights",
            body=_rights_markdown(LGPD_RIGHTS),
        ),
        PrivacyNoticeSection(
            id="requests",
            title="How to Exercise Your Rights",
            body=(
                f"Submit LGPD requests through {lgpd_request_endpoint} or by "
                f"contacting {privacy_contact}. We verify requester identity "
                "before confirming processing, disclosing, correcting, "
                "anonymizing, blocking, deleting, porting, or reviewing "
                "personal data. Confirmation of processing or access is "
                "provided immediately in simplified form where available, or "
                "through a clear and complete statement within "
                f"{LGPD_ACCESS_RESPONSE_DEADLINE}. If immediate action is not "
                "possible, we explain the factual or legal reasons."
            ),
        ),
        PrivacyNoticeSection(
            id="sharing_processors",
            title="Sharing, Operators, and Joint Use",
            body=(
                "List operators, public or private entities, and third-party "
                "recipients that receive personal data. For each recipient, "
                "document the personal-data category, processing purpose, "
                "sharing or operator role, and whether the recipient is "
                "inside or outside Brazil."
            ),
        ),
        PrivacyNoticeSection(
            id="international_transfers",
            title="International Transfers",
            body=(
                "If personal data is transferred outside Brazil, document the "
                "destination, recipient, transfer purpose, personal-data "
                "categories, and applicable LGPD transfer mechanism, such as "
                "adequacy, contractual safeguards, global corporate rules, "
                "ANPD-authorised clauses, consent, or another lawful basis."
            ),
        ),
        PrivacyNoticeSection(
            id="retention",
            title="Retention and Deletion",
            body=(
                "List retention periods for each data category. Personal data "
                "should be retained only as long as needed for the stated "
                "purposes, unless longer retention is required for legal, "
                "regulatory, security, billing, dispute, anonymization, or "
                "audit obligations."
            ),
        ),
    )
    inferred_clauses = infer_privacy_notice_clauses(sdk_dependencies)
    sections = sections + _sdk_clauses_sections(inferred_clauses)
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_LGPD,
        title="LGPD Privacy Notice Template",
        sections=sections,
        rights=LGPD_RIGHTS,
        inferred_clauses=inferred_clauses,
    )


def build_pipeda_privacy_notice(
    *,
    organization_name: str = "{{ organization_name }}",
    privacy_contact: str = "{{ privacy_contact }}",
    privacy_officer_contact: str = "{{ privacy_officer_contact }}",
    pipeda_request_endpoint: str = "{{ pipeda_request_endpoint }}",
    effective_date: str = "{{ effective_date }}",
    sdk_dependencies: Iterable[str] = (),
) -> PrivacyNoticeTemplate:
    """Build the PIPEDA privacy-notice markdown template."""

    sections = (
        PrivacyNoticeSection(
            id="scope",
            title="Scope",
            body=(
                f"This notice explains how {organization_name} collects, "
                "uses, and discloses personal information under Canada's "
                "Personal Information Protection and Electronic Documents Act "
                "(PIPEDA). It is a template and must be reviewed against the "
                "generated application's actual data flows before publication.\n\n"
                f"Effective date: {effective_date}."
            ),
        ),
        PrivacyNoticeSection(
            id="organization",
            title="Organization, Privacy Officer, and Contacts",
            body=(
                f"Organization: {organization_name}.\n\n"
                f"Privacy contact: {privacy_contact}.\n\n"
                f"Person accountable for PIPEDA compliance: "
                f"{privacy_officer_contact}.\n\n"
                f"PIPEDA request path: {pipeda_request_endpoint}."
            ),
        ),
        PrivacyNoticeSection(
            id="categories",
            title="Personal Information We Collect",
            body=(
                "List each personal-information category collected, including "
                "account profile data, authentication identifiers, billing "
                "records, support messages, device or log data, integration "
                "data received from connected third-party services, and any "
                "sensitive personal information. Collection should be limited "
                "to information needed for identified purposes and collected "
                "by fair and lawful means."
            ),
        ),
        PrivacyNoticeSection(
            id="purposes_consent",
            title="Purposes, Consent, and Choices",
            body=(
                "Document each purpose for collection, use, and disclosure "
                "before or at the time of collection. Explain the consent "
                "path, whether express or implied consent is used, what "
                "personal information is collected, which parties receive it, "
                "the purposes, and any risks of harm or other consequences. "
                "For non-integral collections, uses, or disclosures, provide "
                "a clear and accessible choice."
            ),
        ),
        PrivacyNoticeSection(
            id="rights",
            title="Your PIPEDA Rights",
            body=_rights_markdown(PIPEDA_RIGHTS),
        ),
        PrivacyNoticeSection(
            id="requests",
            title="How to Exercise Your Rights",
            body=(
                f"Submit PIPEDA requests through {pipeda_request_endpoint} or "
                f"by contacting {privacy_contact}. We verify requester "
                "identity before disclosing, correcting, or annotating "
                "personal information. We respond to access requests within "
                f"{PIPEDA_ACCESS_RESPONSE_DEADLINE} unless a PIPEDA extension "
                "applies; permitted extensions may allow up to "
                f"{PIPEDA_ACCESS_EXTENSION_DEADLINE} or the time necessary to "
                "convert information into an alternative format. If access is "
                "refused or delayed, we explain the reason and the right to "
                "complain to the Office of the Privacy Commissioner of Canada."
            ),
        ),
        PrivacyNoticeSection(
            id="sharing_service_providers",
            title="Disclosure and Service Providers",
            body=(
                "List third parties, service providers, affiliates, or public "
                "authorities that receive personal information. For each "
                "recipient or category, document the personal-information "
                "categories, disclosure purpose, whether consent is required, "
                "and safeguards used to protect information under the "
                "organization's control."
            ),
        ),
        PrivacyNoticeSection(
            id="retention_accuracy_safeguards",
            title="Retention, Accuracy, and Safeguards",
            body=(
                "List retention periods for each data category. Personal "
                "information should be kept only as long as required to serve "
                "the identified purposes, unless longer retention is required "
                "for legal, security, billing, dispute, or audit obligations. "
                "Document accuracy controls and safeguards appropriate to the "
                "sensitivity of the personal information."
            ),
        ),
        PrivacyNoticeSection(
            id="openness_complaints",
            title="Openness and Complaints",
            body=(
                "Make information about privacy policies and practices readily "
                "available, including how to contact the person accountable "
                "for PIPEDA compliance. Individuals may challenge compliance "
                f"through {privacy_officer_contact} and may contact the Office "
                "of the Privacy Commissioner of Canada if the issue remains "
                "unresolved."
            ),
        ),
    )
    inferred_clauses = infer_privacy_notice_clauses(sdk_dependencies)
    sections = sections + _sdk_clauses_sections(inferred_clauses)
    return PrivacyNoticeTemplate(
        jurisdiction=JURISDICTION_PIPEDA,
        title="PIPEDA Privacy Notice Template",
        sections=sections,
        rights=PIPEDA_RIGHTS,
        inferred_clauses=inferred_clauses,
    )


def infer_privacy_notice_clauses(
    sdk_dependencies: Iterable[str],
) -> tuple[SdkPrivacyClause, ...]:
    """Infer notice clauses from SDK or third-party dependency identifiers."""

    dependencies = tuple(str(dep).strip() for dep in sdk_dependencies if str(dep).strip())
    clauses: list[SdkPrivacyClause] = []
    for rule in SDK_PRIVACY_CLAUSE_RULES:
        matched = tuple(
            dep for dep in dependencies if _matches_sdk_clause_rule(dep, rule)
        )
        if matched:
            clauses.append(
                SdkPrivacyClause(
                    id=rule.id,
                    title=rule.title,
                    body=rule.body,
                    matched_dependencies=matched,
                )
            )
    return tuple(clauses)


def _matches_sdk_clause_rule(dep: str, rule: SdkPrivacyClauseRule) -> bool:
    dep_lc = dep.lower()
    dep_group = dep_lc.split(":", 1)[0] if ":" in dep_lc else ""
    dep_subspec = dep_lc.split("/", 1)[0] if "/" in dep_lc else ""
    for ident in rule.identifiers:
        ident_lc = ident.lower()
        if dep_lc == ident_lc:
            return True
        if dep_lc.startswith(ident_lc):
            return True
        if dep_subspec and dep_subspec == ident_lc:
            return True
        if dep_group and ":" not in ident_lc and dep_group == ident_lc:
            return True
    return False


def _sdk_clauses_sections(
    clauses: tuple[SdkPrivacyClause, ...],
) -> tuple[PrivacyNoticeSection, ...]:
    if not clauses:
        return ()
    return (
        PrivacyNoticeSection(
            id="sdk_third_party_clauses",
            title="SDK and Third-Party Clauses",
            body=_sdk_clauses_markdown(clauses),
        ),
    )


def _sdk_clauses_markdown(clauses: tuple[SdkPrivacyClause, ...]) -> str:
    lines: list[str] = []
    for clause in clauses:
        dependencies = ", ".join(clause.matched_dependencies)
        lines.append(
            f"- **{clause.title}** ({dependencies}): {clause.body}"
        )
    return "\n".join(lines)


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
    "JURISDICTION_LGPD",
    "JURISDICTION_PIPEDA",
    "JURISDICTION_PIPL",
    "LGPD_ACCESS_RESPONSE_DEADLINE",
    "LGPD_RIGHTS",
    "PIPEDA_ACCESS_EXTENSION_DEADLINE",
    "PIPEDA_ACCESS_RESPONSE_DEADLINE",
    "PIPEDA_RIGHTS",
    "PIPL_RIGHTS",
    "SDK_PRIVACY_CLAUSE_RULES",
    "DataSubjectRight",
    "PrivacyNoticeSection",
    "PrivacyNoticeTemplate",
    "SdkPrivacyClause",
    "SdkPrivacyClauseRule",
    "build_ccpa_privacy_notice",
    "build_gdpr_privacy_notice",
    "build_lgpd_privacy_notice",
    "build_pipeda_privacy_notice",
    "build_pipl_privacy_notice",
    "infer_privacy_notice_clauses",
)
