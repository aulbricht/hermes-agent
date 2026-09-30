from types import SimpleNamespace

from agent.conversation_loop import _atlas_route_usage_fields


def test_route_usage_fields_link_denied_sol_fallback_to_luna():
    agent = SimpleNamespace(
        _atlas_route_request_id="route-turn-123",
        _atlas_sol_last_call={
            "route_request_id": "route-turn-123",
            "reservation_id": None,
            "actual_model": "gpt-6-luna",
        },
    )

    assert _atlas_route_usage_fields(agent) == {
        "route_request_id": "route-turn-123",
        "model": "gpt-6-luna",
        "reservation_id": None,
    }


def test_route_usage_fields_keep_sol_reservation_maximum_and_settlement():
    agent = SimpleNamespace(
        _atlas_route_request_id="route-turn-456",
        _atlas_sol_last_call={
            "route_request_id": "route-turn-456",
            "reservation_id": "sol-reservation-1",
            "max_usd": "0.30",
            "settled_usd": "0.12",
            "actual_model": "gpt-6.1-sol",
        },
    )

    assert _atlas_route_usage_fields(agent) == {
        "route_request_id": "route-turn-456",
        "model": "gpt-6.1-sol",
        "reservation_id": "sol-reservation-1",
        "maximum_usd": "0.30",
        "settled_usd": "0.12",
    }
