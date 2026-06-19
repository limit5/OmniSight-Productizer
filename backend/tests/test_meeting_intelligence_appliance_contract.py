"""Wire-format contract lock between the conference-appliance MI lane and the
backend BI0 ingest endpoint.

The board (conference-appliance ``src/transcript_ingest/transcript_ingest.c``,
``conf_ti_build_body`` / ``conf_ti_append_segment``) serialises transcript
batches by hand and POSTs them to ``{base}/meetings/{id}/segments`` with an
``Authorization: Bearer <token>`` header. This test feeds the EXACT byte shape
that C code emits into the backend Pydantic models so any drift on either side
(a renamed/added-required field, a type change) fails here instead of silently
422-ing a live appliance in the field.

The literal below mirrors conf_ti_append_segment field order:
  {"segment_seq":N,"start_ms":N,"end_ms":N,"text":..,"language":..,
   "confidence":F.FFF,"is_final":bool,"source":..}
wrapped in conf_ti_build_body: {"session_id":..,"segments":[...]}

No PG / no LLM — pure contract validation, always runs.
"""

from __future__ import annotations

from backend.models import TranscriptIngestRequest

# Byte-for-byte representative of what the appliance emits for a 2-segment
# batch (one partial caption, one finalised line).
APPLIANCE_BATCH_JSON = (
    '{"session_id":"sess-5f3c",'
    '"segments":['
    '{"segment_seq":0,"start_ms":0,"end_ms":1200,'
    '"text":"Mr. Quilter is the apostle of the middle classes",'
    '"language":"en","confidence":0.500,"is_final":false,'
    '"source":"onboard-rknn"},'
    '{"segment_seq":1,"start_ms":1200,"end_ms":2600,'
    '"text":"and we are glad to welcome his gospel.",'
    '"language":"en","confidence":0.820,"is_final":true,'
    '"source":"onboard-rknn"}'
    ']}'
)


def test_appliance_batch_validates_into_backend_model():
    req = TranscriptIngestRequest.model_validate_json(APPLIANCE_BATCH_JSON)
    assert req.session_id == "sess-5f3c"
    assert len(req.segments) == 2

    partial, final = req.segments
    assert partial.segment_seq == 0 and partial.is_final is False
    assert partial.source == "onboard-rknn"
    assert partial.start_ms == 0 and partial.end_ms == 1200
    assert partial.language == "en"
    assert abs(partial.confidence - 0.5) < 1e-6
    assert final.segment_seq == 1 and final.is_final is True
    assert final.text == "and we are glad to welcome his gospel."


def test_backend_required_fields_are_all_emitted_by_appliance():
    """If the backend makes a NEW field required, this fails until the
    appliance's conf_ti_append_segment is updated to emit it."""
    req = TranscriptIngestRequest.model_validate_json(APPLIANCE_BATCH_JSON)
    seg = req.segments[0]
    # The appliance emits exactly these keys; assert the backend accepts a
    # segment built from only them (the round-trip the wire carries).
    rebuilt = type(seg).model_validate({
        "segment_seq": seg.segment_seq,
        "text": seg.text,
        "is_final": seg.is_final,
        "source": seg.source,
        "start_ms": seg.start_ms,
        "end_ms": seg.end_ms,
        "language": seg.language,
        "confidence": seg.confidence,
    })
    assert rebuilt.source == "onboard-rknn"


def test_confidence_bounds_match_appliance_range():
    # appliance writes %.3f in [0,1]; backend constrains ge=0 le=1.
    ok = TranscriptIngestRequest.model_validate_json(
        '{"session_id":"s","segments":[{"segment_seq":0,"text":"x",'
        '"is_final":true,"source":"onboard-rknn","confidence":1.000}]}')
    assert ok.segments[0].confidence == 1.0
