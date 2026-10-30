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
import re
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
}
PY_SERVICES = {"detection-py": ("ZBM_", "DETECTION_", "RR_STORE_"), "finance-py": ("FIN_",), "legal-py": ("LEGAL_",),
               "seo-py": ("SEO_",)}
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
                self.assertEqual(pins((REPO / "services" / svc / "requirements.txt").read_text()), want)
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


REF = re.compile(r"\$\{\{[a-z][a-z0-9-]*\.[A-Z][A-Z0-9_]*\}\}")


def secret_value_ok(value: str) -> bool:
    """A secret in the IaC is preserve(), a reference (`finance.env.X`), or a JSON map literal whose EVERY value is
    exactly one `${{service.VARIABLE}}` reference (AEGIS L5): a literal secret placed next to a reference is refused."""
    if re.fullmatch(r"preserve\(\)|[a-z][A-Za-z]*\.env\.[A-Z0-9_]+", value):
        return True
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
        self.assertEqual(set(self.blocks), set(SERVICES))
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
        # delete the secret on the next apply (and with it the feature: a ledger caller, a Stripe key, a store)
        for svc, (df, *_r) in SERVICES.items():
            for file_var in secret_files(REPO / df):
                with self.subTest(service=svc, variable=file_var):
                    self.assertTrue(file_var.endswith("_FILE"))
                    self.assertTrue(self._declared_preserved(svc, file_var[:-len("_FILE")]),
                                    f"{file_var[:-5]} is not preserve() on {svc}")

    def test_detection_store_connector_is_wired_without_a_volume(self):
        # AEGIS A2 H-2: the store connector needs its ledger token, the connections JSON and the credential files; the
        # service keeps no state (snapshots in memory), so no volume and no secret on a backed-up disk
        want = ["DETECTION_LEDGER_TOKEN_FILE", "RR_STORE_CONNECTIONS_FILE", "RR_STORE_SECRET_1_FILE",
                "RR_STORE_SECRET_2_FILE"]
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
                q('{"rail_gateway": "${{stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_X}}"}')]
        bad = [q('{"a": "${{x.Y}}", "b": "a-literal-secret-value"}'), q('{"a": "prefix-${{x.Y}}"}'),
               q('{"a": "${{x.Y}}${{x.Z}}"}'), q("{}"), q('{"a": "${{x.Y}}", "b": {"c": "literal"}}'),
               q("{... ${{x.Y}} ...}"), '"a-literal-secret"', "ctx.randomString('x')",
               q('{"a": "${{x.Y}}"}') + " + secret"]
        for v in good:
            self.assertTrue(secret_value_ok(v), v)
        for v in bad:
            self.assertFalse(secret_value_ok(v), v)


class CiAndSmoke(unittest.TestCase):
    def test_ci_builds_every_dockerfile_and_the_smoke_covers_it(self):
        ci = CI.read_text()
        smoke = SMOKE.read_text()
        for svc, (df, _pv, port, name, _v) in SERVICES.items():
            with self.subTest(service=svc):
                self.assertIn(f"docker build -f {df} -t zbm-ci/{name}:ci .", ci)
                self.assertRegex(smoke, rf'"{re.escape(name)}": \({port}, ')
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
        outside_prefixes = {"detection-py": set(), "finance-py": set(), "legal-py": set(), "seo-py": set()}
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


if __name__ == "__main__":
    unittest.main()
