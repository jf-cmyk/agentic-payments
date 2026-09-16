"""Clerk provider extension, loaded only when agent registration is enabled."""
import os

from fastmcp.server.auth.providers.clerk import ClerkProvider

from src.agent_auth import AgentAuth, MetadataDispatch, TokenDispatch


class AgentClerkProvider(ClerkProvider):
    def __init__(self, **kwargs):
        issuer = kwargs["base_url"].rstrip("/")
        redirects = kwargs.get("allowed_client_redirect_uris")
        if redirects is not None:
            kwargs["allowed_client_redirect_uris"] = [*redirects, issuer + "/agent/callback"]
        super().__init__(**kwargs)
        self.agent_auth = AgentAuth(
            self, issuer=issuer, secret=os.environ.get("AGENT_AUTH_SECRET", ""),
            path=os.environ.get("AGENT_AUTH_DB_PATH", "/data/agent_auth.sqlite3"),
            scopes=kwargs["required_scopes"])

    async def load_original_token(self, token):
        return await super().load_access_token(token)

    async def load_access_token(self, token):
        if token.startswith("bsa_"):
            return await self.agent_auth.delegated_token(token)
        return await self.load_original_token(token)

    def get_routes(self, mcp_path=None):
        routes = super().get_routes(mcp_path)
        for route in routes:
            if route.path == "/token":
                route.app = TokenDispatch(route.app, self.agent_auth)
            elif route.path.startswith("/.well-known/oauth-authorization-server"):
                route.app = MetadataDispatch(route.app, self.agent_auth)
        return [*routes, *self.agent_auth.routes()]
