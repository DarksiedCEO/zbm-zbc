"""Reviewer's own mutants over agents/farrah.py and svc_attribution.py (>= 20). Each mutant: one textual change in a
fresh tar copy of the tree; the test set is run; killed = any test fails. Usage:
  python mutants_rv.py <commit-sha> <out.json> [suite: rv|builder|both]"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

F = "services/seo-py/src/agents/farrah.py"
S = "services/seo-py/src/svc_attribution.py"
MUTANTS = [
    ("float_amount", F, "    return d\n\n\ndef exact_sum", "    return Decimal(float(d))\n\n\ndef exact_sum"),
    ("band_swapped", F, '"figure": text(figure), "lower": text(lower), "upper": text(upper)',
     '"figure": text(figure), "lower": text(upper), "upper": text(lower)'),
    ("unknown_out_of_U", F, "upper = SUM_CTX.add(SUM_CTX.add(figure, u_pos), widen)", "upper = SUM_CTX.add(figure, widen)"),
    ("unknown_net_in_U", F, "upper = SUM_CTX.add(SUM_CTX.add(figure, u_pos), widen)",
     "upper = SUM_CTX.add(SUM_CTX.add(figure, r_unknown), widen)"),                      # round 3: the pre-OD-2 formula
    ("unknown_neg_out_of_L", F, "lower = SUM_CTX.add(figure, u_neg)", "lower = figure"),  # round 3: the pre-OD-5 floor
    ("high_rows_31", F, "HIGH_ROWS, MEDIUM_ROWS = 30, 10", "HIGH_ROWS, MEDIUM_ROWS = 31, 10"),
    ("medium_rows_9", F, "HIGH_ROWS, MEDIUM_ROWS = 30, 10", "HIGH_ROWS, MEDIUM_ROWS = 30, 9"),
    ("high_width_011", F, 'HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.10"), Decimal("0.35")',
     'HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.11"), Decimal("0.35")'),
    ("medium_width_034", F, 'HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.10"), Decimal("0.35")',
     'HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.10"), Decimal("0.34")'),
    ("thresholds_swapped", F, 'HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.10"), Decimal("0.35")',
     'HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.35"), Decimal("0.10")'),
    ("high_strict_lt", F, "and width <= SUM_CTX.multiply(HIGH_WIDTH, upper)", "and width < SUM_CTX.multiply(HIGH_WIDTH, upper)"),
    ("partial_month", F, 'return parse_date(w["start"]) <= first and parse_date(w["end"]) >= last',
     'return parse_date(w["start"]) <= last and parse_date(w["end"]) >= first'),
    ("plan_gate_removed", F, "return tid == own_tenant or plan_name in ALLOWED_PLANS", "return True"),
    ("entry_allowed", F, 'ALLOWED_PLANS = ("growth", "full")', 'ALLOWED_PLANS = ("entry", "growth", "full")'),
    ("tenant_ignored", F, '    if d["tenant_id"] != tid:\n        raise InputRefused("DATASET_TENANT_MISMATCH")', "    pass"),
    ("hmac_unchecked", F, 'and type(r.get("dataset_hmac")) is str and verify(tid, doc, r["dataset_hmac"])',
     'and type(r.get("dataset_hmac")) is str'),
    ("quarantine_ignored", F, "            return False                                     # a dataset of a quarantined read: never used",
     "            continue"),
    # round 2 (F-1): the exact absolute value is ``copy_abs()``; the mutant removes it (and one puts the built-in back)
    ("recon_no_abs", F, 'd = amount(it["values"]["difference"]).copy_abs()', 'd = amount(it["values"]["difference"])'),
    ("recon_builtin_abs", F, 'd = amount(it["values"]["difference"]).copy_abs()', 'd = abs(amount(it["values"]["difference"]))'),
    ("recon_no_share", F, "term = CTX.divide(SUM_CTX.multiply(recon, figure), basis_total)", "term = recon"),
    ("sampled_not_low", F, "            force_low = True\n            applied.append(\"incomplete:sampled\")",
     "            applied.append(\"incomplete:sampled\")"),
    ("dropped_norev_not_low", F, "            if rev is None:\n                force_low = True", "            if rev is None:\n                pass"),
    ("other_row_not_widened", F, "widen = SUM_CTX.add(widen, max(other_rows, Decimal(0)))", "pass"),
    ("high_without_fresh", F, 'and not flagged and state == "FRESH" and rows >= HIGH_ROWS', "and not flagged and rows >= HIGH_ROWS"),
    ("stale_allowed", F, 'INSUFFICIENT_STATES = ("STALE", "FAILED_LAST", "NOT_CONNECTED")', 'INSUFFICIENT_STATES = ("FAILED_LAST", "NOT_CONNECTED")'),
    ("u_zero_figure_zero", F, "if reasons or upper.is_zero():", "if reasons:"),
    ("month_filter_removed", F, "or month_of(local_day(r[day], tz)) != month:", ":"),
    ("tz_ignored", F, "    return dt.astimezone(tz).date()", "    return dt.date()"),
    ("no_strip", F, "return value.strip().lower()", "return value.lower()"),
    ("ai_substring", F, "    if source in AI_REFERRERS:\n        return \"ai_referral\"\n    if medium is None",
     "    if source and any(h in source for h in AI_REFERRERS):\n        return \"ai_referral\"\n    if medium is None"),
    ("crm_lost_counted", F, 'if basis == "crm" and r["stage"] != "won":', 'if basis == "crm" and r["stage"] == "open":'),
    ("lag_2_days", F, "DATA_LAG_DAYS = 3 ", "DATA_LAG_DAYS = 2 "),
    ("refunds_dropped", F, "            v = amount(r[field])\n", "            v = amount(r[field])\n            if v < 0:\n                continue\n"),
    ("compliance_gets_result", S, 'if who == "compliance_38" or r["status"] != "completed":', 'if r["status"] != "completed":'),
    ("view_tenant_unchecked", S, 'if r is None or r["tenant_id"] != tid:', "if r is None:"),
    ("plan_not_rechecked_at_commit", S, "if stop is None and not farrah.plan_allows(tid, self.plan_name(tid), self._own_tenant()):",
     "if False:"),
    ("kill_not_rechecked_at_commit", S, 'stop = self.kill_code(tenant=tid, capability=CAPABILITY, write=True) or \\',
     'stop = None and self.kill_code(tenant=tid, capability=CAPABILITY, write=True) or \\'),
    ("guard_record_identity", S, "swept = secret_guard.guard_record(skel)", "swept = skel"),
    ("month_closed_removed", S, "if not farrah.month_closed(month, self.now(), tz):", "if False:"),
    ("in_progress_removed", S, 'raise Conflict(R("RUN_IN_PROGRESS"))', "pass"),
    ("vault_precheck_removed", S, "                if not self.vault.key_configured:\n                    raise Unavailable(R(\"VAULT_UNAVAILABLE\"))       # AC-10: refused before any input is read",
     "                pass"),
    ("amounts_in_ledger", F, '"limitations": reasons,', '"limitations": reasons + [totals["basis_revenue"] or "0"],'),
]


def main():
    sha, out = sys.argv[1], sys.argv[2]
    suite = sys.argv[3] if len(sys.argv) > 3 else "both"
    only = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else None
    rv = os.path.dirname(os.path.abspath(__file__))
    py = os.environ["RV_PY"]
    results = {}
    for name, path, old, new in MUTANTS:
        if only and name not in only:
            continue
        tmp = tempfile.mkdtemp(prefix="rvmut-", dir=os.environ.get("RV_TMP"))
        try:
            subprocess.run(f"git -C /home/claude/zbm-zbc archive {sha} | tar -x -C {tmp}", shell=True, check=True)
            p = os.path.join(tmp, path)
            src = open(p).read()
            if src.count(old) != 1:
                results[name] = "NOT_APPLIED"
                continue
            open(p, "w").write(src.replace(old, new))
            seo = os.path.join(tmp, "services/seo-py")
            os.symlink(rv, os.path.join(seo, "tests/conformance/farrah_rv"))
            tests = []
            if suite in ("builder", "both"):
                tests.append("tests/conformance/farrah")
            if suite in ("rv", "both"):
                tests.append("tests/conformance/farrah_rv")
            env = {**os.environ, "RV_N": os.environ.get("RV_MUT_N", "300"), "RV_FORGE_N": "200",
                   "RV_AC1_N": "60", "RV_AC2_N": "40"}
            r = subprocess.run([py, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "not rv_finding", *tests], cwd=seo, env=env,
                               capture_output=True, text=True, timeout=3000)
            tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            results[name] = ("KILLED" if r.returncode != 0 else "SURVIVED") + " | " + tail[:160]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        json.dump(results, open(out, "w"), indent=1)
        print(name, results[name], flush=True)


if __name__ == "__main__":
    main()
