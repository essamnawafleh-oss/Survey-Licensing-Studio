"""Application error hierarchy.

Errors carry a stable ``code`` so the UI can translate them and tests can assert on
them without matching message text.
"""
from __future__ import annotations


class SlsError(Exception):
    """Base class for all expected, user-reportable errors."""

    code = "error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(SlsError):
    code = "validation"


class UnitsNotVerifiedError(SlsError):
    code = "units_not_verified"


class CrsError(SlsError):
    code = "crs"


class GeometryError(SlsError):
    code = "geometry"


class MissingRuleError(SlsError):
    code = "missing_rule"


class AmbiguousRuleError(SlsError):
    code = "ambiguous_rule"


class RuleImportError(SlsError):
    code = "rule_import"


class CadImportError(SlsError):
    code = "cad_import"


class DwgUnavailableError(SlsError):
    code = "dwg_unavailable"


class PermissionDeniedError(SlsError):
    code = "permission_denied"


class StorageError(SlsError):
    code = "storage"


class ReportError(SlsError):
    code = "report"
