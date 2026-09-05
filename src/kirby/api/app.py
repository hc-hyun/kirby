"""Role-specific A2A endpoints and a small authenticated role catalog."""

from copy import deepcopy
from functools import partial

from a2a import types as t
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from kirby.api.auth import AccessMiddleware, IdentityContext
from kirby.api.files import FILE_TYPES, file_routes
from kirby.api.handler import TaskHandler
from kirby.contracts import Principal


def create_app(
    store,
    roles: dict[str, dict],
    credentials: dict[str, Principal],
    *,
    public_url: str = "http://127.0.0.1:8000",
    poll_interval: float = 0.2,
    object_store=None,
) -> Starlette:
    """The caller owns storage lifecycle and supplies trusted role manifests."""
    if not credentials or any(not token for token in credentials):
        raise ValueError("Explicit bearer credentials are required")
    if not roles or poll_interval <= 0:
        raise ValueError("Roles and a positive polling interval are required")
    roles = deepcopy(roles)
    cards = {}

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def ready(request):
        try:
            healthy = await store.health()
        except Exception:
            healthy = False
        return JSONResponse(
            {"status": "ready" if healthy else "unavailable"},
            status_code=200 if healthy else 503,
        )

    async def catalog(request):
        principal = request.scope["kirby_principal"]
        return JSONResponse(
            {
                "agents": [
                    {
                        "id": role_id,
                        "name": card.name,
                        "card_url": (
                            f"{public_url.rstrip('/')}/agents/{role_id}"
                            "/.well-known/agent-card.json"
                        ),
                        "rpc_url": card.supported_interfaces[0].url,
                    }
                    for role_id, card in cards.items()
                    if role_id in principal.roles
                ]
            }
        )

    async def card_response(request, role_id):
        if role_id not in request.scope["kirby_principal"].roles:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(MessageToDict(cards[role_id]))

    routes = [
        Route("/healthz", health),
        Route("/readyz", ready),
        Route("/agents", catalog),
    ]
    for role_id, manifest in roles.items():
        profile = manifest["profile"]
        prefix = f"/agents/{role_id}"
        cards[role_id] = t.AgentCard(
            name=f"KIRBY {role_id}",
            description=profile["description"],
            version=profile["version"],
            supported_interfaces=[
                t.AgentInterface(
                    url=f"{public_url.rstrip('/')}{prefix}/rpc",
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                )
            ],
            capabilities=t.AgentCapabilities(
                streaming=True, push_notifications=False, extended_agent_card=False
            ),
            security_schemes={
                "bearer": t.SecurityScheme(
                    http_auth_security_scheme=t.HTTPAuthSecurityScheme(scheme="bearer")
                )
            },
            security_requirements=[
                t.SecurityRequirement(schemes={"bearer": t.StringList()})
            ],
            default_input_modes=(
                sorted(set(FILE_TYPES.values())) if object_store else ["text/plain"]
            ),
            default_output_modes=(
                ["application/json", "text/csv"]
                if object_store
                else ["application/json"]
            ),
            skills=[
                t.AgentSkill(**skill) for skill in profile["a2a"]["advertised_skills"]
            ],
        )
        routes.append(
            Route(
                f"{prefix}/.well-known/agent-card.json",
                partial(card_response, role_id=role_id),
            )
        )
        routes.extend(file_routes(store, object_store, public_url, role_id))
        routes.extend(
            create_jsonrpc_routes(
                TaskHandler(
                    store,
                    role_id,
                    manifest,
                    poll_interval,
                    object_store=object_store,
                    public_url=public_url,
                ),
                f"{prefix}/rpc",
                IdentityContext(),
                enable_v0_3_compat=False,
            )
        )

    app = Starlette(routes=routes)
    app.add_middleware(
        AccessMiddleware,
        credentials=dict(credentials),
        max_request_bytes=max(
            manifest["profile"]["execution"]["max_input_bytes"]
            for manifest in roles.values()
        )
        * 6
        + 65536,
    )
    return app
