"""Inspection contracts shared by production-line vision components."""

from backend.inspection.verdict import Defect, InspectionVerdict, verdict_json_schema

__all__ = ["Defect", "InspectionVerdict", "verdict_json_schema"]
