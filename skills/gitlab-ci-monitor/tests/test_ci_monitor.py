import argparse, json, math, os, signal, stat, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/gitlab-ci-monitor/scripts/ci_monitor.py"
sys.path.insert(0, str(SCRIPT.parent))
import ci_monitor as cm  # noqa: E402

SHA = "a" * 40
PID_ = 15  # canonical numeric project id used by the fakes


def job(id, name, status="success", stage="test", runner=7, allow_failure=False, sha=SHA, pipeline_id=5,
        project_id=PID_, commit=True, **kw):
    d = {"id": id, "name": name, "status": status, "stage": stage, "allow_failure": allow_failure,
         "duration": 1.5, "runner": {"id": runner} if runner is not None else None,
         "pipeline": {"id": pipeline_id, "project_id": project_id, "sha": sha}}
    if commit:
        d["commit"] = {"id": sha}
    d.update(kw)
    return d


def pipe(status="success", sha=SHA, id=5, project_id=PID_):
    return {"id": id, "status": status, "sha": sha, "project_id": project_id}


def ev(jobs, required=("a",), pipeline=None, rules=None, sha=SHA, pid=PID_):
    return cm.evaluate(pipeline or pipe(), jobs, list(required), sha, rules or {}, 5, pid)


def ok(obj):
    return (0, json.dumps(obj).encode(), b"")


class FakeGitLab:
    """Replacement for GlabClient._exec that routes by endpoint (read-only)."""
    def __init__(self, pipeline=None, jobs=None, history=None, job_pages=None, errors=None, hook=None,
                 project=(PID_, "g/p")):
        self.pipeline = pipeline or pipe()
        self.jobs = jobs if jobs is not None else [job(1, "a")]
        self.history = history if history is not None else self.jobs
        self.job_pages = job_pages
        self.errors = errors or {}      # substring -> list of responses (consumed first)
        self.hook = hook                # called as hook(endpoint)
        self.project = project
        self.calls = []

    def __call__(self, argv, timeout):
        ep = argv[-1]
        self.calls.append((argv, timeout))
        if self.hook:
            self.hook(ep)
        for sub, q in self.errors.items():
            if sub in ep and q:
                r = q.pop(0) if len(q) > 1 else q[0]
                if isinstance(r, BaseException):
                    raise r
                return r
        if "/jobs" in ep:
            page = int(ep.split("&page=")[1].split("&")[0])
            if "include_retried=true" in ep:
                src = self.history
                return ok(src[(page - 1) * 100:page * 100])
            if self.job_pages is not None:
                return ok(self.job_pages[page - 1] if page <= len(self.job_pages) else [])
            return ok(self.jobs[(page - 1) * 100:page * 100])
        if "/pipelines/" in ep:
            return ok(self.pipeline)
        return ok({"id": self.project[0], "path_with_namespace": self.project[1]})


def args_for(command="watch", **over):
    ns = dict(command=command, host="h.example", project="g/p", pipeline=5, sha=SHA, job=["a"],
              runner_id=None, poll_interval=60, max_duration=3600, request_timeout=5, retries=0,
              output=None, overwrite=False, lock_dir=None, glab_bin="glab")
    ns.update(over)
    return argparse.Namespace(**ns)


def run_with(fake, clock=None, sleep=None, **over):
    over.setdefault("lock_dir", tempfile.mkdtemp())
    real = cm.GlabClient._exec
    cm.GlabClient._exec = lambda self, argv, timeout: fake(argv, timeout)
    try:
        kw = {"log": lambda m: None, "sleep": sleep or (lambda s: None)}
        if clock:
            kw["clock"] = clock
        return cm.run(args_for(**over), **kw)
    finally:
        cm.GlabClient._exec = real


def make_client(fake, deadline=None, retries=3, sleeps=None, timeout=5):
    c = cm.GlabClient("h.example", retries=retries, request_timeout=timeout,
                      deadline=deadline or cm.Deadline(100),
                      sleep=(sleeps.append if sleeps is not None else lambda s: None))
    c._exec = fake
    return c


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ------------------------------------------------------------------ evaluate
class EvaluateTests(unittest.TestCase):
    def test_all_required_success(self):
        o, d = ev([job(1, "a"), job(2, "b")], ("a", "b"))
        self.assertEqual(o, "PASSED")
        self.assertTrue(d["pipeline_sha_verified"])

    def test_failed_required_despite_green_pipeline(self):
        self.assertEqual(ev([job(1, "a", "failed")], pipeline=pipe("success"))[0], "FAILED")

    def test_allowed_failure_required_failed(self):
        self.assertEqual(ev([job(1, "a", "failed", allow_failure=True)])[0], "FAILED")

    def test_allowed_failure_required_passes_when_success(self):
        self.assertEqual(ev([job(1, "a", "success", allow_failure=True)])[0], "PASSED")

    def test_optional_manual_ignored(self):
        self.assertEqual(ev([job(1, "a"), job(2, "opt", "manual")])[0], "PASSED")

    def test_required_manual_action_required(self):
        self.assertEqual(ev([job(1, "a", "manual")], pipeline=pipe("manual"))[0], "ACTION_REQUIRED")

    def test_required_skipped_not_accepted(self):
        self.assertEqual(ev([job(1, "a", "skipped")])[0], "FAILED")

    def test_missing_required_job(self):
        o, d = ev([job(1, "b")])
        self.assertEqual(o, "FAILED")
        self.assertIn("not present", d["jobs"][0]["reason"])

    def test_missing_job_while_running_keeps_waiting(self):
        self.assertIsNone(ev([job(1, "b")], pipeline=pipe("running"))[0])

    def test_pending_states_keep_waiting(self):
        for st in ("created", "pending", "preparing", "running"):
            self.assertIsNone(ev([job(1, "a", st)], pipeline=pipe("running"))[0])

    def test_required_subset_passes_while_unrelated_pending(self):
        o, _ = ev([job(1, "a"), job(2, "other", "running")], pipeline=pipe("running"))
        self.assertEqual(o, "PASSED")

    def test_same_name_same_stage_ambiguous(self):
        o, d = ev([job(1, "a"), job(2, "a")])
        self.assertEqual(o, "AMBIGUOUS_JOB")
        self.assertEqual(d["jobs"][0]["candidate_job_ids"], [1, 2])

    def test_same_name_different_stage_ambiguous(self):
        self.assertEqual(ev([job(1, "a", stage="build"), job(2, "a", stage="test")])[0], "AMBIGUOUS_JOB")

    def test_contradictory_retried_flag_in_current_set(self):
        self.assertEqual(ev([job(1, "a", retried=True)])[0], "AMBIGUOUS_JOB")

    def test_current_failed_attempt_historical_success_not_accepted(self):
        # authoritative current set holds only the failed attempt; history never decides
        self.assertEqual(ev([job(9, "a", "failed")])[0], "FAILED")

    def test_pipeline_sha_mismatch(self):
        self.assertEqual(ev([job(1, "a")], pipeline=pipe(sha="b" * 40))[0], "CANDIDATE_MISMATCH")

    def test_pipeline_id_and_project_mismatch(self):
        self.assertEqual(ev([job(1, "a")], pipeline=pipe(id=6))[0], "CANDIDATE_MISMATCH")
        self.assertEqual(ev([job(1, "a")], pipeline=pipe(project_id=99))[0], "CANDIDATE_MISMATCH")

    def test_job_identity_fail_closed(self):
        cases = [job(1, "a", pipeline_id=6), job(1, "a", sha="b" * 40), job(1, "a", project_id=99),
                 job(1, "a", commit=False, pipeline=None), job(1, "a", pipeline={"id": 5}),
                 {**job(1, "a"), "commit": {"id": "c" * 40}}]
        for j in cases:
            o, d = ev([j])
            self.assertEqual(o, "CANDIDATE_MISMATCH", d["jobs"][0])
            self.assertEqual(d["jobs"][0]["verdict"], "IDENTITY")

    def test_identity_beats_failed_status(self):
        self.assertEqual(ev([job(1, "a", "failed", sha="b" * 40)])[0], "CANDIDATE_MISMATCH")

    def test_runner_rules(self):
        self.assertEqual(ev([job(1, "a", runner=3)], rules={"a": 7})[0], "RUNNER_MISMATCH")
        o, d = ev([job(1, "a", runner=7)], rules={"a": 7})
        self.assertEqual((o, d["jobs"][0]["runner_constraint"]), ("PASSED", 7))
        self.assertEqual(ev([job(1, "a", runner=None)], rules={"a": 7})[0], "RUNNER_MISMATCH")


class RunnerRuleTests(unittest.TestCase):
    def test_global_expands_to_all_required(self):
        self.assertEqual(cm.parse_runner_rules(["6"], ["a", "b"]), {"a": 6, "b": 6})

    def test_per_job(self):
        self.assertEqual(cm.parse_runner_rules(["a=6"], ["a", "b"]), {"a": 6})

    def test_typo_job_rejected(self):
        with self.assertRaises(cm.UsageError):
            cm.parse_runner_rules(["aa=6"], ["a"])

    def test_invalid_ids_rejected(self):
        for bad in ("0", "-1", "abc", "1.5", "a=", "a=0", "+5", " 5", "٥", "a=x", ""):
            with self.assertRaises(cm.UsageError, msg=bad):
                cm.parse_runner_rules([bad], ["a"])

    def test_contradictions_rejected(self):
        for rules in (["a=1", "a=2"], ["1", "2"], ["1", "a=1"]):
            with self.assertRaises(cm.UsageError, msg=rules):
                cm.parse_runner_rules(rules, ["a"])

    def test_typo_surfaces_as_usage_error_in_run(self):
        res = run_with(FakeGitLab(), runner_id=["typo=6"])
        self.assertEqual(res["outcome"], "USAGE_ERROR")

    def test_constraints_recorded_in_result(self):
        res = run_with(FakeGitLab(jobs=[job(1, "a", runner=6)]), runner_id=["6"])
        self.assertEqual(res["runner_constraints"], {"a": 6})
        self.assertEqual(res["outcome"], "PASSED")


# -------------------------------------------------------------------- client
class ClientTests(unittest.TestCase):
    def test_pagination_multi_page_and_numeric_project(self):
        f = FakeGitLab(jobs=[job(i, "j%d" % i) for i in range(1, 106)])
        c = make_client(f)
        self.assertEqual(len(c.jobs(PID_, 5)), 105)
        self.assertIn("page=2", f.calls[1][0][-1])
        self.assertIn("projects/15/", f.calls[0][0][-1])

    def test_duplicate_identical_records_across_pages_deduplicated(self):
        p1 = [job(i, "j%d" % i) for i in range(1, 101)]
        p2 = [p1[-1], job(500, "x")]  # shifted pagination repeats one record
        c = make_client(FakeGitLab(job_pages=[p1, p2]))
        jobs = c.jobs(PID_, 5)
        self.assertEqual(len(jobs), 101)
        self.assertEqual(len({j["id"] for j in jobs}), 101)

    def test_conflicting_duplicate_is_error(self):
        p1 = [job(i, "j%d" % i) for i in range(1, 101)]
        other = dict(p1[0], status="failed")
        c = make_client(FakeGitLab(job_pages=[p1, [other]]))
        with self.assertRaises(cm.ApiError) as e:
            c.jobs(PID_, 5)
        self.assertEqual(e.exception.kind, "inconsistent_pagination")

    def test_non_advancing_pages_are_error(self):
        p1 = [job(i, "j%d" % i) for i in range(1, 101)]
        c = make_client(FakeGitLab(job_pages=[p1, p1]))
        with self.assertRaises(cm.ApiError) as e:
            c.jobs(PID_, 5)
        self.assertEqual(e.exception.kind, "inconsistent_pagination")

    def test_page_limit_bounded(self):
        full = [job(i, "j") for i in range(1, 101)]
        pages = [[dict(j, id=j["id"] + 1000 * n) for j in full] for n in range(cm.MAX_PAGES + 1)]
        c = make_client(FakeGitLab(job_pages=pages))
        with self.assertRaises(cm.ApiError) as e:
            c.jobs(PID_, 5)
        self.assertEqual(e.exception.kind, "too_many_pages")

    def test_schema_validation(self):
        for bad in ([{"id": "1", "name": "a", "status": "success"}],
                    [{"id": 1, "name": 5, "status": "success"}],
                    [{"id": True, "name": "a", "status": "success"}],
                    [{"id": 1, "name": "a", "status": "success", "runner": {"id": "x"}}],
                    [{"id": 1, "name": "a", "status": "success", "pipeline": "x"}],
                    ["notadict"], {"not": "list"}):
            c = make_client(FakeGitLab(job_pages=[bad]))
            with self.assertRaises(cm.ApiError, msg=str(bad)) as e:
                c.jobs(PID_, 5)
            self.assertEqual(e.exception.kind, "schema")
        with self.assertRaises(cm.ApiError):
            make_client(lambda a, t: ok([1])).pipeline(PID_, 5)
        with self.assertRaises(cm.ApiError):
            make_client(lambda a, t: ok({"id": 15, "path_with_namespace": 3})).project("g/p")

    def test_project_path_must_match_returned_path(self):
        for requested in ("g/p", "G/P", "g/p.git", "/g/p/"):  # documented normalization
            self.assertEqual(make_client(FakeGitLab()).project(requested), (15, "g/p"))
        for returned in ("foreign/project", "g/p2", "other/p", "g/p/sub"):
            with self.assertRaises(cm.ApiError, msg=returned) as e:
                make_client(FakeGitLab(project=(15, returned))).project("g/p")
            self.assertEqual(e.exception.kind, "project_mismatch")
            self.assertTrue(e.exception.permanent)

    def test_redirected_project_never_passes(self):
        res = run_with(FakeGitLab(project=(99, "foreign/project")))
        self.assertEqual(res["outcome"], "API_ERROR")
        self.assertIn("project_mismatch", res["error"])
        self.assertNotEqual(res["required_job_acceptance"]["status"], "ACCEPTED")
        self.assertIsNone(res["project"]["id"])
        for cmd, job_ in (("status", None), ("status", ["a"])):
            res = run_with(FakeGitLab(project=(99, "foreign/project")), command=cmd, job=job_)
            self.assertEqual(res["outcome"], "API_ERROR")

    def test_numeric_alias_must_match_returned_id(self):
        with self.assertRaises(cm.ApiError):
            make_client(FakeGitLab(project=(16, "g/p"))).project("15")
        self.assertEqual(make_client(FakeGitLab()).project("15"), (15, "g/p"))
        self.assertEqual(make_client(FakeGitLab()).project("g/p"), (15, "g/p"))

    def test_empty_malformed_and_exit_status_first(self):
        for resp, kind in (((0, b"  ", b""), "empty_response"), ((0, b"{nope", b""), "malformed_json"),
                           ((1, b'{"ok": true}', b"x"), "cli_error")):
            c = make_client(lambda a, t, r=resp: r, retries=0)
            with self.assertRaises(cm.ApiError) as e:
                c.get("x")
            self.assertEqual(e.exception.kind, kind)

    def test_classification_and_permanence(self):
        for text, kind, perm in ((b"HTTP 401 Unauthorized", "auth", True), (b"403 Forbidden", "permission", True),
                                 (b"404 Not Found", "not_found", True), (b"HTTP 429", "rate_limited", False),
                                 (b"HTTP 503", "server_error", False), (b"weird", "cli_error", False)):
            n = []
            c = make_client(lambda a, t, x=text: (n.append(1), (1, x, b""))[1], retries=2)
            with self.assertRaises(cm.ApiError) as e:
                c.get("x")
            self.assertEqual((e.exception.kind, e.exception.permanent), (kind, perm))
            self.assertEqual(len(n), 1 if perm else 3)

    def test_transient_retry_then_success_with_backoff(self):
        sleeps, seq = [], [(1, b"", b"HTTP 502"), (1, b"", b"HTTP 502"), ok({"a": 1})]
        c = make_client(lambda a, t: seq.pop(0), sleeps=sleeps)
        self.assertEqual(c.get("x"), {"a": 1})
        self.assertEqual(sleeps, [1, 2])

    def test_request_timeout_is_transient_and_finite(self):
        n = []
        def fn(a, t):
            n.append(1)
            raise subprocess.TimeoutExpired("glab", 1)
        c = make_client(fn, retries=1)
        with self.assertRaises(cm.ApiError) as e:
            c.get("x")
        self.assertEqual((e.exception.kind, len(n)), ("timeout", 2))

    def test_request_timeout_clipped_to_remaining_deadline(self):
        clk = Clock()
        d = cm.Deadline(10, clock=clk)
        clk.t = 8.0
        f = FakeGitLab()
        make_client(f, deadline=d, timeout=30).get("x")
        self.assertAlmostEqual(f.calls[0][1], 2.0)

    def test_retry_delay_respects_deadline(self):
        clk = Clock()
        d = cm.Deadline(10, clock=clk)
        sleeps = []
        c = make_client(lambda a, t: (1, b"", b"HTTP 500"), deadline=d, retries=10, sleeps=sleeps)
        clk.t = 9.0
        with self.assertRaises(cm.TimedOut):
            c.get("x")
        self.assertEqual(sleeps, [])

    def test_only_get_requests(self):
        f = FakeGitLab()
        c = make_client(f)
        c.project("g/p"); c.pipeline(PID_, 5); c.jobs(PID_, 5); c.jobs(PID_, 5, include_retried=True)
        for argv, _ in f.calls:
            self.assertEqual(argv[argv.index("-X") + 1], "GET")
            self.assertEqual(argv[:2], ["glab", "api"])
            self.assertIn("--hostname", argv)
            for bad in ("-f", "-F", "--field", "--raw-field", "--input", "POST", "PUT", "DELETE"):
                self.assertNotIn(bad, argv)
            for bad in ("/retry", "/play", "/cancel", "/erase"):
                self.assertNotIn(bad, argv[-1])

    def test_project_path_url_encoded(self):
        f = FakeGitLab(project=(15, "acme/webapp"))
        make_client(f).project("acme/webapp")
        self.assertEqual(f.calls[0][0][-1], "projects/acme%2Fwebapp")


# ----------------------------------------------------------------------- run
class RunTests(unittest.TestCase):
    def test_passed_end_to_end_and_distinct_statuses(self):
        res = run_with(FakeGitLab(pipeline=pipe("running")))
        self.assertEqual(res["outcome"], "PASSED")
        self.assertEqual(res["overall_pipeline"]["status"], "running")
        self.assertEqual(res["required_job_acceptance"]["status"], "ACCEPTED")
        self.assertEqual(res["project"]["id"], PID_)
        self.assertTrue(res["poll_details"]["current"])
        self.assertTrue(res["missing_evidence"])

    def test_history_is_reporting_only(self):
        hist = [job(1, "a", "success"), job(9, "a", "failed")]
        res = run_with(FakeGitLab(jobs=[job(9, "a", "failed")], history=hist))
        self.assertEqual(res["outcome"], "FAILED")
        self.assertEqual(res["required_job_acceptance"]["required_jobs"][0]["previous_attempts"],
                         [{"id": 1, "status": "success"}])
        hist = [job(1, "a", "failed"), job(9, "a", "success")]
        res = run_with(FakeGitLab(jobs=[job(9, "a", "success")], history=hist))
        self.assertEqual(res["outcome"], "PASSED")
        self.assertEqual(res["required_job_acceptance"]["required_jobs"][0]["previous_attempts"],
                         [{"id": 1, "status": "failed"}])

    def test_history_failure_does_not_change_acceptance(self):
        f = FakeGitLab(errors={"include_retried=true": [(1, b"", b"404")]})
        res = run_with(f)
        self.assertEqual((res["outcome"], res["required_job_acceptance"]["history"]), ("PASSED", "unavailable"))

    def test_status_in_progress_and_inspected(self):
        res = run_with(FakeGitLab(pipeline=pipe("running"), jobs=[job(1, "a", "running")]), command="status")
        self.assertEqual(res["outcome"], "IN_PROGRESS")
        res = run_with(FakeGitLab(), command="status", job=None)
        self.assertEqual((res["outcome"], res["required_job_acceptance"]["status"]), ("INSPECTED", "NOT_EVALUATED"))

    def test_inspection_only_verifies_pipeline_id_and_project(self):
        for pl in (pipe(id=6), pipe(project_id=99)):
            res = run_with(FakeGitLab(pipeline=pl), command="status", job=None, sha=None)
            self.assertEqual(res["outcome"], "CANDIDATE_MISMATCH", pl)
            self.assertFalse(res["overall_pipeline"]["sha_verified"])
        res = run_with(FakeGitLab(), command="status", job=None, sha=None)
        self.assertEqual(res["outcome"], "INSPECTED")
        self.assertFalse(res["overall_pipeline"]["sha_verified"])  # no sha supplied: not claimed

    def test_runner_id_without_job_is_usage_error(self):
        for rid in (["6"], ["a=6"], ["6", "7"]):
            res = run_with(FakeGitLab(), command="status", job=None, sha=None, runner_id=rid)
            self.assertEqual(res["outcome"], "USAGE_ERROR", rid)
        with self.assertRaises(cm.UsageError):
            cm.parse_runner_rules(["6"], [])

    def test_non_finite_remote_numbers_rejected(self):
        for lit in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            raw = ('[{"id":1,"name":"a","status":"success","stage":"test","duration":%s,"runner":{"id":7},'
                   '"pipeline":{"id":5,"project_id":15,"sha":"%s"},"commit":{"id":"%s"}}]' % (lit, SHA, SHA)).encode()
            res = run_with(FakeGitLab(errors={"/jobs": [(0, raw, b"")]}))
            self.assertEqual(res["outcome"], "API_ERROR", lit)
            self.assertIn("malformed_json", res["error"])
            self.assertNotEqual(res["required_job_acceptance"]["status"], "ACCEPTED")
            def strict(c):
                raise ValueError(c)
            json.loads(cm.dumps_strict(res), parse_constant=strict)  # standard JSON only
        raw = b'{"id":5,"status":"success","sha":"%s","project_id":15,"x":NaN}' % SHA.encode()
        res = run_with(FakeGitLab(errors={"/pipelines/5": [(0, raw, b"")]}))
        self.assertEqual(res["outcome"], "API_ERROR")

    def test_dumps_strict_falls_back_to_valid_json(self):
        res = {"outcome": "PASSED", "exit_code": 0, "command": "watch",
               "required_job_acceptance": {"status": "ACCEPTED", "x": float("nan")}}
        text = cm.dumps_strict(res)
        def strict(c):
            raise ValueError(c)
        d = json.loads(text, parse_constant=strict)
        self.assertEqual((d["outcome"], d["exit_code"], res["outcome"]), ("INTERNAL_ERROR", 70, "INTERNAL_ERROR"))
        self.assertNotIn("ACCEPTED", text)

    def test_status_without_job_checks_sha(self):
        res = run_with(FakeGitLab(pipeline=pipe(sha="c" * 40)), command="status", job=None)
        self.assertEqual(res["outcome"], "CANDIDATE_MISMATCH")

    def test_watch_polls_until_pass(self):
        n = {"p": 0}
        f = FakeGitLab()
        orig = f.__call__
        def hook(ep):
            if ep.endswith("/pipelines/5"):
                n["p"] += 1
                f.pipeline = pipe("running" if n["p"] == 1 else "success")
                f.jobs = [job(1, "a", "running" if n["p"] == 1 else "success")]
        f.hook = hook
        res = run_with(f)
        self.assertEqual((res["outcome"], res["polls"]), ("PASSED", 2))

    def test_overall_deadline_including_sleeps(self):
        clk, slept = Clock(), []
        def sleep(s):
            slept.append(s); clk.t += s
        f = FakeGitLab(pipeline=pipe("running"), jobs=[job(1, "a", "running")])
        res = run_with(f, clock=clk, sleep=sleep, poll_interval=60, max_duration=100)
        self.assertEqual(res["outcome"], "TIMED_OUT")
        self.assertLessEqual(sum(slept), 100)

    def test_slow_request_exhausts_budget(self):
        clk = Clock()
        res = run_with(FakeGitLab(hook=lambda ep: setattr(clk, "t", clk.t + 70) if ep.endswith("/pipelines/5") else None),
                       clock=clk, max_duration=60)
        self.assertEqual(res["outcome"], "TIMED_OUT")

    def test_slow_project_lookup_counts(self):
        clk = Clock()
        res = run_with(FakeGitLab(hook=lambda ep: setattr(clk, "t", clk.t + 70) if ep.startswith("projects/g") else None),
                       clock=clk, max_duration=60)
        self.assertEqual(res["outcome"], "TIMED_OUT")

    def test_slow_target_discovery_counts(self):
        clk = Clock()
        real = cm.git_remotes
        cm.git_remotes = lambda cwd=None, timeout=10: (setattr(clk, "t", clk.t + 100), {("h.example", "g/p")})[1]
        try:
            res = run_with(FakeGitLab(), clock=clk, max_duration=60, host=None, project=None)
        finally:
            cm.git_remotes = real
        self.assertEqual(res["outcome"], "TIMED_OUT")

    def test_discovery_subprocess_timeout_is_clipped(self):
        real = subprocess.run
        seen = {}
        def fake(*a, **k):
            seen["t"] = k["timeout"]; raise subprocess.TimeoutExpired("git", k["timeout"])
        subprocess.run = fake
        try:
            with self.assertRaises(cm.TimedOut):
                cm.git_remotes(None, 3.0)
        finally:
            subprocess.run = real
        self.assertEqual(seen["t"], 3.0)

    def test_no_acceptance_after_budget_spent(self):
        clk = Clock()
        f = FakeGitLab(hook=lambda ep: None)
        calls = {"n": 0}
        orig_call = f.__call__
        def wrapped(argv, timeout):
            r = orig_call(argv, timeout)
            if "include_retried=true" in argv[-1]:
                clk.t += 1000  # budget burns during the final (history) request
            return r
        res = run_with(wrapped, clock=clk, max_duration=60)
        self.assertEqual(res["outcome"], "TIMED_OUT")
        self.assertNotEqual(res["required_job_acceptance"]["status"], "ACCEPTED")

    def test_invalid_floating_inputs(self):
        for field in ("poll_interval", "max_duration", "request_timeout"):
            for bad in (float("nan"), float("inf"), float("-inf"), 0, -1):
                res = run_with(FakeGitLab(), **{field: bad})
                self.assertEqual(res["outcome"], "USAGE_ERROR", (field, bad))
        self.assertEqual(run_with(FakeGitLab(), max_duration=1e9)["outcome"], "USAGE_ERROR")
        self.assertEqual(run_with(FakeGitLab(), retries=99)["outcome"], "USAGE_ERROR")

    def test_usage_validations(self):
        for over in (dict(sha="abc"), dict(job=None), dict(job=["a", "a"]), dict(job=[""]), dict(job=["a\x1b[0m"])):
            self.assertEqual(run_with(FakeGitLab(), **over)["outcome"], "USAGE_ERROR", over)

    def test_candidate_mismatch(self):
        res = run_with(FakeGitLab(pipeline=pipe(sha="c" * 40)))
        self.assertEqual((res["outcome"], res["required_job_acceptance"]["status"]), ("CANDIDATE_MISMATCH", "NOT_ACCEPTED"))

    def test_job_pipeline_identity_mismatch_outcome(self):
        res = run_with(FakeGitLab(jobs=[job(1, "a", pipeline_id=6)]))
        self.assertEqual(res["outcome"], "CANDIDATE_MISMATCH")

    def test_stale_details_labelled_after_api_error(self):
        n = {"p": 0}
        f = FakeGitLab(pipeline=pipe("running"), jobs=[job(1, "a", "running")])
        def hook(ep):
            if ep.endswith("/pipelines/5"):
                n["p"] += 1
        f.hook = hook
        f.errors = {}
        def hook2(ep):
            hook(ep)
            if n["p"] >= 2 and ep.endswith("/pipelines/5"):
                f.errors = {"/pipelines/5": [(1, b"", b"404")]}
        f.hook = hook2
        res = run_with(f)
        self.assertEqual(res["outcome"], "API_ERROR")
        self.assertFalse(res["poll_details"]["current"])
        self.assertEqual(res["poll_details"]["last_successful_poll"], 1)
        self.assertIn("STALE", res["poll_details"]["note"])
        self.assertFalse(res["overall_pipeline"]["sha_verified"])

    def test_large_job_list_truncation_is_labelled(self):
        jobs = [job(i, "j%d" % i) for i in range(1, 601)] + [job(1000, "a")]
        res = run_with(FakeGitLab(jobs=jobs))
        self.assertEqual((len(res["all_jobs"]), res["all_jobs_total"], res["all_jobs_truncated"]), (500, 601, True))

    def test_remote_strings_are_sanitized_in_output(self):
        evil = "x\x1b]0;PWNED\x07\x1b[31mred"
        res = run_with(FakeGitLab(jobs=[job(1, "a"), job(2, evil, stage="s\x1b[2J")]))
        text = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_unexpected_exception_is_structured(self):
        f = FakeGitLab()
        def boom(ep):
            if ep.endswith("/pipelines/5"):
                raise RuntimeError("secret-internal-detail")
        f.hook = boom
        res = run_with(f)
        self.assertEqual(res["outcome"], "INTERNAL_ERROR")
        self.assertNotIn("secret-internal-detail", json.dumps(res))

    def test_remote_parsing_and_discovery(self):
        self.assertEqual(cm.parse_remote("git@gitlab.example.com:acme/webapp.git"), ("gitlab.example.com", "acme/webapp"))
        self.assertEqual(cm.parse_remote("https://gitlab.example.com/a/b/c.git"), ("gitlab.example.com", "a/b/c"))
        self.assertEqual(cm.parse_remote("ssh://git@h:2222/a/b.git"), ("h", "a/b"))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(cm.UsageError):
                cm.resolve_target(None, "g/p", cwd=d)
            subprocess.run(["git", "init", "-q", d], check=True)
            subprocess.run(["git", "-C", d, "remote", "add", "origin", "https://git.a/x/y.git"], check=True)
            subprocess.run(["git", "-C", d, "remote", "add", "other", "https://git.b/x/y.git"], check=True)
            with self.assertRaises(cm.UsageError):
                cm.resolve_target(None, None, cwd=d)
            subprocess.run(["git", "-C", d, "remote", "remove", "other"], check=True)
            self.assertEqual(cm.resolve_target(None, None, cwd=d)[:2], ("git.a", "x/y"))


# --------------------------------------------------------------------- locks
class LockTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.dir = os.path.join(self.base, "locks")
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+rwx", self.base]))

    def sentinel(self, name="sentinel"):
        p = os.path.join(self.base, name)
        Path(p).write_bytes(b"SENTINEL-DATA\n")
        return p, Path(p).read_bytes(), os.stat(p).st_mtime_ns

    def lp(self, pid=1, pipeline=5, host="h"):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        return cm.lock_path(self.dir, host, pid, pipeline)

    def test_hard_linked_sentinel_untouched_and_rejected(self):
        s, data, mt = self.sentinel()
        os.link(s, self.lp())
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 1, 5, self.dir):
                pass
        self.assertEqual((Path(s).read_bytes(), os.stat(s).st_mtime_ns), (data, mt))

    def test_symlink_rejected_target_untouched(self):
        s, data, mt = self.sentinel()
        os.symlink(s, self.lp())
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 1, 5, self.dir):
                pass
        self.assertEqual((Path(s).read_bytes(), os.stat(s).st_mtime_ns), (data, mt))

    def test_non_regular_files_rejected(self):
        os.mkfifo(self.lp(1))
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 1, 5, self.dir):
                pass
        os.mkdir(self.lp(2))
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 2, 5, self.dir):
                pass

    def test_unsafe_lock_directories_rejected(self):
        real = os.path.join(self.base, "real"); os.mkdir(real, 0o700)
        link = os.path.join(self.base, "link"); os.symlink(real, link)
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 1, 5, link):
                pass
        wide = os.path.join(self.base, "wide"); os.mkdir(wide); os.chmod(wide, 0o777)
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 1, 5, wide):
                pass
        afile = os.path.join(self.base, "afile"); Path(afile).write_text("x")
        with self.assertRaises(cm.LockError):
            with cm.pipeline_lock("h", 1, 5, afile):
                pass

    def test_ordinary_lock_never_writes_file_and_is_exclusive(self):
        with cm.pipeline_lock("h", 1, 5, self.dir) as a:
            self.assertTrue(a)
            self.assertEqual(os.stat(self.lp()).st_size, 0)
            with cm.pipeline_lock("H", 1, 5, self.dir) as b:  # host is case-insensitive
                self.assertFalse(b)
        with cm.pipeline_lock("h", 1, 5, self.dir) as c:  # released on exit
            self.assertTrue(c)

    def test_independent_projects_and_pipelines(self):
        with cm.pipeline_lock("h", 1, 5, self.dir) as a, cm.pipeline_lock("h", 2, 5, self.dir) as b, \
                cm.pipeline_lock("h", 1, 6, self.dir) as c, cm.pipeline_lock("h2", 1, 5, self.dir) as d:
            self.assertTrue(a and b and c and d)

    def test_lock_error_is_structured_outcome(self):
        os.makedirs(self.dir, mode=0o700)
        s, data, _ = self.sentinel()
        os.link(s, cm.lock_path(self.dir, "h.example", PID_, 5))
        res = run_with(FakeGitLab(), lock_dir=self.dir)
        self.assertEqual((res["outcome"], res["exit_code"]), ("LOCK_ERROR", 11))
        self.assertEqual(Path(s).read_bytes(), data)

    def test_hard_death_releases_lock(self):
        code = ("import sys,time;sys.path.insert(0,%r);import ci_monitor as cm\n"
                "with cm.pipeline_lock('h',1,5,%r) as g:\n print('held' if g else 'no',flush=True);time.sleep(120)\n"
                % (str(SCRIPT.parent), self.dir))
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "held")  # readiness
        with cm.pipeline_lock("h", 1, 5, self.dir) as g:
            self.assertFalse(g)
        p.kill(); p.wait()
        with cm.pipeline_lock("h", 1, 5, self.dir) as g:
            self.assertTrue(g)


# ----------------------------------------------------------- process-level
FAKE_GLAB = r'''#!/usr/bin/env python3
import sys, json, os, time, subprocess
sc = json.load(open(os.environ["FAKE_SCENARIO"]))
ep = sys.argv[-1]
CHILD = ("import os,sys,time\n"
         "p=sys.argv[1]; open(p+'.tmp','w').write(str(os.getpid())); os.rename(p+'.tmp',p)\n"
         "time.sleep(120)\n")
def ready(name):
    p = os.path.join(sc["pid_dir"], name)
    open(p + ".tmp", "w").write(str(os.getpid())); os.rename(p + ".tmp", p)
mode = sc.get("mode", "ok")
hang_on = sc.get("hang_on")
if mode == "secrets":
    sys.stderr.write(sc["secret_text"]); print(sc["secret_text"]); sys.exit(sc.get("code", 1))
if mode in ("hang", "hang_group", "detach") and (hang_on is None or hang_on in ep):
    pd = sc["pid_dir"]
    if mode == "hang_group":
        subprocess.Popen([sys.executable, "-c", CHILD, os.path.join(pd, "child")])
    if mode == "detach":
        subprocess.Popen([sys.executable, "-c", CHILD, os.path.join(pd, "child")], start_new_session=True)
        ready("glab"); sys.exit(0)
    ready("glab"); time.sleep(120)
if "/jobs" in ep:
    print(sc["raw_jobs"] if "raw_jobs" in sc else json.dumps(sc["jobs"] if "page=1" in ep else []))
elif "/pipelines/" in ep:
    print(json.dumps(sc["pipeline"]))
else:
    print(json.dumps({"id": 15, "path_with_namespace": "g/p"}))
'''


class ProcessBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lock = os.path.join(self.d, "locks")
        self.pids = os.path.join(self.d, "pids"); os.mkdir(self.pids)
        self.glab = os.path.join(self.d, "glab")
        Path(self.glab).write_text(FAKE_GLAB); os.chmod(self.glab, 0o755)
        self.procs = []
        self.addCleanup(self._cleanup)
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+rwx", self.d]))

    def _cleanup(self):
        for p in self.procs:
            with_suppress(lambda: os.killpg(p.pid, signal.SIGKILL))
            with_suppress(lambda: p.kill())
            with_suppress(lambda: p.communicate(timeout=5))
        for f in os.listdir(self.pids):
            if not f.endswith(".tmp"):
                with_suppress(lambda f=f: os.kill(int(Path(self.pids, f).read_text()), signal.SIGKILL))

    def scenario(self, **kw):
        sc = {"pid_dir": self.pids, "pipeline": pipe(), "jobs": [job(1, "a")]}
        sc.update(kw)
        path = os.path.join(self.d, "scenario.json")
        Path(path).write_text(json.dumps(sc))
        self.env = dict(os.environ, FAKE_SCENARIO=path)
        return path

    def cli(self, *extra, command="watch", env=None, ignore_sigint=False):
        cmd = [sys.executable, str(SCRIPT), command, "--host", "h.example", "--project", "g/p", "--pipeline", "5",
               "--sha", SHA, "--job", "a", "--glab-bin", self.glab, "--lock-dir", self.lock, *extra]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env or self.env,
                             start_new_session=True,
                             preexec_fn=(lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)) if ignore_sigint else None)
        self.procs.append(p)
        return p

    def wait_for(self, name, timeout=15):
        path = Path(self.pids, name)
        end = time.monotonic() + timeout
        while not path.exists():
            self.assertLess(time.monotonic(), end, "readiness marker %s never appeared" % name)
            time.sleep(0.02)
        return int(path.read_text())

    def assert_dead(self, pid, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail("process %d still alive" % pid)


def with_suppress(fn):
    try:
        fn()
    except Exception:
        pass


class ProcessTests(ProcessBase):
    def test_stdout_pure_json_exit_code_and_output_file(self):
        self.scenario()
        out_file = os.path.join(self.d, "out.json")
        p = self.cli("--output", out_file)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0)
        res = json.loads(out)
        self.assertEqual(res["outcome"], "PASSED")
        self.assertEqual(json.loads(Path(out_file).read_text())["outcome"], "PASSED")
        self.assertNotIn("{", err)  # progress only

    def test_parser_failure_is_json_and_help_is_text(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "status", "--pipeline", "x"], capture_output=True, text=True)
        self.assertEqual((r.returncode, json.loads(r.stdout)["outcome"]), (64, "USAGE_ERROR"))
        r = subprocess.run([sys.executable, str(SCRIPT), "bogus"], capture_output=True, text=True)
        self.assertEqual((r.returncode, json.loads(r.stdout)["outcome"]), (64, "USAGE_ERROR"))
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("exit codes", r.stdout)
        r = subprocess.run([sys.executable, str(SCRIPT), "watch", "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        for bad in ("nan", "inf", "-5"):
            r = subprocess.run([sys.executable, str(SCRIPT), "status", "--host", "h", "--project", "p", "--pipeline", "5",
                                "--max-duration", bad], capture_output=True, text=True)
            self.assertEqual((r.returncode, json.loads(r.stdout)["outcome"]), (64, "USAGE_ERROR"), bad)

    def test_existing_output_not_replaced_without_overwrite(self):
        self.scenario()
        out_file = os.path.join(self.d, "keep.json"); Path(out_file).write_text("keep")
        p = self.cli("--output", out_file)
        out, _ = p.communicate(timeout=30)
        self.assertEqual(json.loads(out)["outcome"], "USAGE_ERROR")
        self.assertEqual(Path(out_file).read_text(), "keep")
        p = self.cli("--output", out_file, "--overwrite")
        out, _ = p.communicate(timeout=30)
        self.assertEqual(json.loads(Path(out_file).read_text())["outcome"], "PASSED")

    @unittest.skipIf(os.geteuid() == 0, "permission-based failure cannot be produced as root")
    def test_output_write_failure_is_machine_readable(self):
        self.scenario()
        outdir = os.path.join(self.d, "ro"); os.mkdir(outdir)
        out_file = os.path.join(outdir, "o.json")
        os.chmod(outdir, 0o555)
        p = self.cli("--output", out_file)
        out, _ = p.communicate(timeout=30)
        res = json.loads(out)
        self.assertEqual((res["outcome"], res["exit_code"], p.returncode), ("OUTPUT_ERROR", 12, 12))
        self.assertEqual(res["underlying_outcome"], "PASSED")
        self.assertFalse(res["output_file"]["written"])
        self.assertFalse(os.path.exists(out_file))

    def test_symlink_output_refused(self):
        self.scenario()
        target = os.path.join(self.d, "t"); Path(target).write_text("keep")
        link = os.path.join(self.d, "l.json"); os.symlink(target, link)
        out, _ = self.cli("--output", link, "--overwrite").communicate(timeout=30)
        self.assertEqual(json.loads(out)["outcome"], "USAGE_ERROR")
        self.assertEqual(Path(target).read_text(), "keep")

    def test_secret_bearing_glab_errors_never_leak(self):
        secrets = ["glpat-AAAAAAAAAAAAAAAAAAAA", "OPAQUE9f8e7d6c5b4a3b2c1d0e", "SIGNED-SIG-123456",
                   "TOKVALUE-98765", "pa55w0rd-in-url", "Zm9vOmJhcg==BASIC", "ENVSECRET=hunter2hunter2"]
        text = ('HTTP 500 {"message":"boom","token":"%s","private_token":"%s"}\n'
                'Authorization: Bearer %s\nPRIVATE-TOKEN: %s\n'
                'GET https://u:%s@gitlab.example.com/api/v4/x?X-Amz-Signature=%s&token=%s\n'
                'Authorization: Basic %s\nenv %s\n\x1b]0;PWNED-TITLE\x07\x1b[31m'
                % (secrets[0], secrets[1], secrets[1], secrets[1], secrets[4], secrets[2], secrets[3], secrets[5], secrets[6]))
        for code in (1, 22):
            out_file = os.path.join(self.d, "s%d.json" % code)
            self.scenario(mode="secrets", secret_text=text, code=code)
            p = self.cli("--retries", "0", "--output", out_file)
            out, err = p.communicate(timeout=30)
            blob = out + err + Path(out_file).read_text()
            for s in secrets + ["PWNED-TITLE", "boom"]:
                self.assertNotIn(s, blob)
            self.assertNotIn("\x1b", blob)
            self.assertNotIn("\x07", blob)
            res = json.loads(out)
            self.assertEqual(res["outcome"], "API_ERROR")
            self.assertIn(res["error"].split(":")[0], cm.ERRORS)

    def test_auth_text_classified_but_not_echoed(self):
        self.scenario(mode="secrets", secret_text='{"message":"401 Unauthorized"} token=glpat-XXXXXXXXXXXXXXXXXXXX')
        out, err = self.cli("--retries", "3").communicate(timeout=30)
        res = json.loads(out)
        self.assertIn("auth", res["error"])
        self.assertNotIn("glpat", out + err)
        self.assertLess(res["elapsed_seconds"], 3)  # permanent error: no 1+2+4s backoff retries

    def test_remote_names_do_not_inject_into_progress(self):
        self.scenario(jobs=[job(1, "a"), job(2, "n\x1b]0;X\x07\x1b[2J")], pipeline=pipe("success"))
        out, err = self.cli().communicate(timeout=30)
        self.assertNotIn("\x1b", out + err)

    def test_duplicate_monitor_refused_and_alias_and_recovery(self):
        self.scenario(mode="hang", hang_on="/pipelines/5")
        first = self.cli("--max-duration", "60")
        self.wait_for("glab")
        # numeric alias of the same project resolves to the same canonical identity
        cmd = [sys.executable, str(SCRIPT), "watch", "--host", "h.example", "--project", "15", "--pipeline", "5",
               "--sha", SHA, "--job", "a", "--glab-bin", self.glab, "--lock-dir", self.lock]
        second = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        self.procs.append(second)
        out, _ = second.communicate(timeout=30)
        self.assertEqual((second.returncode, json.loads(out)["outcome"]), (7, "ALREADY_MONITORING"))
        os.killpg(first.pid, signal.SIGKILL)  # hard death of the monitor and its glab
        first.communicate(timeout=10)
        self.scenario()  # healthy glab now
        third = self.cli()
        out, _ = third.communicate(timeout=30)
        self.assertEqual(json.loads(out)["outcome"], "PASSED")

    def test_different_pipeline_not_blocked(self):
        self.scenario(mode="hang", hang_on="/pipelines/5")
        first = self.cli("--max-duration", "60")
        self.wait_for("glab")
        cmd = [sys.executable, str(SCRIPT), "watch", "--host", "h.example", "--project", "g/p", "--pipeline", "6",
               "--sha", SHA, "--job", "a", "--glab-bin", self.glab, "--lock-dir", self.lock, "--max-duration", "3",
               "--request-timeout", "2"]
        other = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        self.procs.append(other)
        out, _ = other.communicate(timeout=30)
        self.assertNotEqual(json.loads(out)["outcome"], "ALREADY_MONITORING")

    def _signal_test(self, sig, expect_code, ignore_sigint=False):
        self.scenario(mode="hang_group", hang_on="/pipelines/5")
        p = self.cli("--max-duration", "60", ignore_sigint=ignore_sigint)
        glab_pid = self.wait_for("glab")
        child_pid = self.wait_for("child")
        p.send_signal(sig)
        out, _ = p.communicate(timeout=30)
        res = json.loads(out)
        self.assertEqual((p.returncode, res["outcome"]), (expect_code, "INTERRUPTED"))
        self.assert_dead(glab_pid)
        self.assert_dead(child_pid)  # same-process-group descendant is killed too
        with cm.pipeline_lock("h.example", 15, 5, self.lock) as got:
            self.assertTrue(got)

    def test_sigint_cleanup(self):
        self._signal_test(signal.SIGINT, 130)

    def test_sigterm_cleanup(self):
        self._signal_test(signal.SIGTERM, 130)

    def test_sigint_when_launched_with_sigint_ignored(self):
        # background launchers (e.g. `cmd &` in non-interactive shells) leave SIGINT ignored
        self._signal_test(signal.SIGINT, 130, ignore_sigint=True)

    def test_non_finite_numbers_end_to_end(self):
        raw = ('[{"id":1,"name":"a","status":"success","stage":"test","duration":Infinity,"runner":{"id":7},'
               '"pipeline":{"id":5,"project_id":15,"sha":"%s"}}]' % SHA)
        self.scenario(raw_jobs=raw)
        out_file = os.path.join(self.d, "nf.json")
        out, _ = self.cli("--retries", "0", "--output", out_file).communicate(timeout=30)
        def strict(c):
            raise ValueError(c)
        for text in (out, Path(out_file).read_text()):
            self.assertEqual(json.loads(text, parse_constant=strict)["outcome"], "API_ERROR")

    def test_overall_deadline_real_process(self):
        self.scenario(mode="hang", hang_on="/pipelines/5")
        t0 = time.monotonic()
        p = self.cli("--max-duration", "2", "--request-timeout", "30", "--retries", "5")
        out, _ = p.communicate(timeout=30)
        self.assertEqual((json.loads(out)["outcome"], p.returncode), ("TIMED_OUT", 3))
        self.assertLess(time.monotonic() - t0, 2 + 3 * cm.CLEANUP_SECONDS + 5)
        self.assert_dead(self.wait_for("glab"))


class CleanupTests(ProcessBase):
    """Direct GlabClient tests with deterministic readiness (the clock starts after the child is up)."""

    def client(self, timeout):
        base = self

        class C(cm.GlabClient):
            def _spawn(self, argv):
                proc = super()._spawn(argv)
                base.wait_for(base.marker)  # readiness: request timeout only starts once the child is running
                return proc
        os.environ["FAKE_SCENARIO"] = self.env["FAKE_SCENARIO"]
        self.addCleanup(os.environ.pop, "FAKE_SCENARIO", None)
        return C("h", glab_bin=self.glab, request_timeout=timeout, retries=0, deadline=cm.Deadline(60))

    def test_ordinary_timeout_kills_child(self):
        self.scenario(mode="hang"); self.marker = "glab"
        t0 = time.monotonic()
        with self.assertRaises(cm.ApiError) as e:
            self.client(0.5).get("x")
        self.assertEqual(e.exception.kind, "timeout")
        self.assertLess(time.monotonic() - t0, 0.5 + 2 * cm.CLEANUP_SECONDS + 5)
        self.assert_dead(self.wait_for("glab"))

    def test_same_group_descendant_killed_on_timeout(self):
        self.scenario(mode="hang_group"); self.marker = "child"
        with self.assertRaises(cm.ApiError):
            self.client(0.5).get("x")
        self.assert_dead(self.wait_for("glab"))
        self.assert_dead(self.wait_for("child"))

    def test_detached_pipe_holder_cannot_extend_runtime(self):
        self.scenario(mode="detach"); self.marker = "child"
        t0 = time.monotonic()
        with self.assertRaises(cm.ApiError) as e:  # glab exits 0 but a detached child keeps the pipe open
            self.client(0.5).get("x")
        self.assertEqual(e.exception.kind, "timeout")
        self.assertLess(time.monotonic() - t0, 0.5 + 2 * cm.CLEANUP_SECONDS + 5)  # bounded; child (setsid) survives by design
        self.assertTrue(Path(self.pids, "child").exists())  # killed by test cleanup, outside contract


# ----------------------------------------------------------------- installer
def write_exe(path, text):
    Path(path).write_text(text); os.chmod(path, 0o755)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home)
        self.canon = str(ROOT / "skills/gitlab-ci-monitor")
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+rwx", self.home]))

    def inst(self, *a, env=None):
        return subprocess.run([str(ROOT / "install.sh"), *a], env=env or self.env, capture_output=True, text=True)

    def link(self, rel):
        return os.path.join(self.home, rel, "gitlab-ci-monitor")

    def test_dry_run_changes_nothing(self):
        r = self.inst("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertIn("would link", r.stdout)
        self.assertEqual(os.listdir(self.home), [])

    def test_install_idempotent_and_uninstall(self):
        self.assertEqual(self.inst().returncode, 0)
        for rel in (".claude/skills", ".agents/skills"):
            self.assertEqual(os.readlink(self.link(rel)), self.canon)
        r = self.inst()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("already installed"), 2)
        self.assertEqual(self.inst("--uninstall", "--dry-run").returncode, 0)
        self.assertTrue(os.path.islink(self.link(".claude/skills")))
        self.assertEqual(self.inst("--uninstall").returncode, 0)
        self.assertFalse(os.path.lexists(self.link(".claude/skills")))
        self.assertEqual(self.inst("--uninstall").returncode, 0)  # idempotent

    def test_collision_refused_nothing_changed_foreign_preserved(self):
        t = Path(self.link(".agents/skills")); t.mkdir(parents=True)
        (t / "mine.txt").write_text("x")
        r = self.inst()
        self.assertEqual(r.returncode, 1)
        self.assertIn("CONFLICT", r.stderr)
        self.assertFalse(os.path.lexists(self.link(".claude/skills")))
        self.inst("--uninstall")
        self.assertTrue((t / "mine.txt").exists())
        self.assertFalse(os.path.islink(str(t)) or os.path.lexists(str(t / "gitlab-ci-monitor")))  # no nested link

    def test_foreign_and_broken_symlinks_conflict_and_survive_uninstall(self):
        for target in ("/nonexistent/elsewhere", self.home):
            t = Path(self.link(".claude/skills"))
            t.parent.mkdir(parents=True, exist_ok=True)
            if t.is_symlink():
                t.unlink()
            t.symlink_to(target)
            self.assertEqual(self.inst("--agents", "claude").returncode, 1)
            self.assertEqual(self.inst("--uninstall", "--agents", "claude").returncode, 0)
            self.assertTrue(t.is_symlink() and os.readlink(t) == target)

    def test_invalid_parent_path(self):
        Path(self.home, ".claude").write_text("i am a file")
        r = self.inst()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.lexists(self.link(".agents/skills")))  # preflight: no partial install
        self.assertEqual(Path(self.home, ".claude").read_text(), "i am a file")

    def test_broken_parent_symlink(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        os.symlink("/nonexistent/dir", os.path.join(self.home, ".claude/skills"))
        self.assertEqual(self.inst("--agents", "claude").returncode, 1)

    def test_valid_parent_symlink_is_followed_once(self):
        real = os.path.join(self.home, "realskills"); os.mkdir(real)
        os.makedirs(os.path.join(self.home, ".claude"))
        os.symlink(real, os.path.join(self.home, ".claude/skills"))
        self.assertEqual(self.inst("--agents", "claude").returncode, 0)
        self.assertEqual(os.readlink(os.path.join(real, "gitlab-ci-monitor")), self.canon)
        self.assertEqual(os.listdir(real), ["gitlab-ci-monitor"])

    @unittest.skipIf(os.geteuid() == 0, "permission-based failure cannot be produced as root")
    def test_unwritable_parent_preflight(self):
        os.makedirs(os.path.join(self.home, ".claude/skills")); os.chmod(os.path.join(self.home, ".claude/skills"), 0o555)
        r = self.inst()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.lexists(self.link(".agents/skills")))

    def _fake_tool(self, name, body):
        b = os.path.join(self.home, "fakebin"); os.makedirs(b, exist_ok=True)
        write_exe(os.path.join(b, name), body)
        return dict(self.env, PATH=b + ":" + os.environ["PATH"])

    def test_partial_failure_is_nonzero_and_rolled_back(self):
        env = self._fake_tool("ln", '#!/bin/sh\nc="%s/count"\nn=$(cat "$c" 2>/dev/null || echo 0)\necho $((n+1)) > "$c"\n'
                                    '[ "$n" -ge 1 ] && exit 1\nexec /bin/ln "$@"\n' % self.home)
        r = self.inst(env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED to install", r.stderr)
        self.assertFalse(os.path.lexists(self.link(".claude/skills")))  # first link rolled back
        self.assertFalse(os.path.lexists(self.link(".agents/skills")))

    def test_failed_removal_is_nonzero(self):
        self.assertEqual(self.inst().returncode, 0)
        env = self._fake_tool("rm", "#!/bin/sh\nexit 1\n")
        r = self.inst("--uninstall", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED to remove", r.stderr)

    def test_failed_mkdir_is_nonzero(self):
        env = self._fake_tool("mkdir", "#!/bin/sh\nexit 1\n")
        r = self.inst(env=env)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("installed:", r.stdout)

    def test_opencode_only_creates_single_shared_link_and_uninstall_keeps_it(self):
        r = self.inst("--agents", "opencode")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(os.readlink(self.link(".agents/skills")), self.canon)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config/opencode")))
        self.assertFalse(os.path.lexists(self.link(".claude/skills")))
        r = self.inst("--uninstall", "--agents", "opencode")
        self.assertEqual(r.returncode, 0)
        self.assertIn("shared", r.stdout)
        self.assertTrue(os.path.islink(self.link(".agents/skills")))
        self.assertEqual(self.inst("--uninstall", "--agents", "claude,codex").returncode, 0)
        self.assertFalse(os.path.lexists(self.link(".agents/skills")))

    def test_opencode_satisfied_by_existing_claude_link(self):
        self.assertEqual(self.inst("--agents", "claude").returncode, 0)
        self.assertEqual(self.inst("--agents", "opencode").returncode, 0)
        self.assertFalse(os.path.lexists(self.link(".agents/skills")))

    def test_foreign_opencode_compatible_definition_is_not_mistaken_for_ours(self):
        t = Path(self.link(".agents/skills")); t.mkdir(parents=True); (t / "SKILL.md").write_text("foreign")
        for agents in ("opencode", "claude,opencode"):
            r = self.inst("--agents", agents)
            self.assertEqual(r.returncode, 1, agents)
            self.assertIn("foreign", r.stderr)
        self.assertFalse(os.path.lexists(self.link(".claude/skills")))
        self.assertEqual((t / "SKILL.md").read_text(), "foreign")

    def test_foreign_claude_definition_with_opencode_only(self):
        t = Path(self.link(".claude/skills")); t.mkdir(parents=True); (t / "SKILL.md").write_text("foreign")
        r = self.inst("--agents", "opencode")
        self.assertEqual(r.returncode, 0)
        self.assertIn("foreign", r.stderr)  # warned: ours overrides via precedence
        self.assertEqual(os.readlink(self.link(".agents/skills")), self.canon)
        self.assertEqual((t / "SKILL.md").read_text(), "foreign")

    def test_installed_links_run_cli_from_unrelated_directory(self):
        self.assertEqual(self.inst().returncode, 0)
        with tempfile.TemporaryDirectory() as other:
            for rel in (".claude/skills", ".agents/skills"):
                r = subprocess.run([sys.executable, os.path.join(self.link(rel), "scripts/ci_monitor.py"), "watch", "--help"],
                                   cwd=other, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0)
                self.assertTrue(os.path.isfile(os.path.join(self.link(rel), "references/gitlab-workflow.md")))


if __name__ == "__main__":
    unittest.main()
