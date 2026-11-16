#!/usr/bin/env python3
"""Read-only GitLab CI acceptance monitor (uses the authenticated `glab` CLI).

Commands: `status` (one-shot) and `watch` (bounded polling). Progress goes to
stderr; stdout carries exactly one JSON document. See --help for exit codes.

Trust boundary: remote (GitLab/glab) text never reaches the output verbatim.
Errors are fixed allowlisted strings; remote strings that are echoed (stage,
status, job names in the job list) are control-character stripped and bounded.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
import urllib.parse

SCHEMA_VERSION = 2

EXIT_CODES = {
    "PASSED": 0, "FAILED": 1, "ACTION_REQUIRED": 2, "TIMED_OUT": 3,
    "API_ERROR": 4, "CANDIDATE_MISMATCH": 5, "RUNNER_MISMATCH": 6,
    "ALREADY_MONITORING": 7, "IN_PROGRESS": 8, "AMBIGUOUS_JOB": 9,
    "INSPECTED": 10, "LOCK_ERROR": 11, "OUTPUT_ERROR": 12,
    "USAGE_ERROR": 64, "INTERNAL_ERROR": 70, "INTERRUPTED": 130,
}

PENDING_STATUSES = {"created", "pending", "preparing", "running", "scheduled",
                    "waiting_for_resource", "waiting_for_callback"}
FAILED_STATUSES = {"failed", "canceled", "canceling"}
PIPELINE_TERMINAL = {"success", "failed", "canceled", "skipped", "manual"}

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUNNER_RE = re.compile(r"^[1-9][0-9]{0,17}$")
MAX_PAGES = 50
PER_PAGE = 100
MAX_ALL_JOBS = 500
MAX_RETRIES = 10
MAX_DURATION_CAP = 86400.0
# Grace given to reap a killed glab and drain its pipes. It is NOT part of the requested
# monitoring duration: worst-case overshoot per killed request is CLEANUP_SECONDS (+ tiny wait).
CLEANUP_SECONDS = 1.0

# Fixed, allowlisted error descriptions. Remote/glab output is never copied into these.
ERRORS = {
    "auth": "GitLab authentication failed (HTTP 401)",
    "permission": "GitLab denied access (HTTP 403)",
    "not_found": "GitLab resource not found (HTTP 404)",
    "rate_limited": "GitLab rate limit (HTTP 429)",
    "server_error": "GitLab server error (HTTP 5xx)",
    "cli_error": "glab request failed",
    "timeout": "glab request timed out",
    "empty_response": "glab returned an empty response",
    "malformed_json": "glab returned malformed JSON",
    "schema": "unexpected GitLab response schema",
    "glab_missing": "glab executable not found or not executable",
    "too_many_pages": "job list exceeded the page limit",
    "inconsistent_pagination": "inconsistent job pagination (conflicting or non-advancing pages)",
    "project_mismatch": "GitLab returned a different project than requested (moved/redirected path?); pass the current path or numeric ID",
}
PERMANENT = {"auth", "permission", "not_found", "glab_missing", "schema",
             "too_many_pages", "inconsistent_pagination", "project_mismatch"}

NOT_PROVEN = [
    "unit test counts", "native acceptance results", "artifact integrity",
    "status of jobs that were not listed as required",
    "merge, deployment or product-level readiness",
]


def safe_text(value, limit=80):
    """Strip control/non-printable characters and bound length (terminal-injection safe)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value)
    value = "".join(ch if (ch.isprintable() and ch not in "\x7f") else " " for ch in value)
    value = re.sub(r" {2,}", " ", value).strip()
    return value[:limit] + ("..." if len(value) > limit else "")


# ------------------------------------------------------------------ deadline
class TimedOut(Exception):
    pass


class UsageError(Exception):
    pass


class LockError(Exception):
    pass


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.start = clock()
        self.seconds = seconds

    def elapsed(self):
        return self.clock() - self.start

    def remaining(self):
        return self.seconds - self.elapsed()

    def check(self):
        if self.remaining() <= 0:
            raise TimedOut()


class ApiError(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind
        self.permanent = kind in PERMANENT
        self.message = ERRORS[kind]


def _classify_failure(blob):
    """Internal classification of glab failure text. The text itself is discarded."""
    head = blob[:2000]
    if re.search(r"\b401\b", head) or re.search(r"(?i)unauthori[sz]ed|not logged in|authentication", head):
        return "auth"
    if re.search(r"\b403\b", head):
        return "permission"
    if re.search(r"\b404\b", head):
        return "not_found"
    if re.search(r"\b429\b", head):
        return "rate_limited"
    if re.search(r"\b5\d\d\b", head):
        return "server_error"
    return "cli_error"


# ------------------------------------------------------------ schema checks
def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _schema(cond):
    if not cond:
        raise ApiError("schema")


def validate_project(d):
    _schema(isinstance(d, dict) and _is_int(d.get("id")) and d["id"] > 0)
    _schema(isinstance(d.get("path_with_namespace"), str))
    return d


def validate_pipeline(d):
    _schema(isinstance(d, dict) and _is_int(d.get("id")))
    _schema(isinstance(d.get("status"), str) and isinstance(d.get("sha"), str))
    _schema(d.get("project_id") is None or _is_int(d.get("project_id")))
    return d


def validate_job(j):
    _schema(isinstance(j, dict) and _is_int(j.get("id")))
    _schema(isinstance(j.get("name"), str) and isinstance(j.get("status"), str))
    _schema(j.get("stage") is None or isinstance(j.get("stage"), str))
    r = j.get("runner")
    _schema(r is None or (isinstance(r, dict) and (r.get("id") is None or _is_int(r.get("id")))))
    p = j.get("pipeline")
    _schema(p is None or (isinstance(p, dict) and (p.get("id") is None or _is_int(p.get("id")))
                          and (p.get("sha") is None or isinstance(p.get("sha"), str))
                          and (p.get("project_id") is None or _is_int(p.get("project_id")))))
    c = j.get("commit")
    _schema(c is None or (isinstance(c, dict) and (c.get("id") is None or isinstance(c.get("id"), str))))
    d = j.get("duration")
    _schema(d is None or (isinstance(d, (int, float)) and not isinstance(d, bool)))
    _schema(j.get("retried") is None or isinstance(j.get("retried"), bool))
    return j


def _reject_constant(name):
    raise ValueError("non-standard JSON constant")


def _strict_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def normalize_project_path(path):
    """Documented normalization: case-insensitive, surrounding slashes and a trailing .git ignored."""
    return str(path).strip().strip("/").removesuffix(".git").strip("/").lower()


# -------------------------------------------------------------------- client
class GlabClient:
    """Read-only GitLab access via `glab api -X GET`. Never builds mutating calls."""

    def __init__(self, host, glab_bin="glab", request_timeout=30.0, retries=3,
                 deadline=None, sleep=time.sleep, log=None):
        self.host = host
        self.glab_bin = glab_bin
        self.request_timeout = request_timeout
        self.retries = retries
        self.deadline = deadline or Deadline(3600)
        self.sleep = sleep
        self.log = log or (lambda m: None)

    def _argv(self, endpoint):
        return [self.glab_bin, "api", "--hostname", self.host, "-X", "GET", endpoint]

    def _spawn(self, argv):
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True,
                                # the parent briefly blocks SIGINT/SIGTERM around spawn; the child must not inherit that
                                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, set()))

    def _exec(self, argv, timeout):
        """Run argv (no shell) under a hard timeout. On timeout/interrupt the whole process
        group is SIGKILLed, pipes are drained for at most CLEANUP_SECONDS and then closed, so a
        descendant that escaped the group (setsid) and holds a pipe cannot block us."""
        proc = None
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            proc = self._spawn(argv)  # signals are deferred until proc is tracked
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except BaseException:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            if proc is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    proc.communicate(timeout=CLEANUP_SECONDS)
                for stream in (proc.stdout, proc.stderr):
                    with contextlib.suppress(Exception):
                        stream.close()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=CLEANUP_SECONDS)
            raise

    def _once(self, endpoint):
        self.deadline.check()
        timeout = min(self.request_timeout, self.deadline.remaining())
        try:
            rc, out, err = self._exec(self._argv(endpoint), timeout)
        except subprocess.TimeoutExpired:
            self.deadline.check()
            raise ApiError("timeout")
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            raise ApiError("glab_missing")
        out_s = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        err_s = err.decode("utf-8", "replace") if isinstance(err, bytes) else (err or "")
        if rc != 0:  # exit status first; output is classified internally, then discarded
            raise ApiError(_classify_failure(out_s[:2000] + " " + err_s[:2000]))
        if not out_s.strip():
            raise ApiError("empty_response")
        try:
            return json.loads(out_s, parse_constant=_reject_constant, parse_float=_strict_float)
        except ValueError:  # includes NaN/Infinity/-Infinity and overflowing literals such as 1e999
            raise ApiError("malformed_json")

    def get(self, endpoint):
        attempt = 0
        while True:
            try:
                return self._once(endpoint)
            except ApiError as e:
                if e.permanent or attempt >= self.retries:
                    raise
                delay = min(2 ** attempt, 30)
                attempt += 1
                self.log("transient error (%s); retry %d/%d in %ss" % (e.kind, attempt, self.retries, delay))
                if delay >= self.deadline.remaining():
                    raise TimedOut()
                self.sleep(delay)

    def project(self, ref):
        d = validate_project(self.get("projects/%s" % project_ref(ref)))
        if str(ref).isdigit():
            if d["id"] != int(ref):
                raise ApiError("schema")
        elif normalize_project_path(d["path_with_namespace"]) != normalize_project_path(ref):
            raise ApiError("project_mismatch")
        return d["id"], d["path_with_namespace"]

    def pipeline(self, project_id, pipeline_id):
        return validate_pipeline(self.get("projects/%d/pipelines/%d" % (project_id, pipeline_id)))

    def jobs(self, project_id, pipeline_id, include_retried=False):
        """Paginated, bounded, de-duplicated by job id. include_retried=false is GitLab's
        authoritative current-attempt set; include_retried=true is history/reporting only."""
        by_id, order = {}, []
        for page in range(1, MAX_PAGES + 1):
            data = self.get("projects/%d/pipelines/%d/jobs?per_page=%d&page=%d&include_retried=%s"
                            % (project_id, pipeline_id, PER_PAGE, page, "true" if include_retried else "false"))
            if not isinstance(data, list):
                raise ApiError("schema")
            new = 0
            for j in data:
                validate_job(j)
                if j["id"] in by_id:
                    if by_id[j["id"]] != j:
                        raise ApiError("inconsistent_pagination")
                    continue
                by_id[j["id"]] = j
                order.append(j["id"])
                new += 1
            if len(data) < PER_PAGE:
                return [by_id[i] for i in order]
            if new == 0:
                raise ApiError("inconsistent_pagination")
        raise ApiError("too_many_pages")


def project_ref(project):
    return str(project) if str(project).isdigit() else urllib.parse.quote(str(project), safe="")


# ---------------------------------------------------------------- evaluation
def _runner_id(job):
    r = job.get("runner")
    return r.get("id") if isinstance(r, dict) else None


def _identity_problem(job, pipeline_id, sha, project_id):
    """Fail closed. job.pipeline.id and job.pipeline.sha must be present and exact; project_id
    and commit.id are verified whenever GitLab provides them."""
    p = job.get("pipeline")
    if not isinstance(p, dict) or p.get("id") is None or p.get("sha") is None:
        return "job pipeline identity evidence missing"
    if p["id"] != pipeline_id:
        return "job belongs to a different pipeline"
    if p["sha"].lower() != sha:
        return "job pipeline sha does not equal expected candidate sha"
    if p.get("project_id") is not None and project_id is not None and p["project_id"] != project_id:
        return "job belongs to a different project"
    c = job.get("commit")
    if isinstance(c, dict) and c.get("id") is not None and c["id"].lower() != sha:
        return "job commit does not equal expected candidate sha"
    return None


def evaluate(pipeline, jobs, required, expected_sha, runner_rules, pipeline_id=None, project_id=None):
    """Pure. `jobs` must be the authoritative current-job set (include_retried=false).
    Returns (outcome|None, details). None means keep waiting."""
    details = {"pipeline_sha_verified": False, "jobs": []}
    if pipeline_id is not None and pipeline.get("id") != pipeline_id:
        details["reason"] = "pipeline id in response does not equal requested pipeline"
        return "CANDIDATE_MISMATCH", details
    if project_id is not None and pipeline.get("project_id") is not None and pipeline["project_id"] != project_id:
        details["reason"] = "pipeline belongs to a different project"
        return "CANDIDATE_MISMATCH", details
    psha = str(pipeline.get("sha", "")).lower()
    if psha != expected_sha:
        details["reason"] = "pipeline sha does not equal expected candidate sha"
        details["actual_sha"] = safe_text(psha, 64)
        return "CANDIDATE_MISMATCH", details
    details["pipeline_sha_verified"] = True
    pstatus = pipeline.get("status")
    terminal = pstatus in PIPELINE_TERMINAL

    for name in required:
        matches = [j for j in jobs if j.get("name") == name]
        want = runner_rules.get(name)
        entry = {"requested_name": name, "runner_constraint": want, "previous_attempts": None}
        if len(matches) > 1:
            entry.update(verdict="AMBIGUOUS", reason="several distinct current jobs share this name",
                         candidate_job_ids=sorted(j["id"] for j in matches),
                         candidate_stages=[safe_text(j.get("stage"), 40) for j in matches])
        elif not matches:
            entry.update(verdict="MISSING", reason="required job not present in current job set")
        else:
            job = matches[0]
            status = job["status"]
            runner = _runner_id(job)
            entry.update(name=name, id=job["id"], stage=safe_text(job.get("stage"), 40),
                         status=safe_text(status, 40), runner_id=runner, duration=job.get("duration"),
                         allow_failure=bool(job.get("allow_failure")))
            ident = _identity_problem(job, pipeline["id"], expected_sha, project_id)
            if job.get("retried") is True:
                entry.update(verdict="AMBIGUOUS", reason="current-set job is flagged retried (contradictory)")
            elif ident:
                entry.update(verdict="IDENTITY", reason=ident)
            elif status in FAILED_STATUSES:
                entry.update(verdict="FAILED", reason="required job %s" % status)
            elif status == "skipped":
                entry.update(verdict="FAILED", reason="required job was skipped (not accepted)")
            elif status == "manual":
                entry.update(verdict="ACTION", reason="required manual job must be played by the owner")
            elif want is not None and runner is not None and runner != want:
                entry.update(verdict="RUNNER", reason="job ran on runner %s; expected %s" % (runner, want))
            elif status == "success":
                if want is not None and runner is None:
                    entry.update(verdict="RUNNER", reason="runner identity unavailable; cannot verify")
                else:
                    entry.update(verdict="OK")
            elif status in PENDING_STATUSES:
                entry.update(verdict="PENDING")
            else:
                entry.update(verdict="FAILED", reason="unrecognized job status")
        details["jobs"].append(entry)

    if terminal:  # nothing more will be created or run by itself
        for e in details["jobs"]:
            if e["verdict"] in ("MISSING", "PENDING"):
                e["verdict"] = "FAILED"
                e["reason"] = e.get("reason") or "job unfinished although pipeline is %s" % safe_text(pstatus, 30)
    vs = [e["verdict"] for e in details["jobs"]]
    for v, outcome in (("IDENTITY", "CANDIDATE_MISMATCH"), ("FAILED", "FAILED"), ("RUNNER", "RUNNER_MISMATCH"),
                       ("AMBIGUOUS", "AMBIGUOUS_JOB"), ("ACTION", "ACTION_REQUIRED")):
        if v in vs:
            return outcome, details
    if vs and all(v == "OK" for v in vs):
        return "PASSED", details
    return None, details


# ------------------------------------------------------------------- locking
def lock_dir_default():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ci-monitor")


def lock_path(lock_dir, host, project_id, pipeline_id):
    key = "%s|%s|%d" % (host.lower(), project_id, pipeline_id)
    return os.path.join(lock_dir, hashlib.sha256(key.encode()).hexdigest()[:32] + ".lock")


def _prepare_lock_dir(lock_dir):
    uid = os.geteuid()
    try:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(lock_dir)), exist_ok=True)
            os.mkdir(lock_dir, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(lock_dir)
    except OSError:
        raise LockError("lock directory cannot be created or inspected")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise LockError("lock directory is a symlink or not a directory")
    if st.st_uid != uid or (st.st_mode & 0o022):
        raise LockError("lock directory is not owned by this user or is group/world writable")


@contextlib.contextmanager
def pipeline_lock(host, project_id, pipeline_id, lock_dir=None):
    """Advisory flock keyed by host|numeric project id|pipeline. Yields True if acquired,
    False if another process holds it. The lock file is never truncated or written; symlinks,
    hard links and non-regular files are rejected without modification."""
    lock_dir = lock_dir or lock_dir_default()
    _prepare_lock_dir(lock_dir)
    path = lock_path(lock_dir, host, project_id, pipeline_id)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError:
        raise LockError("lock file cannot be opened safely (symlink, directory or permission problem)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            raise LockError("lock file is not a private regular file (hard link, wrong owner or special file)")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # releases the flock; process death does the same


# ------------------------------------------------------------ git discovery
_REMOTE_RES = [
    re.compile(r"^(?:ssh|https?|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$"),
    re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>[^/].*)$"),
]


def parse_remote(url):
    for rx in _REMOTE_RES:
        m = rx.match(url.strip())
        if m:
            return m.group("host").lower(), m.group("path").removesuffix(".git").strip("/")
    return None


def git_remotes(cwd=None, timeout=10):
    try:
        out = subprocess.run(["git", "remote", "-v"], cwd=cwd, capture_output=True, text=True,
                             timeout=max(min(timeout, 10), 0.1)).stdout
    except subprocess.TimeoutExpired:
        raise TimedOut()
    except (OSError, subprocess.SubprocessError):
        return set()
    found = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            p = parse_remote(parts[1])
            if p:
                found.add(p)
    return found


def resolve_target(host, project, cwd=None, deadline=None):
    """Explicit values win. Missing values come from git remotes only if unambiguous."""
    if host and project:
        return host.lower(), project, "explicit"
    if deadline:
        deadline.check()
    remotes = git_remotes(cwd, deadline.remaining() if deadline else 10)
    if not host:
        hosts = {h for h, _ in remotes}
        if len(hosts) != 1:
            raise UsageError("host not given and cannot be derived unambiguously from git remotes; pass --host")
        host = hosts.pop()
    if not project:
        paths = {p for h, p in remotes if h == host.lower()}
        if len(paths) != 1:
            raise UsageError("project not given and cannot be derived unambiguously from git remotes; pass --project")
        project = paths.pop()
    return host.lower(), project, "git-remote"


# ----------------------------------------------------------------- validation
def parse_runner_rules(items, required):
    """Returns {job: runner_id} covering exactly the jobs a rule applies to. `N` (global) expands
    to every required job; `JOB=N` must name a required job. Mixing forms or conflicting values is
    rejected, so a rule can never silently be ineffective."""
    if not items:
        return {}
    if not required:
        raise UsageError("--runner-id requires at least one --job")
    glob, per = set(), {}
    for it in items:
        name, sep, val = it.rpartition("=")
        if not RUNNER_RE.match(val):
            raise UsageError("--runner-id must be a positive integer N or JOB=N")
        rid = int(val)
        if sep:
            if name not in required:
                raise UsageError("--runner-id names a job that is not in --job: %s" % safe_text(name, 60))
            if per.setdefault(name, rid) != rid:
                raise UsageError("contradictory --runner-id values for job %s" % safe_text(name, 60))
        else:
            glob.add(rid)
    if glob and per:
        raise UsageError("do not mix global --runner-id N with per-job JOB=N rules")
    if len(glob) > 1:
        raise UsageError("contradictory global --runner-id values")
    if glob:
        return {n: next(iter(glob)) for n in required}
    return per


def _positive_finite(name, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise UsageError("%s must be a finite positive number" % name)


def validate_args(args):
    for n in ("poll_interval", "max_duration", "request_timeout"):
        _positive_finite("--" + n.replace("_", "-"), getattr(args, n))
    if args.max_duration > MAX_DURATION_CAP:
        raise UsageError("--max-duration may not exceed %d seconds" % MAX_DURATION_CAP)
    if not _is_int(args.retries) or not 0 <= args.retries <= MAX_RETRIES:
        raise UsageError("--retries must be an integer between 0 and %d" % MAX_RETRIES)
    jobs = args.job or []
    for j in jobs:
        if not j or len(j) > 200 or safe_text(j, 300) != j:
            raise UsageError("--job names must be non-empty, printable and at most 200 characters")
    if len(set(jobs)) != len(jobs):
        raise UsageError("duplicate --job name")
    if args.sha is not None and not SHA_RE.match(args.sha.lower()):
        raise UsageError("--sha must be a full 40-hex SHA")
    if jobs and not args.sha:
        raise UsageError("--job requires --sha with the full 40-hex candidate SHA")
    if args.command == "watch" and not jobs:
        raise UsageError("watch requires at least one --job")
    if args.pipeline <= 0:
        raise UsageError("--pipeline must be positive")
    return parse_runner_rules(args.runner_id, jobs)


# ------------------------------------------------------------------- output
def check_output_path(path, overwrite):
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise UsageError("--output directory does not exist")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise UsageError("--output cannot be inspected")
    if stat.S_ISLNK(st.st_mode):
        raise UsageError("--output refuses symlink targets")
    if not stat.S_ISREG(st.st_mode):
        raise UsageError("--output is not a regular file")
    if not overwrite:
        raise UsageError("--output exists; pass --overwrite to replace it")


def _create_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def write_output(path, text, overwrite):
    check_output_path(path, overwrite)
    if os.path.lexists(path):
        tmp = "%s.tmp.%d" % (path, os.getpid())
        _create_new(tmp, text)
        try:
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    else:
        _create_new(path, text)


def summarize_all(jobs):
    rows = [{"id": j["id"], "name": safe_text(j["name"], 100), "stage": safe_text(j.get("stage"), 40),
             "status": safe_text(j["status"], 40), "runner_id": _runner_id(j),
             "allow_failure": bool(j.get("allow_failure"))}
            for j in sorted(jobs, key=lambda j: j["id"])]
    return rows[:MAX_ALL_JOBS], len(rows)


def build_result(outcome, ctx, st, error=None, elapsed=0.0):
    pipeline, details = st.get("pipeline"), st.get("details") or {}
    required = ctx.get("required") or []
    acc_pending = ("IN_PROGRESS", "INSPECTED", "TIMED_OUT", "INTERRUPTED", "API_ERROR",
                   "ALREADY_MONITORING", "LOCK_ERROR", "INTERNAL_ERROR")
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "ci_monitor",
        "command": ctx.get("command"),
        "outcome": outcome,
        "exit_code": EXIT_CODES[outcome],
        "host": ctx.get("host"),
        "project": {"requested": ctx.get("project_input"), "id": ctx.get("project_id"),
                    "path": ctx.get("project_path"), "source": ctx.get("target_source")},
        "pipeline_id": ctx.get("pipeline_id"),
        "expected_sha": ctx.get("sha"),
        "runner_constraints": ctx.get("runner_rules") or {},
        "overall_pipeline": {
            "status": safe_text(pipeline.get("status"), 40) if pipeline else None,
            "sha": safe_text(pipeline.get("sha"), 64) if pipeline else None,
            "sha_verified": bool(details.get("pipeline_sha_verified", False)) and st.get("current", False),
            "note": "Overall GitLab status only; it does not decide required-job acceptance.",
        },
        "required_job_acceptance": {
            "status": ("NOT_EVALUATED" if not required else
                       "ACCEPTED" if outcome == "PASSED" else
                       "PENDING" if outcome in acc_pending else "NOT_ACCEPTED"),
            "required_jobs": details.get("jobs", []),
            "reason": details.get("reason"),
            "history": st.get("history", "not_requested"),
        },
        "poll_details": {
            "current": bool(st.get("current", False)),
            "last_successful_poll": st.get("last_ok_poll"),
            "note": ("details are from the latest poll" if st.get("current") else
                     "no poll completed; no pipeline details" if not st.get("last_ok_poll") else
                     "details are STALE: from the last successful poll, not the final state"),
        },
        "all_jobs": details.get("all_jobs"),
        "all_jobs_total": details.get("all_jobs_total"),
        "all_jobs_truncated": bool(details.get("all_jobs_total") and details["all_jobs_total"] > MAX_ALL_JOBS),
        "missing_evidence": NOT_PROVEN,
        "elapsed_seconds": round(elapsed, 1),
        "polls": st.get("polls", 0),
        "error": error,
    }


# ----------------------------------------------------------------- commands
def run(args, log=None, sleep=time.sleep, clock=time.monotonic):
    deadline = Deadline(float("inf"), clock)  # monotonic clock starts before target discovery
    base_log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    slog = lambda m: base_log(safe_text(m, 300))  # noqa: E731
    ctx = {"command": args.command, "required": list(args.job or []), "sha": args.sha,
           "pipeline_id": args.pipeline, "project_input": safe_text(args.project, 200)}
    st = {"pipeline": None, "details": None, "polls": 0, "current": False, "last_ok_poll": None,
          "history": "not_requested"}

    def finish(outcome, error=None):
        return build_result(outcome, ctx, st, error, deadline.elapsed())

    try:
        rules = validate_args(args)
        if args.sha:
            args.sha = ctx["sha"] = args.sha.lower()
        ctx["runner_rules"] = rules
        check_output_path(args.output, args.overwrite)
        deadline.seconds = float(args.max_duration)
        host, project, source = resolve_target(args.host, args.project, deadline=deadline)
        ctx.update(host=host, target_source=source)
        client = GlabClient(host, args.glab_bin, float(args.request_timeout), args.retries, deadline, sleep, slog)
        project_id, project_path = client.project(project)
        ctx.update(project_id=project_id, project_path=safe_text(project_path, 200))

        lock_cm = (pipeline_lock(host, project_id, args.pipeline, args.lock_dir) if args.command == "watch"
                   else contextlib.nullcontext(True))
        with lock_cm as acquired:
            if not acquired:
                return finish("ALREADY_MONITORING", "another watch holds the lock for this pipeline")
            seen = {}
            while True:
                st["polls"] += 1
                st["current"] = False
                pipeline = client.pipeline(project_id, args.pipeline)
                jobs = client.jobs(project_id, args.pipeline, include_retried=False)
                st["pipeline"] = pipeline
                if args.job:
                    outcome, details = evaluate(pipeline, jobs, args.job, args.sha, rules, args.pipeline, project_id)
                else:
                    outcome, details = None, {"pipeline_sha_verified": False, "jobs": []}
                    id_ok = pipeline["id"] == args.pipeline and pipeline.get("project_id") in (None, project_id)
                    sha_ok = (not args.sha) or str(pipeline.get("sha", "")).lower() == args.sha
                    details["pipeline_sha_verified"] = bool(args.sha) and sha_ok and id_ok
                    if not (id_ok and sha_ok):
                        details["reason"] = "pipeline id, project or sha does not match the request"
                        outcome = "CANDIDATE_MISMATCH"
                details["all_jobs"], details["all_jobs_total"] = summarize_all(jobs)
                st["details"], st["current"], st["last_ok_poll"] = details, True, st["polls"]
                for e in details["jobs"]:
                    key, cur = e["requested_name"], e.get("status") or e["verdict"]
                    if seen.get(key) != cur:
                        slog("[%6.0fs] job %s: %s -> %s" % (deadline.elapsed(), key, seen.get(key), cur))
                        seen[key] = cur
                if seen.get("__pipeline") != pipeline["status"]:
                    slog("[%6.0fs] pipeline %s: %s" % (deadline.elapsed(), args.pipeline, pipeline["status"]))
                    seen["__pipeline"] = pipeline["status"]
                if outcome is None and args.command == "status":
                    outcome = ("IN_PROGRESS" if args.job or pipeline["status"] not in PIPELINE_TERMINAL
                               else "INSPECTED")
                if outcome:
                    if args.job and outcome != "CANDIDATE_MISMATCH":
                        _attach_history(client, project_id, args.pipeline, details, st)
                    if outcome == "PASSED":
                        deadline.check()  # never accept after the budget is spent
                    return finish(outcome)
                if deadline.remaining() <= 0:
                    return finish("TIMED_OUT", "maximum watch duration reached")
                sleep(min(args.poll_interval, deadline.remaining()))
    except UsageError as e:
        return finish("USAGE_ERROR", safe_text(str(e), 200))
    except LockError as e:
        return finish("LOCK_ERROR", safe_text(str(e), 200))
    except TimedOut:
        return finish("TIMED_OUT", "maximum duration reached")
    except ApiError as e:
        return finish("API_ERROR", "%s: %s" % (e.kind, e.message))
    except KeyboardInterrupt:
        return finish("INTERRUPTED", "interrupted by signal")
    except Exception as e:  # never leak details of unexpected failures
        return finish("INTERNAL_ERROR", "unexpected %s" % type(e).__name__)


def _attach_history(client, project_id, pipeline_id, details, st):
    """Historical attempts are reporting-only; they never influence acceptance."""
    try:
        hist = client.jobs(project_id, pipeline_id, include_retried=True)
    except ApiError:
        st["history"] = "unavailable"
        return
    for e in details["jobs"]:
        cur = e.get("id")
        e["previous_attempts"] = sorted(
            ({"id": j["id"], "status": safe_text(j["status"], 40)} for j in hist
             if j["name"] == e["requested_name"] and j["id"] != cur), key=lambda a: a["id"])
    st["history"] = "ok"


EPILOG = """\
exit codes: PASSED=0 FAILED=1 ACTION_REQUIRED=2 TIMED_OUT=3 API_ERROR=4
  CANDIDATE_MISMATCH=5 RUNNER_MISMATCH=6 ALREADY_MONITORING=7 IN_PROGRESS=8
  (status only) AMBIGUOUS_JOB=9 INSPECTED=10 (status without --job: nothing
  evaluated) LOCK_ERROR=11 OUTPUT_ERROR=12 USAGE_ERROR=64 INTERNAL_ERROR=70
  INTERRUPTED=130

examples:
  ci_monitor.py status --host gitlab.example.com --project acme/webapp --pipeline 1234 \\
      --sha <40-hex> --job build --job native-tests
  ci_monitor.py watch --host gitlab.example.com --project acme/webapp --pipeline 1234 \\
      --sha <40-hex> --job native-tests --runner-id native-tests=42 \\
      --poll-interval 60 --max-duration 3600 --output /tmp/ci-1234.json

Read-only: only `glab api -X GET` is used. stdout = one JSON document; stderr = progress.
Required-job acceptance is NOT overall pipeline status and proves nothing else.
--max-duration (default 3600, max 86400) covers target discovery, requests, retries and sleeps;
a killed glab may add up to ~1s of cleanup grace on top.
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    p = _Parser(prog="ci_monitor.py", description="Read-only GitLab CI acceptance monitor.",
                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name, helptext in (("status", "one-shot pipeline and job inspection"),
                           ("watch", "bounded monitoring of required jobs")):
        s = sub.add_parser(name, help=helptext, description=helptext, epilog=EPILOG, allow_abbrev=False,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        s.add_argument("--host", help="GitLab hostname (never defaulted to gitlab.com)")
        s.add_argument("--project", help="project path or numeric ID (default: unambiguous git remote)")
        s.add_argument("--pipeline", type=int, required=True, help="pipeline ID")
        s.add_argument("--sha", help="exact 40-hex candidate SHA the pipeline must be for")
        s.add_argument("--job", action="append", help="required job name (repeatable)")
        s.add_argument("--runner-id", action="append", help="expected runner ID: N (all required jobs) or JOB=N")
        s.add_argument("--poll-interval", type=float, default=60.0, help="seconds between polls (default 60)")
        s.add_argument("--max-duration", type=float, default=3600.0, help="total deadline in seconds (default 3600)")
        s.add_argument("--request-timeout", type=float, default=30.0, help="per-request timeout seconds (default 30)")
        s.add_argument("--retries", type=int, default=3, help="transient-error retries per request (default 3, max 10)")
        s.add_argument("--output", help="also write the JSON result to this new file")
        s.add_argument("--overwrite", action="store_true", help="allow --output to replace an existing regular file")
        s.add_argument("--lock-dir", help="directory for watch locks (default: $XDG_RUNTIME_DIR/ci-monitor)")
        s.add_argument("--glab-bin", default="glab", help="glab executable (default: glab)")
    return p


def dumps_strict(result):
    """Standard JSON only (allow_nan=False). On failure the result is replaced in place by a
    structured INTERNAL_ERROR so neither stdout nor the file can hold non-standard JSON or a PASS."""
    try:
        return json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (ValueError, TypeError):
        keep = {k: result.get(k) for k in ("command", "host", "pipeline_id", "expected_sha", "output_file")}
        result.clear()
        result.update(keep, schema_version=SCHEMA_VERSION, tool="ci_monitor", outcome="INTERNAL_ERROR",
                      exit_code=EXIT_CODES["INTERNAL_ERROR"], error="result contained a non-finite value",
                      missing_evidence=NOT_PROVEN)
        return json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _emit(result):
    sys.stdout.write(dumps_strict(result))
    sys.stdout.flush()
    return result["exit_code"]


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except UsageError as e:
        ctx = {"command": None}
        res = build_result("USAGE_ERROR", ctx, {}, safe_text(str(e), 200))
        print("usage error: " + safe_text(str(e), 200), file=sys.stderr)
        return _emit(res)

    def _term(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _term)
    # A background launcher may leave SIGINT ignored; install the handler explicitly.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    result = run(args)
    if args.output:
        if result["outcome"] == "USAGE_ERROR":
            result["output_file"] = {"path": safe_text(args.output, 300), "written": False,
                                     "error": "not written (usage error)"}
        else:
            result["output_file"] = {"path": safe_text(args.output, 300), "written": True}
            try:
                write_output(args.output, dumps_strict(result), args.overwrite)
            except (OSError, UsageError):
                result["underlying_outcome"] = result["outcome"]
                result["outcome"], result["exit_code"] = "OUTPUT_ERROR", EXIT_CODES["OUTPUT_ERROR"]
                result["output_file"] = {"path": safe_text(args.output, 300), "written": False,
                                         "error": "requested output file could not be written; "
                                                  "any existing file does NOT contain this result"}
                result["error"] = "output file write failed"
    return _emit(result)


if __name__ == "__main__":
    sys.exit(main())
