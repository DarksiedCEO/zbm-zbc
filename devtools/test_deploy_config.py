"""The Railway deploy files agree with each other and with the code (go-live Days 1-2). Standard library only; CI runs
it in the `hygiene-static` job, and `python -m pytest devtools/` collects it:

    python3 -B -m unittest devtools/test_deploy_config.py -v

- every Railway Dockerfile pins its runtime base by digest, names an exact version for any other stage (and marks
  it an OPEN ITEM), runs as uid/gid 10001 through deploy/docker/zbm-entrypoint.sh, and gives the entrypoint the port
  variable the service's code actually reads;
- the Python images install only deploy/python/requirements.lock.txt, with hashes, and its direct pins are the
  services' requirements.txt pins without pytest;
- .railway/railway.ts deploys exactly these Dockerfiles, one replica each, RAILWAY_RUN_UID=0 on exactly the services
  with a volume or a --secret-file in their image, a daily backup schedule on every volume, a PORT equal to the port
  the CI smoke uses, every --secret-file value variable declared preserve(), and holds no secret value (secrets are
  preserve() or references);
- the CI job builds every one of them and the smoke script covers every one;
- docs/deploy/railway.md names every environment variable the deployed code reads (Python, Go, Rust and the
  dashboard), every variable the IaC sets, and every Dockerfile;
- the runbook's store credential paths (per JSON field) and its kill-file path (variable table, step 8, Stop) agree
  with the entrypoint and with each other, and its LEDGER_CALLERS steps use devtools/ledger_callers.py with that
  script's grants (devtools/test_ledger_callers.py tests the script itself).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "deploy" / "railway.md"
IAC = REPO / ".railway" / "railway.ts"
CI = REPO / ".github" / "workflows" / "ci.yml"
LOCK = REPO / "deploy" / "python" / "requirements.lock.txt"
REQ_IN = REPO / "deploy" / "python" / "requirements.in"
ENTRY = REPO / "deploy" / "docker" / "zbm-entrypoint.sh"
SMOKE = REPO / "devtools" / "railway_smoke.py"

# Railway service -> (Dockerfile, the service's own port variable, its port, smoke-table name, has a volume)
SERVICES = {
    "ledger": ("services/ledger-rust/Dockerfile", "LEDGER_PORT", 8090, "ledger-rust", True),
    "detection": ("services/detection-py/Dockerfile", "DETECTION_PORT", 8000, "detection-py", False),
    "legal": ("services/legal-py/Dockerfile", "LEGAL_PORT", 8420, "legal-py", True),
    "finance": ("services/finance-py/Dockerfile", "FIN_PORT", 8410, "finance-py", True),
    "orchestrator": ("services/orchestrator-go/Dockerfile.orchestrator", "ORCHESTRATOR_PORT", 8080, "orchestrator", False),
    "stripe-gateway": ("services/orchestrator-go/Dockerfile.stripe-gateway", "STRIPE_GATEWAY_PORT", 8470,
                       "stripe-gateway", False),
    "seo": ("services/seo-py/Dockerfile", "SEO_PORT", 8500, "seo-py", True),
    "compliance": ("services/compliance-py/Dockerfile", "COMPLIANCE_PORT", 8380, "compliance-py", True),   # ADR 0024
}
# ADR 0026: Railway CRON services (no port, no healthcheck, no volume, no domain): name -> Dockerfile
CRON_SERVICES = {"fin-cron": "deploy/scheduler/Dockerfile", "ops-alerts": "deploy/scheduler/Dockerfile"}
CRON_SCRIPT = REPO / "deploy" / "scheduler" / "fin_cron.py"
PY_SERVICES = {"detection-py": ("ZBM_", "DETECTION_", "RR_STORE_"), "finance-py": ("FIN_",), "legal-py": ("LEGAL_",),
               "seo-py": ("SEO_",), "compliance-py": ("COMPLIANCE_",)}
# ADR 0024: compliance-py's requirements.txt is clipper-network-py's (that suite checks the two are identical) and has
# no use for cryptography; its pins must be a SUBSET of requirements.in, each locked at its pin. The image installs the
# whole lock, like every Python image.
LOCK_SUBSET = {"compliance-py"}
# AEGIS final go-live M-2: the prefix scan alone missed detection-py's RR_STORE_* variables (both Revenue Recovery
# kill switches among them). Besides the prefixes, any upper-case name in an environment-read position is collected
# whatever its prefix: os.environ.get("X"), os.environ["X"], os.getenv("X"), env.get("X"), env["X"] (the services'
# config functions take the environment as `env`).
ENV_READ = re.compile(r"""(?:\benviron\.get|\benviron\[|\bgetenv|\benv\.get|\benv\[)\(?\s*["']([A-Z][A-Z0-9_]+)["']""")
# string literals under src/ that look like variables but are not read from the environment: thin-client prefixes
# (`_thin(env, "FIN_VI", ...)` reads FIN_VI_URL / _TOKEN / _CALLER_TOKEN, which are named) and a reason code
NOT_VARIABLES = {"FIN_VI", "FIN_COMPLIANCE", "FIN_LEGAL", "LEDGER_UNAVAILABLE"}
DIGEST_FROM = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}(\s+AS\s+\w+)?\s*$", re.I)
TAG_FROM = re.compile(r"^FROM\s+[a-z0-9./-]+:\d+\.\d+\.\d+[a-z0-9.-]*(\s+AS\s+\w+)?\s*$", re.I)


def logical_lines(text: str) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            cur += line.rstrip()[:-1] + " "
            continue
        out.append(cur + line)
        cur = ""
    return out + ([cur] if cur else [])


def instructions(path: Path) -> list[str]:
    return [ln.strip() for ln in logical_lines(path.read_text()) if ln.strip() and not ln.strip().startswith("#")]


def entrypoint_args(path: Path) -> list[str]:
    line = next(i for i in instructions(path) if i.upper().startswith("ENTRYPOINT"))
    return ast.literal_eval(line.split(None, 1)[1])


def secret_files(path: Path) -> list[str]:
    """The *_FILE settings an image's entrypoint writes from Railway variables (--secret-file NAME)."""
    args = entrypoint_args(path)
    return [args[i + 1] for i, a in enumerate(args[:args.index("--")]) if a == "--secret-file"]


def entrypoint_secret_path(file_var: str) -> str:
    """Where zbm-entrypoint writes --secret-file NAME: $SECRET_DIR/ + NAME lower-cased with _ as - (write_secret)."""
    return "/run/zbm-secrets/" + file_var.lower().replace("_", "-")


def code_variables() -> dict[str, set[str]]:
    """Every environment variable the deployed code reads, per component."""
    out: dict[str, set[str]] = {}
    for svc, prefixes in PY_SERVICES.items():
        names: set[str] = set()
        for p in (REPO / "services" / svc / "src").rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            names |= {n for n in re.findall(r"""["']([A-Z][A-Z0-9_]+)["']""", text)
                      if n.startswith(prefixes + ("LEDGER_SERVICE_",))}
            names |= set(ENV_READ.findall(text))
        out[svc] = names - NOT_VARIABLES
    go: set[str] = set()
    for p in (REPO / "services" / "orchestrator-go" / "cmd").rglob("*.go"):
        if p.name.endswith("_test.go"):
            continue
        text = p.read_text()
        go |= set(re.findall(r'[Gg]etenv\("([A-Z0-9_]+)"\)', text))
        go |= set(re.findall(r'\w+Env\s*=\s*"([A-Z][A-Z0-9_]+)"', text))
    out["orchestrator-go"] = go
    rust = (REPO / "services" / "ledger-rust" / "src" / "bin" / "server.rs").read_text()
    out["ledger-rust"] = set(re.findall(r'"(LEDGER_[A-Z0-9_]+)"', rust))
    dash: set[str] = set()
    for p in list((REPO / "apps" / "dashboard-ts" / "src").rglob("*.ts*")) + list(
            (REPO / "apps" / "dashboard-ts" / "scripts").rglob("*.mjs")):
        text = p.read_text()
        dash |= set(re.findall(r'\benv(?:\.|\[")([A-Z][A-Z0-9_]+)', text))
        dash |= set(re.findall(r'process\.env\.([A-Z][A-Z0-9_]+)', text))
        dash |= set(re.findall(r'_ENV\s*=\s*"([A-Z][A-Z0-9_]+)"', text))
    out["dashboard-ts"] = dash
    return out


def iac_services() -> dict[str, str]:
    """Railway service name -> the text of its service(...) block in .railway/railway.ts."""
    text = IAC.read_text()
    blocks = {}
    for m in re.finditer(r'service\("([a-z-]+)",\s*\{', text):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        blocks[m.group(1)] = text[m.end():i - 1]
    return blocks


class Dockerfiles(unittest.TestCase):
    def test_bases_are_pinned(self):
        for svc, (df, *_rest) in SERVICES.items():
            froms = [i for i in instructions(REPO / df) if i.upper().startswith("FROM ")]
            with self.subTest(dockerfile=df):
                self.assertTrue(froms, "no FROM")
                self.assertRegex(froms[-1], DIGEST_FROM, "the runtime stage must be pinned by digest")
                for f in froms[:-1]:
                    if not DIGEST_FROM.match(f):
                        self.assertRegex(f, TAG_FROM, "a build stage must name an exact x.y.z version")
                        self.assertIn("OPEN ITEM", (REPO / df).read_text(), "a stage pinned by tag only is an OPEN ITEM")

    def test_non_root_through_the_entrypoint_with_the_right_port_variable(self):
        reads = code_variables()
        for svc, (df, port_var, *_rest) in SERVICES.items():
            ins = instructions(REPO / df)
            final = ins[max(i for i, x in enumerate(ins) if x.upper().startswith("FROM ")):]
            with self.subTest(dockerfile=df):
                self.assertIn("USER 10001:10001", final)
                self.assertNotIn("USER root", final)
                args = entrypoint_args(REPO / df)
                self.assertEqual(args[0], "/usr/local/bin/zbm-entrypoint")
                self.assertIn("COPY deploy/docker/zbm-entrypoint.sh /usr/local/bin/zbm-entrypoint", final)
                self.assertEqual(args[args.index("--port-var") + 1], port_var)
                if port_var != "DETECTION_PORT":       # detection-py takes --port; the image maps DETECTION_PORT
                    self.assertTrue(any(port_var in names for names in reads.values()),
                                    f"{port_var} is read by no service code")

    def test_python_images_install_only_the_hash_lock(self):
        for svc in PY_SERVICES:
            text = "\n".join(instructions(REPO / "services" / svc / "Dockerfile"))
            with self.subTest(service=svc):
                self.assertIn("COPY deploy/python/requirements.lock.txt", text)
                self.assertIn("--require-hashes", text)
                self.assertIn("--only-binary=:all:", text)
                self.assertNotRegex(text, r"requirements\.txt|pip install (?!--require-hashes)")

    def test_the_lock_matches_the_services_requirements(self):
        pins = lambda text: {ln.strip() for ln in text.splitlines()
                             if ln.strip() and not ln.startswith("#") and not ln.startswith("pytest==")}
        want = pins(REQ_IN.read_text())
        for svc in PY_SERVICES:
            with self.subTest(service=svc):
                have = pins((REPO / "services" / svc / "requirements.txt").read_text())
                if svc in LOCK_SUBSET:
                    self.assertTrue(have and have <= want, f"{svc}: every pin must be one of requirements.in's")
                else:
                    self.assertEqual(have, want)
        lock = LOCK.read_text()
        entries = re.findall(r"^([a-z0-9._-]+)==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", lock, re.M)
        names = {n for n, _v, _h in entries}
        self.assertEqual(len(entries), len(re.findall(r"^[a-z0-9._-]+==", lock, re.M)), "a lock entry without a hash")
        for req in want:
            name, version = re.match(r"([a-z0-9._-]+)(?:\[[a-z,]+\])?==(\S+)", req).groups()
            self.assertIn((name, version), {(n, v) for n, v, _h in entries}, f"{req} is not locked at its pin")
        self.assertNotIn("pytest", names)

    def test_entrypoint_is_posix_sh_and_executable(self):
        self.assertTrue(ENTRY.read_text().startswith("#!/bin/sh\n"))
        self.assertTrue(ENTRY.stat().st_mode & 0o111, "deploy/docker/zbm-entrypoint.sh must be executable")


# `${{service.VARIABLE}}` (another service's variable) or `${{VARIABLE}}` (the same service's; docs.railway.com/variables)
REF = re.compile(r"\$\{\{(?:[a-z][a-z0-9-]*\.)?[A-Z][A-Z0-9_]*\}\}")


def secret_value_ok(value: str) -> bool:
    """A secret in the IaC is preserve(), a reference (`finance.env.X`), exactly one Railway reference in a string
    (`"${{compliance.X}}"`, ADR 0024: a service declared before the one holding the value), or a JSON map literal whose
    EVERY value is exactly one `${{service.VARIABLE}}` reference (AEGIS L5): a literal secret next to a reference is
    refused."""
    if re.fullmatch(r"preserve\(\)|[a-z][A-Za-z]*\.env\.[A-Z0-9_]+", value):
        return True
    one = re.fullmatch(r'"(.*)"', value)
    if one:
        return REF.fullmatch(one.group(1)) is not None
    m = re.fullmatch(r"'(\{.*\})'", value)
    if not m:
        return False
    try:
        mapping = json.loads(m.group(1))
    except ValueError:
        return False
    return (isinstance(mapping, dict) and bool(mapping)
            and all(isinstance(v, str) and REF.fullmatch(v) for v in mapping.values()))


class RailwayIac(unittest.TestCase):
    def setUp(self):
        self.blocks = iac_services()
        self.text = IAC.read_text()

    def test_exactly_the_dockerfiles(self):
        self.assertEqual(set(self.blocks), set(SERVICES) | set(CRON_SERVICES))
        for svc, (df, _pv, port, _smoke, has_volume) in SERVICES.items():
            b = self.blocks[svc]
            with self.subTest(service=svc):
                self.assertIn(f'dockerfile("{df}"', b)
                self.assertIn(f'PORT: "{port}"', b)
                self.assertEqual(f'"/data":' in b, has_volume)
                # root at start is needed to hand a root-owned volume to uid 10001 and to write a secret file the
                # service cannot read back from /proc/1/environ (AEGIS L3); nowhere else (AEGIS A2 H-2: detection)
                needs_root = has_volume or bool(secret_files(REPO / df))
                self.assertEqual('RAILWAY_RUN_UID: "0"' in b, needs_root,
                                 "RAILWAY_RUN_UID=0 exactly with a volume or a --secret-file")
                self.assertIn('deploy("/health", ' + ("true" if has_volume else "false") + ")", b)

    def test_every_secret_file_value_variable_is_preserved(self):
        # an image's --secret-file NAME_FILE is written from the Railway variable NAME; a plan that dropped NAME would
        # delete the secret on the next apply (and with it the feature: a ledger caller, a Stripe key, a store). ADR
        # 0024: a reference to the service that holds the value (`compliance.env.X`) is declared too, and kept.
        for svc, (df, *_r) in SERVICES.items():
            for file_var in secret_files(REPO / df):
                with self.subTest(service=svc, variable=file_var):
                    self.assertTrue(file_var.endswith("_FILE"))
                    name = file_var[:-len("_FILE")]
                    self.assertTrue(self._declared_preserved(svc, name) or self._declared_reference(svc, name),
                                    f"{name} is neither preserve() nor a reference on {svc}")

    def test_detection_store_connector_is_wired_without_a_volume(self):
        # AEGIS A2 H-2: the store connector needs its ledger token, the connections JSON and the credential files; the
        # service keeps no state (snapshots in memory), so no volume and no secret on a backed-up disk
        want = ["DETECTION_LEDGER_TOKEN_FILE", "RR_STORE_CONNECTIONS_FILE", "RR_STORE_SECRET_1_FILE",
                "RR_STORE_SECRET_2_FILE", "DETECTION_COMPLIANCE_TOKEN_FILE", "DETECTION_COMPLIANCE_CALLER_TOKEN_FILE"]
        self.assertEqual(secret_files(REPO / SERVICES["detection"][0]), want)
        b = self.blocks["detection"]
        self.assertIn('DETECTION_LEDGER_URL: PRIVATE("ledger", 8090),', b)
        self.assertNotIn("volumeMounts", b)
        self.assertNotRegex(re.sub(r"//[^\n]*", "", b), r"\bRR_STORE_CONNECTIONS_FILE\s*:|\bDETECTION_LEDGER_TOKEN_FILE\s*:",
                            "the *_FILE settings are the entrypoint's; setting one next to its value refuses the start")

    def test_every_volume_has_a_daily_backup_schedule(self):
        # AEGIS L6: each service with a volume goes through withDailyBackups (backupSchedules ["DAILY"] on its attachment)
        self.assertIn('const DAILY_BACKUPS: VolumeMount["backupSchedules"] = ["DAILY"];', self.text)
        for svc, (*_r, has_volume) in SERVICES.items():
            with self.subTest(service=svc):
                self.assertEqual(f'withDailyBackups(service("{svc}", ' in self.text, has_volume)

    def test_every_volume_states_its_live_region_and_size_so_apply_cannot_null_them(self):
        # `railway config plan` (2026-10-27, railway 3.13.1) on the four volumes declared without a config:
        # `config.region ("us-west2" -> null)`, `config.sizeMB (5000 -> null)`, "4 destructive change(s)". The data
        # volumes must state what is deployed, so a plan shows no change to them.
        self.assertIn('const VOLUME_AS_DEPLOYED: VolumeConfig = { region: "us-west2", sizeMB: 5000 };', self.text)
        calls = re.findall(r'\bvolume\(("[^"]+")([^)]*)\)', self.text)
        self.assertEqual(sorted(n for n, _ in calls), sorted(f'"{s}-data"' for s, (*_r, v) in SERVICES.items() if v))
        for name, rest in calls:
            with self.subTest(volume=name):
                self.assertEqual(rest.strip(), ", VOLUME_AS_DEPLOYED")

    def test_one_replica_and_restart_on_failure(self):
        self.assertIn("numReplicas: 1,", self.text)
        self.assertIn('restartPolicyType: "ON_FAILURE"', self.text)
        self.assertNotRegex(self.text, r"\breplicas\s*:", "replicas are set once, in deploy(): numReplicas 1")
        self.assertNotIn("randomString", self.text.split("import", 1)[1], "ctx.randomString is not a secret")

    def test_no_secret_values(self):
        for svc, b in self.blocks.items():
            for name, value in re.findall(r"^\s+([A-Z][A-Z0-9_]+):\s*(.+?),?\s*(?://.*)?$", b, re.M):
                if re.search(r"TOKEN|SECRET|KEY|PASSWORD|CALLERS", name):
                    with self.subTest(service=svc, variable=name):
                        self.assertTrue(secret_value_ok(value), f"{value}: a secret must be preserve(), a reference, "
                                        "or a JSON map whose every value is one ${{service.VARIABLE}}")

    def _declared_reference(self, svc: str, name: str) -> bool:
        code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", self.blocks[svc], flags=re.S))
        return re.search(rf"^\s+{name}:\s*[a-z][A-Za-z]*\.env\.[A-Z0-9_]+,", code, re.M) is not None

    def _declared_preserved(self, svc: str, name: str) -> bool:
        # comments stripped first, so a declaration inside /* ... */ or after // does not count
        code = re.sub(r"/\*.*?\*/", "", self.blocks[svc], flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        return re.search(rf"^\s+{name}:\s*preserve\(\)", code, re.M) is not None

    def test_every_dashboard_set_secret_is_declared_so_apply_cannot_delete_it(self):
        # `railway config plan` reports Railway as owning every variable of a service: one set in the dashboard but
        # absent from that service's block is deleted by the next `railway config apply`. Every row of the runbook's
        # Secrets table whose owner is a Railway service must therefore be declared in that service's block.
        runbook = (REPO / "docs" / "deploy" / "railway.md").read_text()
        table = runbook.split("\n## Secrets\n", 1)[1].split("\n## ", 1)[0]
        rows = re.findall(r"^\| `([A-Z][A-Z0-9_]+)`[^|]*\|[^|]*\| ([A-Za-z-]+) \|", table, re.M)
        self.assertGreaterEqual(len(rows), 20, "the Secrets table was not parsed")
        for name, owner in rows:
            with self.subTest(service=owner, variable=name):
                if owner == "Vercel":         # set in Vercel, not Railway
                    continue
                self.assertIn(owner, self.blocks, f"{name}: owner {owner!r} is not a Railway service in railway.ts")
                self.assertTrue(self._declared_preserved(owner, name), f"{name} is not preserve() on {owner}")

    def test_hand_set_operator_variables_are_declared_so_apply_cannot_reset_them(self):
        # Set by hand in the dashboard (keys, budgets, success/cancel pages, incident rollbacks and kill switches): a
        # plan that dropped one would silently reset it on the next apply (an engine back OFF, a kill switch released).
        hand_set = {
            "seo": ("SEO_PERPLEXITY_API_KEY", "SEO_PERPLEXITY_DAILY_CALLS", "SEO_PERPLEXITY_DAILY_SPEND_USD",
                    "SEO_PERPLEXITY_TENANT_DAILY_CALLS", "SEO_PERPLEXITY_TENANT_DAILY_SPEND_USD",
                    "SEO_PERPLEXITY_PROTOCOL", "SEO_PERPLEXITY_MODEL", "SEO_PERPLEXITY_RESERVE_USD_PER_CALL",
                    "SEO_KILL_GLOBAL", "SEO_KILLED_CAPABILITIES", "SEO_KILLED_PROVIDERS"),
            "finance": ("FIN_STRIPE_SUCCESS_URL", "FIN_STRIPE_CANCEL_URL"),
            "detection": ("RR_STORE_READS_DISABLED", "RR_STORE_KILL_SWITCH_FILE"),
        }
        for svc, names in hand_set.items():
            for name in names:
                with self.subTest(service=svc, variable=name):
                    self.assertTrue(self._declared_preserved(svc, name), f"{name} is not preserve() on {svc}")

    def test_the_preserve_check_ignores_comments(self):
        saved = self.blocks
        try:
            self.blocks = {"x": "\n  env: {\n    /* A: preserve(), */\n    // B: preserve(),\n    C: preserve(),\n  }"}
            self.assertFalse(self._declared_preserved("x", "A"))
            self.assertFalse(self._declared_preserved("x", "B"))
            self.assertTrue(self._declared_preserved("x", "C"))
        finally:
            self.blocks = saved

    def test_the_secret_check_refuses_a_literal_inside_a_reference_map(self):
        q = lambda body: "'" + body + "'"   # noqa: E731  the map as it appears in railway.ts, single-quoted
        good = ["preserve()", "finance.env.FIN_SERVICE_TOKEN",
                q('{"rail_gateway": "${{stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_X}}"}'),
                q('{"finance_31": "${{finance.FIN_X}}", "onboarding": "${{LEGAL_ONBOARDING_CALLER_TOKEN}}"}')]
        bad = [q('{"a": "${{x.Y}}", "b": "a-literal-secret-value"}'), q('{"a": "prefix-${{x.Y}}"}'),
               q('{"a": "${{x.Y}}${{x.Z}}"}'), q("{}"), q('{"a": "${{x.Y}}", "b": {"c": "literal"}}'),
               q("{... ${{x.Y}} ...}"), '"a-literal-secret"', "ctx.randomString('x')",
               q('{"a": "${{x.Y}}"}') + " + secret", q('{"a": "${{lower_case}}"}'), q('{"a": "${{ Y }}"}')]
        for v in good:
            self.assertTrue(secret_value_ok(v), v)
        for v in bad:
            self.assertFalse(secret_value_ok(v), v)


    def _caller_map(self, svc: str, name: str) -> dict:
        m = re.search(rf"^\s+{name}:\s*'(\{{[^']*\}})',", self.blocks[svc], re.M)
        self.assertIsNotNone(m, f"{name} on {svc} is not a single-quoted JSON map literal")
        return json.loads(m.group(1))   # the services' parsers need a JSON object (a ValueError fails the test)

    def test_caller_maps_resolve_and_use_only_names_the_service_accepts(self):
        # A map entry whose reference points nowhere would reach the service as the literal "${{...}}" text, and a
        # name outside the service's CALLER_NAMES makes it refuse to start (config._tokens). Each reference must name
        # a variable declared in the service it points at (the same service for `${{VARIABLE}}`).
        maps = {"finance": ("FIN_CALLER_TOKENS", "services/finance-py/src/config.py"),
                "legal": ("LEGAL_CALLER_TOKENS", "services/legal-py/src/config.py"),
                "compliance": ("COMPLIANCE_CALLER_TOKENS", "services/compliance-py/src/config.py")}
        for svc, (name, config_py) in maps.items():
            tree = ast.parse((REPO / config_py).read_text())
            accepted = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                            and any(getattr(t, "id", None) == "CALLER_NAMES" for t in n.targets))
            mapping = self._caller_map(svc, name)
            refs = list(mapping.values())
            self.assertEqual(len(set(refs)), len(refs), f"{name}: two callers would share one token (refused)")
            for caller, ref in mapping.items():
                with self.subTest(service=svc, caller=caller):
                    self.assertIn(caller, accepted, f"{svc} refuses to start on caller name {caller!r}")
                    target, _, var = ref[3:-2].rpartition(".")
                    target = target or svc
                    self.assertIn(target, self.blocks, f"{ref}: no Railway service {target!r}")
                    self.assertTrue(self._declared_preserved(target, var), f"{ref}: {var} is not preserve() on {target}")

    def test_onboarding_caller_is_wired_with_one_operator_held_token_per_service(self):
        # AEGIS A1 C1: POST /fin/v1/invoices needs `onboarding` (or `scheduler`) and POST /legal/v1/acceptances needs
        # `onboarding` (or hub / clipper_network); without them no invoice can be drafted on Railway. Until an
        # onboarding service exists Andre holds the two tokens; each lives on the service that checks it.
        for svc, name, var in (("finance", "FIN_CALLER_TOKENS", "FIN_ONBOARDING_CALLER_TOKEN"),
                               ("legal", "LEGAL_CALLER_TOKENS", "LEGAL_ONBOARDING_CALLER_TOKEN")):
            with self.subTest(service=svc):
                self.assertEqual(self._caller_map(svc, name).get("onboarding"), "${{" + var + "}}")
                self.assertTrue(self._declared_preserved(svc, var), f"{var} is not preserve() on {svc}")
        self.assertNotIn("scheduler", self._caller_map("finance", "FIN_CALLER_TOKENS"),
                         "scheduler reaches payout runs, releases, refunds and sweeps: not for a hand-held token")


class ComplianceGate(unittest.TestCase):
    """ADR 0024: Compliance (38) is deployed PRIVATE, with its volume, ledger, Legal wiring and scheduler, and every
    go-live caller is wired to it (Finance, SEO, detection; Legal delivers memo proposals)."""

    def setUp(self):
        self.blocks = iac_services()
        self.text = IAC.read_text()

    def test_compliance_is_private_with_its_volume_ledger_and_scheduler(self):
        b = self.blocks["compliance"]
        for forbidden in ("serviceDomains", "customDomains", "tcpProxies", "networking", "domain"):
            self.assertNotIn(forbidden, re.sub(r"//[^\n]*", "", b), "Compliance is private: no public domain")
        self.assertIn('withDailyBackups(service("compliance", ', self.text)
        self.assertIn('const complianceData = volume("compliance-data", VOLUME_AS_DEPLOYED);', self.text)
        for line in ('COMPLIANCE_DATA_DIR: "/data/compliance",', 'LEDGER_SERVICE_URL: PRIVATE("ledger", 8090),',
                     'COMPLIANCE_LEGAL_URL: PRIVATE("legal", 8420),', "COMPLIANCE_LEGAL_TOKEN: legal.env.LEGAL_SERVICE_TOKEN,",
                     'COMPLIANCE_SCHEDULER_ENABLED: "1",', 'COMPLIANCE_WATCHER_ENABLED: "1",'):
            self.assertIn(line, b)
        for name in ("LEDGER_SERVICE_TOKEN", "COMPLIANCE_SERVICE_TOKEN", "COMPLIANCE_ANDRE_APPROVAL_TOKEN",
                     "COMPLIANCE_ONBOARDING_CALLER_TOKEN", "COMPLIANCE_LEGAL_CALLER_TOKEN"):
            self.assertRegex(b, rf"(?m)^\s+{name}:\s*preserve\(\)")
        # nothing that must stay unset is set (config.py refuses to start on them)
        for name in ("COMPLIANCE_SANCTIONS_PROVIDER", "COMPLIANCE_A11Y_PROVIDER", "COMPLIANCE_ALLOW_UNPINNED_SEED",
                     "COMPLIANCE_SEED_PATH", "COMPLIANCE_SEED_SHA256", "COMPLIANCE_RECONCILE_MODE"):
            self.assertNotIn(name, b)

    def test_no_scheduler_token_exists(self):
        m = re.search(r"COMPLIANCE_CALLER_TOKENS:\s*'(\{[^']*\})'", self.blocks["compliance"])
        callers = json.loads(m.group(1))
        self.assertEqual(sorted(callers), ["finance_31", "legal_37", "onboarding", "revenue_recovery", "seo_02"])

    def test_every_go_live_caller_is_wired_to_compliance(self):
        for svc, prefix in (("finance", "FIN_COMPLIANCE"), ("seo", "SEO_COMPLIANCE"),
                            ("detection", "DETECTION_COMPLIANCE")):
            b = self.blocks[svc]
            with self.subTest(service=svc):
                self.assertIn(f'{prefix}_URL: PRIVATE("compliance", 8380),', b)
                self.assertIn(f"{prefix}_TOKEN: compliance.env.COMPLIANCE_SERVICE_TOKEN,", b)
                self.assertRegex(b, rf"(?m)^\s+{prefix}_CALLER_TOKEN:\s*preserve\(\)")
        legal = self.blocks["legal"]
        self.assertIn('LEGAL_COMPLIANCE_URL: PRIVATE("compliance", 8380),', legal)
        self.assertIn('LEGAL_COMPLIANCE_TOKEN: "${{compliance.COMPLIANCE_SERVICE_TOKEN}}",', legal)
        self.assertIn('"compliance_38": "${{compliance.COMPLIANCE_LEGAL_CALLER_TOKEN}}"', legal)

    def test_resources_list_every_service_and_volume_once(self):
        res = re.search(r"resources:\s*\[([^\]]*)\]", self.text).group(1)
        names = [x.strip() for x in res.split(",") if x.strip()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("compliance", names)
        self.assertIn("complianceData", names)


class FinCron(unittest.TestCase):
    """ADR 0026 / red team OPS-H1: the cron service that runs Finance's housekeeping. PRIVATE (no domain), no volume,
    one run per hourly tick, its own caller token (``ops_cron``), never the ``scheduler`` token."""

    def setUp(self):
        self.blocks = iac_services()
        self.text = IAC.read_text()
        self.b = self.blocks["fin-cron"]
        self.code = re.sub(r"//[^\n]*", "", self.b)

    def test_private_without_a_volume_and_a_cron_schedule(self):
        for forbidden in ("domains", "serviceDomains", "customDomains", "tcpProxies", "networking", "volumeMounts",
                          "healthcheck", "requiredMountPath"):
            self.assertNotIn(forbidden, self.code)
        self.assertIn('build: dockerfile("deploy/scheduler/Dockerfile", ["deploy/scheduler/**"]),', self.b)
        self.assertIn('deploy: { numReplicas: 1, restartPolicyType: "NEVER" as const, cronSchedule: "17 * * * *" },',
                      self.b)
        self.assertIn("finCron, opsAlerts]", self.text)

    def test_its_own_caller_token_and_finance_knows_it_as_ops_cron_only(self):
        self.assertRegex(self.b, r"(?m)^\s+FIN_CRON_CALLER_TOKEN:\s*preserve\(\)")
        self.assertIn("FIN_CRON_SERVICE_TOKEN: finance.env.FIN_SERVICE_TOKEN,", self.b)
        self.assertIn('FIN_CRON_FINANCE_URL: PRIVATE("finance", 8410),', self.b)
        m = re.search(r"FIN_CALLER_TOKENS:\s*'(\{[^']*\})'", self.blocks["finance"])
        callers = json.loads(m.group(1))
        self.assertEqual(callers.get("ops_cron"), "${{fin-cron.FIN_CRON_CALLER_TOKEN}}")
        self.assertNotIn("scheduler", callers)
        accepted = (REPO / "services" / "finance-py" / "src" / "config.py").read_text()
        self.assertIn('"ops_cron", "ops_alerts")', accepted)

    def test_the_image_is_pinned_non_root_and_installs_nothing(self):
        ins = instructions(REPO / CRON_SERVICES["fin-cron"])
        froms = [i for i in ins if i.upper().startswith("FROM ")]
        self.assertEqual(len(froms), 1)
        self.assertRegex(froms[0], DIGEST_FROM)
        self.assertIn("USER 10001:10001", ins)
        self.assertFalse([i for i in ins if "pip" in i or "apt-get" in i])
        self.assertEqual(entrypoint_args(REPO / CRON_SERVICES["fin-cron"]),
                         ["/usr/local/bin/python", "-I", "/app/fin_cron.py"])
        imports = set(re.findall(r"^(?:from|import) ([a-z_.]+)", CRON_SCRIPT.read_text(), re.M))
        self.assertLessEqual({i.split(".")[0] for i in imports},
                             {"__future__", "json", "os", "re", "sys", "time", "urllib", "datetime", "typing",
                              "zoneinfo"})

    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fin_cron_under_test", CRON_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    ENV = {"FIN_CRON_FINANCE_URL": "http://finance.railway.internal:8410",
           "FIN_CRON_SERVICE_TOKEN": "s" * 64, "FIN_CRON_CALLER_TOKEN": "c" * 64}

    def test_hourly_calls_daily_reconciliation_retries_and_exit_codes(self):
        import io
        from datetime import datetime, timezone
        mod = self._mod()
        calls, waits = [], []

        def post(url, headers, body):
            calls.append((url, headers, json.loads(body)))
            return 200, {"summary": {"sent": 1, "secret": "never-printed"}}
        out = io.StringIO()
        # 15:17 UTC = 08:17 PDT: hourly only (the reconciliation hour is 7 local)
        self.assertEqual(mod.run(self.ENV, datetime(2026, 10, 31, 15, 17, tzinfo=timezone.utc), post, waits.append,
                                 out), 0)
        self.assertEqual([c[0] for c in calls], ["http://finance.railway.internal:8410/fin/v1/jobs/stripe-sessions/run",
                                                 "http://finance.railway.internal:8410/fin/v1/jobs/client-mail/run"])
        h = calls[0][1]
        self.assertEqual((h["Authorization"], h["X-FIN-Caller-Token"]), ("Bearer " + "s" * 64, "c" * 64))
        self.assertRegex(calls[0][2]["request_id"], r"^[A-Za-z0-9._:-]{1,128}$")
        self.assertNotIn("s" * 64, out.getvalue())
        self.assertNotIn("c" * 64, out.getvalue())
        self.assertNotIn("never-printed", out.getvalue())
        calls.clear()
        # 14:17 UTC = 07:17 PDT: plus the daily reconciliation
        mod.run(self.ENV, datetime(2026, 10, 31, 14, 17, tzinfo=timezone.utc), post, waits.append, io.StringIO())
        self.assertEqual(calls[-1][0], "http://finance.railway.internal:8410/fin/v1/reconciliations/run")
        # a quiesced Finance (503) is retried; a refusal fails the run with an ALERT line
        answers = [(503, None), (200, {}), (403, {"detail": "x"}), ]

        def flaky(url, headers, body):
            return answers.pop(0)
        out = io.StringIO()
        waits.clear()
        self.assertEqual(mod.run(self.ENV, datetime(2026, 10, 31, 15, 17, tzinfo=timezone.utc), flaky, waits.append,
                                 out), 1)
        self.assertEqual(waits, [10])
        self.assertIn("ALERT fin-cron:", out.getvalue())

    def test_the_http_call_reads_json_and_never_follows_a_redirect(self):
        import http.server
        import threading
        mod = self._mod()
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("X-FIN-Caller-Token"),
                             self.rfile.read(int(self.headers["Content-Length"]))))
                if self.path == "/redirect":
                    self.send_response(307)
                    self.send_header("Location", "http://127.0.0.1:1/steal")
                    self.end_headers()
                    return
                body = b'{"summary": {"sent": 2}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            status, doc = mod.http_post(base + "/ok", {"X-FIN-Caller-Token": "c" * 64,
                                                       "Content-Type": "application/json"}, b'{"request_id": "x"}')
            self.assertEqual((status, doc), (200, {"summary": {"sent": 2}}))
            status, _ = mod.http_post(base + "/redirect", {"X-FIN-Caller-Token": "c" * 64}, b"{}")
            self.assertEqual(status, 307)
            self.assertEqual([p for p, _c, _b in seen], ["/ok", "/redirect"])        # nothing reached /steal
        finally:
            srv.shutdown()
            srv.server_close()

    def test_it_refuses_a_public_or_unresolved_configuration(self):
        mod = self._mod()
        bad = [{"FIN_CRON_FINANCE_URL": "http://evil.example:8410"},
               {"FIN_CRON_FINANCE_URL": "http://finance.railway.internal:8410@evil.example"},
               {"FIN_CRON_CALLER_TOKEN": "${{fin-cron.FIN_CRON_CALLER_TOKEN}}"},
               {"FIN_CRON_CALLER_TOKEN": "short"}, {"FIN_CRON_CALLER_TOKEN": "s" * 64},
               {"FIN_CRON_RECON_HOUR": "24"}, {"FIN_CRON_TZ": "Mars/Olympus"}]
        for over in bad:
            with self.subTest(over=over):
                with self.assertRaises(SystemExit):
                    mod.settings({**self.ENV, **over})


class OpsAlerts(unittest.TestCase):
    """ADR 0027: the founder-alert cron. Same image as fin-cron, every 5 minutes, PRIVATE, no volume, its OWN caller
    token that Finance knows as ``ops_alerts`` (it reaches only POST /fin/v1/jobs/founder-alerts/run)."""

    def setUp(self):
        self.blocks = iac_services()
        self.text = IAC.read_text()
        self.b = self.blocks["ops-alerts"]
        self.code = re.sub(r"//[^\n]*", "", self.b)

    def test_private_without_a_volume_every_five_minutes_on_the_fin_cron_image(self):
        for forbidden in ("domains", "serviceDomains", "customDomains", "tcpProxies", "networking", "volumeMounts",
                          "healthcheck", "requiredMountPath"):
            self.assertNotIn(forbidden, self.code)
        self.assertIn('build: dockerfile("deploy/scheduler/Dockerfile", ["deploy/scheduler/**"]),', self.b)
        self.assertIn('deploy: { numReplicas: 1, restartPolicyType: "NEVER" as const, cronSchedule: "*/5 * * * *" },',
                      self.b)
        self.assertIn('FIN_CRON_JOBS: "alerts",', self.b)
        self.assertIn("opsAlerts]", self.text)

    def test_its_own_caller_token_and_finance_knows_it_as_ops_alerts(self):
        self.assertRegex(self.b, r"(?m)^\s+FIN_CRON_CALLER_TOKEN:\s*preserve\(\)")
        self.assertIn("FIN_CRON_SERVICE_TOKEN: finance.env.FIN_SERVICE_TOKEN,", self.b)
        self.assertIn('FIN_CRON_FINANCE_URL: PRIVATE("finance", 8410),', self.b)
        m = re.search(r"FIN_CALLER_TOKENS:\s*'(\{[^']*\})'", self.blocks["finance"])
        callers = json.loads(m.group(1))
        self.assertEqual(callers.get("ops_alerts"), "${{ops-alerts.FIN_CRON_CALLER_TOKEN}}")
        self.assertNotEqual(callers.get("ops_alerts"), callers.get("ops_cron"))
        accepted = (REPO / "services" / "finance-py" / "src" / "config.py").read_text()
        self.assertIn('"ops_alerts")', accepted)

    def test_finance_has_the_alert_settings_and_the_address_is_set_by_hand(self):
        fin = self.blocks["finance"]
        self.assertRegex(fin, r"(?m)^\s+FOUNDER_ALERT_EMAIL:\s*preserve\(\)")
        self.assertRegex(fin, r'(?m)^\s+FOUNDER_ALERT_FROM:\s*"[^"]*<alerts@zbestmedia\.com>"')

    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fin_cron_alerts_under_test", CRON_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    HB = "https://hc-ping.com/0b5c4a8e-7f1d-4e8b-9a52-1c2d3e4f5a6b"

    def _run_alerts(self, answer, env_over=None, ping_status=200):
        import io
        from datetime import datetime, timezone
        mod = self._mod()
        env = {**FinCron.ENV, "FIN_CRON_JOBS": "alerts", "FOUNDER_HEARTBEAT_URL": self.HB, **(env_over or {})}
        calls = []

        def post(url, headers, body):
            calls.append((url, dict(headers), body))
            if url == self.HB:
                return ping_status, None
            return answer
        out = io.StringIO()
        code = mod.run(env, datetime(2026, 10, 31, 15, 5, tzinfo=timezone.utc), post, lambda s: None, out)
        return code, calls, out.getvalue()

    def test_a_successful_alerts_run_pings_the_heartbeat_without_any_token(self):
        code, calls, out = self._run_alerts((200, {"summary": {"enabled": True, "state_saved": True, "open": 0}}))
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls][-1], self.HB)
        hdrs = calls[-1][1]
        self.assertNotIn("Authorization", hdrs)
        self.assertNotIn("X-FIN-Caller-Token", hdrs)
        self.assertNotIn("s" * 64, json.dumps(hdrs) + str(calls[-1][2]))
        self.assertNotIn(self.HB, out)                                  # the check URL is a secret too
        self.assertIn("heartbeat", out)

    def test_no_ping_when_the_run_failed_or_alerts_are_off(self):
        for answer in ((503, None), (403, {"detail": "x"}), (200, {"summary": {"enabled": False}}),
                       (200, {"summary": {"enabled": True, "state_saved": False}}), (200, {})):
            with self.subTest(answer=answer):
                _code, calls, _out = self._run_alerts(answer)
                self.assertNotIn(self.HB, [c[0] for c in calls])

    def test_a_failed_ping_is_an_alert_line_not_a_failed_run(self):
        code, _calls, out = self._run_alerts((200, {"summary": {"enabled": True, "state_saved": True}}),
                                            ping_status=0)
        self.assertEqual(code, 0)
        self.assertIn("ALERT fin-cron:", out)

    def test_the_heartbeat_url_is_optional_https_only_and_never_used_by_housekeeping(self):
        import io
        from datetime import datetime, timezone
        mod = self._mod()
        for bad in ("http://hc-ping.com/x", "${{ops-alerts.FOUNDER_HEARTBEAT_URL}}", "https://", "ftp://x.example/y",
                    "https://hc-ping.com/x y"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    mod.settings({**FinCron.ENV, "FIN_CRON_JOBS": "alerts", "FOUNDER_HEARTBEAT_URL": bad})
        calls = []
        mod.run({**FinCron.ENV, "FOUNDER_HEARTBEAT_URL": self.HB}, datetime(2026, 10, 31, 15, 17, tzinfo=timezone.utc),
                lambda u, h, b: calls.append(u) or (200, {"summary": {"enabled": True, "state_saved": True}}),
                lambda s: None, io.StringIO())
        self.assertNotIn(self.HB, calls)
        _code, calls2, _out = self._run_alerts((200, {"summary": {"enabled": True, "state_saved": True}}),
                                               env_over={"FOUNDER_HEARTBEAT_URL": ""})
        self.assertEqual(len(calls2), 1)

    def test_the_heartbeat_url_is_declared_and_kept_by_hand(self):
        self.assertRegex(self.b, r"(?m)^\s+FOUNDER_HEARTBEAT_URL:\s*preserve\(\)")
        self.assertRegex(self.blocks["finance"], r'(?m)^\s+FIN_ALERT_DAILY_SUMMARY:\s*"1"')

    def test_the_alerts_mode_calls_only_the_alerts_job_every_tick(self):
        import io
        from datetime import datetime, timezone
        mod = self._mod()
        env = {**FinCron.ENV, "FIN_CRON_JOBS": "alerts"}
        calls = []

        def post(url, headers, body):
            calls.append(url)
            return 200, {"summary": {"sent": 1, "open": 2, "why_off": "never-printed-text"}}
        out = io.StringIO()
        for hour in (14, 15):                     # 07:xx PDT is the reconciliation hour of the housekeeping mode
            self.assertEqual(mod.run(env, datetime(2026, 10, 31, hour, 5, tzinfo=timezone.utc), post, lambda s: None,
                                     out), 0)
        self.assertEqual(calls, ["http://finance.railway.internal:8410/fin/v1/jobs/founder-alerts/run"] * 2)
        self.assertNotIn("never-printed-text", out.getvalue())
        with self.assertRaises(SystemExit):
            mod.settings({**FinCron.ENV, "FIN_CRON_JOBS": "payouts"})
        calls.clear()
        mod.run(FinCron.ENV, datetime(2026, 10, 31, 15, 17, tzinfo=timezone.utc), post, lambda s: None, io.StringIO())
        self.assertNotIn("http://finance.railway.internal:8410/fin/v1/jobs/founder-alerts/run", calls)


class CiAndSmoke(unittest.TestCase):
    def test_ci_builds_every_dockerfile_and_the_smoke_covers_it(self):
        ci = CI.read_text()
        smoke = SMOKE.read_text()
        for svc, (df, _pv, port, name, _v) in SERVICES.items():
            with self.subTest(service=svc):
                self.assertIn(f"docker build -f {df} -t zbm-ci/{name}:ci .", ci)
                self.assertRegex(smoke, rf'"{re.escape(name)}": \({port}, ')
        for svc, df in CRON_SERVICES.items():                 # built in CI; no HTTP smoke (it serves nothing)
            self.assertIn(f"docker build -f {df} -t zbm-ci/{svc}:ci .", ci)
        self.assertIn("      - railway-images\n", ci.split("  required:", 1)[1])


class VariableScan(unittest.TestCase):
    """M-2: the scan itself is checked, so a green Runbook test means the set it compared was complete."""

    def test_detection_store_connector_variables_are_scanned(self):
        rr = {"RR_STORE_CONNECTIONS_FILE", "RR_STORE_KILL_SWITCH_FILE", "RR_STORE_READS_DISABLED",
              "RR_STORE_MAX_CONCURRENT_SYNCS", "RR_STORE_SYNC_DEADLINE_SECONDS"}
        self.assertLessEqual(rr, code_variables()["detection-py"])

    def test_env_reads_are_found_whatever_their_prefix(self):
        text = ('os.environ.get("AA_ONE")\nos.environ["BB_TWO"]\nos.getenv("CC_THREE")\n'
                'env.get("DD_FOUR")\nenv["EE_FIVE"]\nx = "NOT_READ"\nenvelope.get("FF_SIX")\n')
        self.assertEqual(sorted(ENV_READ.findall(text)), ["AA_ONE", "BB_TWO", "CC_THREE", "DD_FOUR", "EE_FIVE"])

    def test_no_python_service_reads_a_variable_outside_its_prefixes_unseen(self):
        """Every upper-case name a Python service reads in an environment-read position is either under the
        service's scanned prefixes or listed here, so a new prefix cannot slip past this file unnoticed."""
        # finance-py: the client-mail settings named in ADR 0026 (RESEND_API_KEY becomes RESEND_API_KEY_FILE) and the
        # founder-alert settings of ADR 0027
        outside_prefixes = {"detection-py": set(), "finance-py": {"RESEND_API_KEY_FILE", "MAIL_FROM",
                                                                  "FOUNDER_ALERT_EMAIL", "FOUNDER_ALERT_FROM"},
                            "legal-py": set(),
                            "seo-py": set(), "compliance-py": set()}
        for svc, prefixes in PY_SERVICES.items():
            found = set()
            for p in (REPO / "services" / svc / "src").rglob("*.py"):
                found |= {n for n in ENV_READ.findall(p.read_text(encoding="utf-8"))
                          if not n.startswith(prefixes + ("LEDGER_SERVICE_",))}
            with self.subTest(service=svc):
                self.assertEqual(found, outside_prefixes[svc])


class Runbook(unittest.TestCase):
    def setUp(self):
        self.doc = DOC.read_text()

    def named(self, name: str) -> bool:
        return re.search(rf"(?<![A-Z0-9_]){re.escape(name)}(?![A-Z0-9_])", self.doc) is not None

    def test_every_variable_the_code_reads_is_documented(self):
        for component, names in code_variables().items():
            self.assertTrue(names, f"no variable found for {component} (the scan is broken)")
            missing = sorted(n for n in names if not self.named(n))
            with self.subTest(component=component):
                self.assertEqual(missing, [], f"{component} reads these, docs/deploy/railway.md names none of them")

    def test_every_variable_the_iac_sets_and_every_image_variable_is_documented(self):
        names = set(re.findall(r"^\s+([A-Z][A-Z0-9_]+):", IAC.read_text(), re.M))
        names |= {"RAILWAY_RUN_UID", "PORT", "DETECTION_BIND_ADDR", "DETECTION_PORT"}
        for df, *_r in SERVICES.values():
            args = entrypoint_args(REPO / df)
            names |= {args[i + 1][:-len("_FILE")] for i, a in enumerate(args) if a == "--secret-file"}
        self.assertEqual(sorted(n for n in names if not self.named(n)), [])

    def test_the_store_credential_paths_in_the_runbook_are_where_the_entrypoint_writes(self):
        # the connections JSON names the credential files by path; a path that drifted from the entrypoint's naming
        # rule would make every sync STORE_CREDENTIAL_UNREADABLE
        entry = ENTRY.read_text()
        self.assertIn("SECRET_DIR=/run/zbm-secrets\n", entry)
        self.assertIn("""target="$SECRET_DIR/$(printf '%s' "$file_var" | tr 'A-Z_' 'a-z-')\"""", entry)
        section = self.doc.split("### Revenue Recovery: connecting a real store\n", 1)[1].split("\n## ", 1)[0]
        for slot in ("RR_STORE_SECRET_1_FILE", "RR_STORE_SECRET_2_FILE"):
            with self.subTest(slot=slot):
                self.assertIn(f'"{entrypoint_secret_path(slot)}"', section)
        self.assertIn(entrypoint_secret_path("RR_STORE_CONNECTIONS_FILE"), self.doc)

    def store_section(self) -> str:
        return self.doc.split("### Revenue Recovery: connecting a real store\n", 1)[1].split("\n## ", 1)[0]

    def test_every_store_credential_path_in_the_runbook_json_is_its_slots(self):
        # AEGIS detection-branch M-4 (mutants M4, M11): each path is checked where it is used, per field, so one right
        # occurrence elsewhere cannot hide a wrong one
        blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", self.store_section(), re.S)]
        creds = [b["connections"][0]["credential"] if "connections" in b else b for b in blocks]
        kinds = sorted(c["kind"] for c in creds)
        self.assertEqual(kinds, ["client_credentials", "static_token"], "the step-9 JSON and the static-token example")
        want = {"client_id_file": entrypoint_secret_path("RR_STORE_SECRET_1_FILE"),
                "client_secret_file": entrypoint_secret_path("RR_STORE_SECRET_2_FILE"),
                "token_file": entrypoint_secret_path("RR_STORE_SECRET_1_FILE")}
        for c in creds:
            for field, path in c.items():
                if field != "kind":
                    with self.subTest(kind=c["kind"], field=field):
                        self.assertEqual(path, want[field])
        self.assertNotRegex(self.store_section(), r'"kind": "static_token", "token_file":\s*\n',
                            "every static-token credential is in a JSON block (checked above), none inline")

    def test_the_kill_file_path_is_one_path_everywhere(self):
        # AEGIS detection-branch M-4 (mutants M9, M10) and L-7: the variable table, step 8 (required) and the Stop
        # section name the same file; otherwise the at-once stop touches a file detection never checks
        table = re.search(r"^\| `RR_STORE_KILL_SWITCH_FILE` \|[^\n]*set it to `([^`]+)`[^\n]*`touch ([^`]+)`", self.doc, re.M)
        self.assertIsNotNone(table, "the detection variable table row")
        section = self.store_section()
        step8 = re.search(r"^8\. .*?(?=^9\. )", section, re.M | re.S).group(0)
        self.assertIn("**Required,\n   not optional:**", step8)
        in_step8 = re.findall(r"`RR_STORE_KILL_SWITCH_FILE` = `([^`]+)`", step8)
        stop = section.split("**Stop / disconnect.**", 1)[1]
        in_stop = re.findall(r"`touch ([^`]+)`", stop.split("\n\n", 1)[0])
        paths = {table.group(1), table.group(2), *in_step8, *in_stop}
        self.assertEqual(len(in_step8), 1)
        self.assertEqual(len(in_stop), 1)
        self.assertEqual(len(paths), 1, f"kill-file paths differ: {sorted(paths)}")
        anywhere = set(re.findall(r"/[A-Za-z0-9_./-]*\.kill\b", self.doc, re.I))
        self.assertEqual(anywhere, paths, "every kill-file path in the runbook")

    def test_ledger_callers_come_from_the_script_with_its_grants(self):
        # AEGIS detection-branch M-2 / M-4: the runbook runs devtools/ledger_callers.py (no inline copy that could
        # drift), and what it tells Andre each caller may do is the script's table
        import sys
        sys.path.insert(0, str(REPO / "devtools"))
        import ledger_callers as lc
        self.assertNotIn("hashlib.sha256(", self.doc, "no inline LEDGER_CALLERS generator")
        flat = " ".join(self.doc.split())
        rec_dep = "--current ~/zbm/ledger-callers.json --deployed ~/zbm/ledger-callers.deployed.json"
        for cmd in ("devtools/ledger_callers.py new --out ~/zbm/ledger-callers.json",
                    "devtools/ledger_callers.py check --current ~/zbm/ledger-callers.json",
                    f"devtools/ledger_callers.py compare {rec_dep}",
                    f"devtools/ledger_callers.py rotate seo-py {rec_dep} --out ~/zbm/ledger-callers.next.json"):
            self.assertIn(cmd, flat)
        adds = re.findall(r"ledger_callers\.py add detection-py (--[^`]*?)(?=`)", flat)
        self.assertEqual(adds, [f"{rec_dep} --out ~/zbm/ledger-callers.next.json"] * 2,
                         "both add commands pass the record AND the deployed copy (N-M1)")
        for caller, (dept, _where) in lc.CALLERS.items():
            with self.subTest(caller=caller):
                self.assertRegex(self.doc, rf"\n- {re.escape(caller)} writes `{dept}`[;,]")
        self.assertIn("(caller `detection-py`, department `rr_store`)", self.doc)
        smoke = re.search(r"the callers listed \(([^)]*), each with its one department\)", self.doc).group(1)
        self.assertEqual([c.strip() for c in smoke.split(",")], list(lc.CALLERS))
        probe_rows = re.findall(r"^\| [a-z-]+ \| `([a-z_]+)` \|$", self.doc, re.M)
        self.assertEqual(sorted(probe_rows), sorted(lc.KNOWN_DEPARTMENTS), "the per-department check covers each one")
        self.assertIn(f'open("{entrypoint_secret_path("DETECTION_LEDGER_TOKEN_FILE")}")', self.doc)

    def test_ledger_callers_is_not_sealed_and_the_deployed_value_is_the_truth(self):
        # AEGIS ledger-callers N-M1: LEDGER_CALLERS holds hashes only, so it is set NOT Sealed and read back; every
        # change starts by copying the deployed value and comparing the record with it
        flat = " ".join(self.doc.split())
        self.assertIn("except `LEDGER_CALLERS`: it holds hashes only and is set **not** Sealed", flat)
        self.assertIn("| `LEDGER_CALLERS` | yes | no (hashes only; set **not** Sealed) |", self.doc)
        self.assertIn("(keep the file it writes; **not** Sealed: it holds hashes only)", flat)
        self.assertIn("**Already Sealed?**", self.doc)
        self.assertIn("Railway cannot unseal a variable", flat)
        self.assertIn("Do not recreate it from a record you cannot prove is current", flat)
        change = self.doc.split("#### Changing `LEDGER_CALLERS`", 1)[1].split("**If the new ledger deployment", 1)[0]
        steps = re.findall(r"^(\d)\. \*\*([^*]+)\*\*", change, re.M)
        self.assertEqual([n for n, _t in steps], list("12345678"))
        self.assertTrue(steps[0][1].startswith("Copy the deployed value and compare"), steps[0])
        self.assertTrue(steps[1][1].startswith("Baseline"), steps[1])
        cflat = " ".join(change.split())
        self.assertIn("the deployed value wins", cflat)
        self.assertIn("refuse, before asking for any token, unless the record equals the deployed copy", cflat)
        self.assertIn("Copy it now, in this session; never reuse an old copy", cflat)

    def test_post_change_checks_do_not_roll_back_a_correct_change(self):
        # AEGIS ledger-callers N-M2: a baseline before any change; after the ledger redeploy only the UNCHANGED callers
        # decide a rollback; the changed caller's 401 / no-token is expected until its own redeploy, and a 401 after
        # that is fixed on that service, never by a ledger rollback
        change = self.doc.split("#### Changing `LEDGER_CALLERS`", 1)[1].split("**If the new ledger deployment", 1)[0]
        cflat = " ".join(change.split())
        step = {n: " ".join(body.split()) for n, body in
                re.findall(r"^(\d)\. (.*?)(?=^\d\. |^What each check must print)", change, re.M | re.S)}
        self.assertIn("Every one must print `200`. Anything else: **stop**", step["2"])
        self.assertIn("whose token you did *not* change", step["6"])
        self.assertIn("Any `401` or `403`: **roll back**", step["6"])
        self.assertIn("that is not a reason to roll back", step["6"])
        self.assertIn("A rotated service prints `401`", step["6"])
        self.assertIn("`rr_store no-token`", step["6"])
        self.assertIn("Make the new value the record", step["7"])
        self.assertIn("Fix **that service's token**, **not** with a ledger rollback", step["8"])
        self.assertNotIn("roll back", step["8"].split("`403`")[0].lower(), "a 401 after its redeploy is not a rollback")
        rows = {r[0]: r for r in re.findall(r"^\| (step \d[^|]*) \| ([^|]*) \| ([^|]*) \| ([^|]*) \|$", change, re.M)}
        self.assertEqual(len(rows), 3)
        base, after_ledger, after_service = (rows[k] for k in sorted(rows))
        self.assertIn("else stop", base[1])
        self.assertIn("else roll back", after_ledger[1])
        self.assertIn("not a rollback", after_ledger[2])
        self.assertIn("not a rollback", after_ledger[3])
        self.assertIn("fix its token, not the ledger", after_service[2])
        self.assertIn('raise SystemExit(d + " no-token")', self.doc, "no traceback for a missing token file")
        self.assertIn("**Rollback** (step 6 only, before step 7)", cflat)
        self.assertIn("pass on 3.9 to 3.13", " ".join(self.doc.split()))
        self.assertIn("`getpass` shows what you type when it is not attached to a terminal", " ".join(self.doc.split()))

    def test_every_dockerfile_and_the_unverified_xff_note(self):
        for df, *_r in SERVICES.values():
            self.assertIn(df, self.doc)
        self.assertRegex(self.doc, r"DASHBOARD_TRUSTED_PROXY_HOPS[^\n]*\n?[^\n]*UNVERIFIED")
        self.assertIn("/webhooks/stripe", self.doc)


# A fake Search Console key shaped like the real download (multi-line JSON, ~2.3 KB, a PEM with \n escapes) plus the
# characters a careless write would mangle (%, $, backquote, backslash, quotes). Not a key: the PEM body is a word.
FAKE_GSC_KEY = (
    '{\n  "type": "service_account",\n  "project_id": "fake-project",\n'
    '  "private_key_id": "0123456789abcdef0123456789abcdef01234567",\n'
    '  "private_key": "-----BEGIN ' + 'PRIVATE KEY-----\\nFAKE-NOT-A-KEY\\n-----END ' + 'PRIVATE KEY-----\\n",\n'
    '  "client_email": "fake@example.iam.gserviceaccount.com",\n'
    '  "token_uri": "https://oauth2.googleapis.com/token",\n'
    '  "note": "%s %d $HOME `id` \\\\ \\" \' -n",\n'
    '  "padding": "' + "x" * 1900 + '"\n}'
)
GSC_CHILD = ('if [ -n "${SEO_GSC_SERVICE_ACCOUNT+x}" ]; then echo VALUE-LEAKED; exit 3; fi; '
             'if [ -z "${SEO_GSC_SERVICE_ACCOUNT_FILE+x}" ]; then echo NO-FILE-VARIABLE; exit 0; fi; '
             'stat -c "%u %a" "$SEO_GSC_SERVICE_ACCOUNT_FILE" >&2; cat "$SEO_GSC_SERVICE_ACCOUNT_FILE"')


class SeoSearchConsoleKey(unittest.TestCase):
    """golive-gsc: on Railway the Search Console key is a variable; the seo image has the entrypoint write it to the
    file seo reads (SEO_GSC_SERVICE_ACCOUNT -> SEO_GSC_SERVICE_ACCOUNT_FILE), and an unset key still starts seo
    (NOT_CONNECTED). The entrypoint is run for real, with only its secret directory moved into a temporary one."""

    def test_the_seo_image_maps_the_gsc_key_file_and_the_iac_preserves_its_variable(self):
        args = entrypoint_args(REPO / SERVICES["seo"][0])
        files = [args[i + 1] for i, a in enumerate(args) if a == "--secret-file"]
        self.assertIn("SEO_GSC_SERVICE_ACCOUNT_FILE", files)
        self.assertIn("SEO_LOG_HASH_KEY_FILE", files)
        self.assertLess(args.index("SEO_GSC_SERVICE_ACCOUNT_FILE"), args.index("--"))
        block = iac_services()["seo"]
        self.assertRegex(block, r"(?m)^\s+SEO_GSC_SERVICE_ACCOUNT:\s*preserve\(\)")
        self.assertNotRegex(block, r"(?m)^\s+SEO_GSC_SERVICE_ACCOUNT_FILE:")  # set by the entrypoint, never Railway
        self.assertRegex(block, r'(?m)^\s+RAILWAY_RUN_UID:\s*"0"')  # the entrypoint writes secrets only as root
        doc = DOC.read_text()
        self.assertIn("| `SEO_GSC_SERVICE_ACCOUNT` (optional) | `SEO_GSC_SERVICE_ACCOUNT_FILE` | seo |", doc)
        self.assertNotIn("Search Console is NOT_CONNECTED on Railway", doc)  # the open item is closed
        # seo's own reader: the file is optional and may be up to 16 KiB (a real key is ~2.3 KB)
        cfg = (REPO / "services" / "seo-py" / "src" / "config.py").read_text()
        self.assertIn('_secret_file_bytes(env, "SEO_GSC_SERVICE_ACCOUNT_FILE", max_bytes=16 * 1024)', cfg)
        self.assertIn('if not (env.get("SEO_GSC_SERVICE_ACCOUNT_FILE") or "").strip():\n        return', cfg)

    def run_entrypoint(self, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        tmp.chmod(0o711)                                   # uid 10001 must reach the secret directory inside
        secret_dir = tmp / "zbm-secrets"
        secret_dir.mkdir(mode=0o700)                       # the image creates it (seo Dockerfile)
        text = ENTRY.read_text()
        self.assertEqual(text.count("SECRET_DIR=/run/zbm-secrets\n"), 1)
        script = tmp / "zbm-entrypoint.sh"
        script.write_text(text.replace("SECRET_DIR=/run/zbm-secrets\n", f"SECRET_DIR={secret_dir}\n"))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra_env}
        return subprocess.run(["sh", str(script), "--secret-file", "SEO_GSC_SERVICE_ACCOUNT_FILE", "--",
                               "sh", "-c", GSC_CHILD], env=env, capture_output=True, timeout=30)

    def test_no_key_set_starts_without_a_file(self):
        for extra in ({}, {"SEO_GSC_SERVICE_ACCOUNT": ""}):
            with self.subTest(env=extra):
                r = self.run_entrypoint(extra)
                self.assertEqual((r.returncode, r.stdout), (0, b"NO-FILE-VARIABLE\n"), r.stderr)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("setpriv"),
                         "writes a secret only as root (the railway-images smoke covers it in CI)")
    def test_a_multi_line_json_key_reaches_the_file_byte_exact_owner_only(self):
        r = self.run_entrypoint({"SEO_GSC_SERVICE_ACCOUNT": FAKE_GSC_KEY})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.decode().strip(), "10001 600")   # owned by the service user, mode 0600
        self.assertGreater(len(FAKE_GSC_KEY), 2000)
        self.assertEqual(r.stdout, FAKE_GSC_KEY.encode())         # byte-identical, value not in the child's env
        # trailing newlines are dropped (shell command substitution); seo strips the key file's whitespace anyway
        r = self.run_entrypoint({"SEO_GSC_SERVICE_ACCOUNT": FAKE_GSC_KEY + "\n"})
        self.assertEqual((r.returncode, r.stdout), (0, FAKE_GSC_KEY.encode()), r.stderr)


if __name__ == "__main__":
    unittest.main()
