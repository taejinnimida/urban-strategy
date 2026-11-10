"""Transport-neutral urban FACT envelope; never contains planning eligibility.

A missing source is UNKNOWN, not an empty or non-applicable property.
Jurisdiction-specific legal decisions belong in downstream consumers.
"""
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
from typing import Any, Literal

FactStatus = Literal['COMPLETE', 'PARTIAL', 'REFERENCE', 'UNAVAILABLE', 'ERROR']

@dataclass(frozen=True)
class FactEnvelope:
    kind: str
    status: FactStatus
    source: str
    as_of: str | None
    geometry_crs: str
    coverage: str
    facts: dict[str, Any] | None = None
    source_revision: str | None = None

    def serialize(self) -> dict[str, Any]:
        # Detached payload prevents a downstream business RULE from mutating FACT.
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))


def fact_revision(boundary: dict, kind: str, source_revision: str | None) -> str:
    """Stable revision of source and boundary only, not a scheme's evaluation."""
    payload = {'boundary':boundary,'kind':kind,'source_revision':source_revision}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return sha256(encoded.encode('utf-8')).hexdigest()


def unavailable(kind: str, source: str = '미확보') -> FactEnvelope:
    return FactEnvelope(kind=kind,status='UNAVAILABLE',source=source,
                        as_of=None,geometry_crs='EPSG:4326',
                        coverage='NOT_VERIFIED',facts=None)
