"""
A minimal stand-in for dspace_rest_client's SimpleDSpaceObject, faithful to
its real metadata shape: get_metadata_values(field) returns a list of
{"value": ..., "language": ..., "authority": ..., "confidence": ...,
"place": ...} dicts, not plain strings. Tests build fixtures with this class
specifically so they'd catch a regression if normalize.py ever went back to
trusting the library's misleading docstring.
"""


class FakeDSO:
    def __init__(
        self,
        handle: str,
        uuid: str,
        fields: dict[str, list[str]],
        *,
        embedded: dict | None = None,
        authorities: dict[str, list[str | None]] | None = None,
    ):
        """
        @param fields: mapping of field name → list of plain string values.
        @param embedded: optional embedded data dict, e.g.
            {"owningCollection": {"uuid": "...", "name": "..."}}
        @param authorities: optional mapping of field name → list of authority
            values (same length/order as the corresponding field values).
            Use this to simulate per-author CRIS Person UUIDs.
        """
        self.handle = handle
        self.uuid = uuid
        self.embedded = embedded or {}
        self.metadata = {}
        for field, values in fields.items():
            field_authorities = (authorities or {}).get(field, [None] * len(values))
            self.metadata[field] = [
                {
                    "value": v,
                    "language": None,
                    "authority": field_authorities[i] if i < len(field_authorities) else None,
                    "confidence": -1,
                    "place": i,
                }
                for i, v in enumerate(values)
            ]

    def get_metadata_values(self, field: str) -> list[dict]:
        return self.metadata.get(field, [])
