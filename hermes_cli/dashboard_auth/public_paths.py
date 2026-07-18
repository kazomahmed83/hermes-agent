"""Shared allowlist of ``/api/*`` paths that bypass dashboard auth.

Two middlewares enforce dashboard auth and previously kept independent
copies of this list:

* ``hermes_cli.web_server.auth_middleware`` — loopback / ``--insecure``
  mode, gates on the ephemeral ``_SESSION_TOKEN``.
* ``hermes_cli.dashboard_auth.middleware.gated_auth_middleware`` —
  non-loopback mode, gates on the OAuth session cookie.

When the lists drifted, ``/api/status`` ended up public under the legacy
gate but 401'd under the OAuth gate. That broke the portal's wildcard
liveness probe (``nous-account-service`` ``fly-provider.ts``
``getInstanceRuntimeStatus``), which fetches ``/api/status`` without a
cookie as its sole signal of "agent dashboard is alive": every healthy
wildcard-subdomain agent surfaced as STARTING/down in the portal UI even
though the dashboard was serving correctly.

Centralising the allowlist here so both middlewares import the same
frozenset prevents the next drift. Keep this list minimal — only truly
non-sensitive, read-only endpoints belong here. As a sanity check, every
entry should be safe to expose to:

  * external uptime probes (Pingdom, Better Stack, NAS),
  * the dashboard SPA before the user has logged in,
  * anyone who happens to ``curl`` the hostname.

If a new endpoint doesn't pass all three tests, it should be gated and
the SPA should bootstrap it after login instead.
"""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # Liveness probe target. Returns version, gateway state, active
    # session count, and the dashboard auth-gate shape. No bodies, no
    # session content, no secrets. Documented as the portal's wildcard
    # liveness probe in
    # ``docs/agent-dashboard-public-url-contract.md`` (NAS side).
    "/api/status",
    # Read-only config-defaults / schema feeds for the SPA's Config page.
    "/api/config/defaults",
    "/api/config/schema",
    # Read-only model metadata (context windows, etc.) — same shape as
    # provider catalogs already exposed on the public internet.
    "/api/model/info",
    # Read-only theme + plugin manifests for the dashboard skin engine.
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    # Chronos managed-cron fire webhook (NAS -> agent). NOT cookie-gated: it
    # carries its own short-lived NAS-minted JWT (purpose=cron_fire), which the
    # handler verifies as the real auth. Must bypass the dashboard auth gate so
    # the NAS relay's bearer-only callback reaches the verifier instead of a
    # 401 no_cookie. The JWT — not this allowlist — is the security boundary.
    "/api/cron/fire",

    # One AI Employee GHL Marketplace integration endpoints. These are public
    # because GoHighLevel calls them server-to-server or loads them inside
    # HighLevel. Sensitive routes carry their own OAuth/signature/secret
    # boundary; do not broaden this to a prefix.
    "/api/plugins/one-ai-employee/ghl/health",
    "/api/plugins/one-ai-employee/ghl/registry",
    "/api/plugins/one-ai-employee/ghl/oauth/callback",
    "/api/plugins/one-ai-employee/ghl/webhooks",
    "/api/plugins/one-ai-employee/ghl/webhooks/workflow-action",
    "/api/plugins/one-ai-employee/ghl/webhooks/trigger-subscription",
    "/api/plugins/one-ai-employee/ghl/dashboard",
    "/api/plugins/one-ai-employee/ghl/custom.js",

    "/api/plugins/one-ai-employee/connect/health",
    "/api/plugins/one-ai-employee/connect/registry",
    "/api/plugins/one-ai-employee/connect/oauth/callback",
    "/api/plugins/one-ai-employee/connect/webhooks",
    "/api/plugins/one-ai-employee/connect/webhook",
    "/api/plugins/one-ai-employee/connect/webhooks/workflow-action",
    "/api/plugins/one-ai-employee/connect/webhooks/trigger-subscription",
    "/api/plugins/one-ai-employee/connect/dashboard",
    "/api/plugins/one-ai-employee/connect/custom.js",
})
