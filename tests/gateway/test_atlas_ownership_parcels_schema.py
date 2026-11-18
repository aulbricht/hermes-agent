"""Operation coupling survives the normal discovery schema sanitizer."""

import jsonschema
import pytest

from model_tools import get_tool_definitions
from tools import atlas_ownership_tool as tool


@pytest.mark.parametrize("operation,arguments,accepted", [
    ("evidence", {"offset": 0, "limit": 50}, True),
    ("catalog", {}, True),
    ("parcels_within_radius", {"address": "Exact source address", "radius": 1.5, "unit": "mi"}, True),
    ("parcels_within_radius", {"latitude": 0, "longitude": 0, "radius": 1, "unit": "ft"}, True),
    ("parcels_within_radius", {}, False),
    ("parcels_within_radius", {"offset": 0, "limit": 50}, False),
    ("evidence", {"address": "Exact source address", "radius": 1.5, "unit": "mi"}, False),
    ("catalog", {"latitude": 0, "longitude": 0, "radius": 1, "unit": "ft"}, False),
])
def test_discovered_arguments_belong_to_operation(operation, arguments, accepted):
    reset = tool.bind_ownership_capability("atown_" + "s" * 48)
    try:
        definitions = get_tool_definitions(enabled_toolsets=["atlas_ownership"], quiet_mode=True,
                                           skip_tool_search_assembly=True)
        schema = next(item["function"]["parameters"] for item in definitions
                      if item["function"]["name"] == "atlas_ownership")
        request = {"operation": operation, "arguments": arguments}
        if accepted:
            jsonschema.validate(request, schema)
        else:
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(request, schema)
    finally:
        tool.clear_ownership_capability(reset)
