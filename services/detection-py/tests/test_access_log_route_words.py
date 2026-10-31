"""AEGIS round 4 N4-8: detection's access log wrote client slugs in path positions (`/stores/acme-pets/status`): a
plain lowercase word passed the plain-word rule. The app registers its route templates (access_redact.register_routes),
so only route words are kept."""

import access_redact
import api  # noqa: F401 - registers detection's routes at import


def test_n4_8_a_client_slug_in_a_path_is_redacted():
    assert access_redact.redact_target("/stores/acme-pets/status") == "/stores/:id/status"
    assert access_redact.redact_target("/stores/sync") == "/stores/sync"
    assert access_redact.redact_target("/health") == "/health"
