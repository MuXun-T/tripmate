from app.api import app


def test_openapi_exposes_authenticated_p0_routes_and_health() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    required = {
        "/health/live",
        "/health/ready",
        "/v1/trips",
        "/v1/conversations/{conversation_id}/memory",
        "/v1/conversations/{conversation_id}/messages",
        "/v1/actions/{action_id}/confirmation",
        "/v1/reservations",
        "/v1/reservations/{reservation_id}/confirm",
        "/v1/reservations/{reservation_id}/cancel",
        "/v1/knowledge/search",
    }
    assert required <= paths.keys()
    assert schema["components"]["securitySchemes"]["TripMateBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert paths["/v1/reservations"]["post"]["security"] == [{"TripMateBearer": []}]


def test_openapi_reservation_input_does_not_accept_authority_fields() -> None:
    schema = app.openapi()
    body = schema["paths"]["/v1/reservations"]["post"]["requestBody"]
    component = body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    fields = schema["components"]["schemas"][component]["properties"]

    assert "tenant_id" not in fields
    assert "user_id" not in fields
    assert "price" not in fields
    assert "status" not in fields
