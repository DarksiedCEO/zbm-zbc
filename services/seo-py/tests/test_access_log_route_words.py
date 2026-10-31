"""AEGIS round 4 N4-8: seo's access log wrote tenant and entity slugs (`/seo/v1/tenants/acme-pets`): a plain lowercase
word passed the plain-word rule. create_app registers seo's route templates (access_redact.register_routes)."""

import access_redact
from helpers import Harness


def test_n4_8_tenant_slugs_in_paths_are_redacted(tmp_path):
    Harness(tmp_path)
    try:
        assert access_redact.redact_target("/seo/v1/tenants/acme-pets") == "/seo/v1/tenants/:id"
        assert access_redact.redact_target("/health") == "/health"
    finally:
        access_redact.register_routes(None)
