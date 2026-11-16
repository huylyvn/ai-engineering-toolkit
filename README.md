# ai-engineering-toolkit

Small, agent-independent engineering utilities. Version 1 ships one portable
Agent Skill: **gitlab-ci-monitor**, a read-only GitLab CI acceptance monitor.

## The problem
Coding agents waste context polling CI, spawn duplicate monitors, mis-parse JSON, trust "green pipeline" as
"my job passed", and sometimes act on the wrong pipeline or commit. This toolkit moves monitoring into one
deterministic Python CLI (standard library + the authenticated [`glab`](https://gitlab.com/gitlab-org/cli) CLI)
that any agent able to run a shell command can reuse. Python does the polling; the agent does the judgment;
the human keeps consequential approvals (merge, deploy, retries).

## Features
- `status` (one-shot) and `watch` (bounded polling) for explicitly required jobs of an exact pipeline and commit SHA.
- Required-job acceptance reported separately from overall pipeline status; never infers test counts or artifact integrity.
- Handles retries (GitLab's current-job set is authoritative), manual/skipped/allow-failure jobs, runner pinning, ambiguity.
- Verifies project identity (path or numeric ID), pipeline ID, commit SHA and job-embedded identities; fails closed.
- Strictly read-only: only `glab api -X GET`. Never retries, plays, cancels, merges, pushes or deploys.
- One end-to-end deadline, finite retries, bounded pagination, process-group cleanup, OS-backed duplicate-monitor lock.
- Stable exit codes and one standard-JSON document on stdout; progress on stderr; error text is allowlisted (no secret echo).
- Safe, idempotent symlink installer with dry-run and clean uninstall.

## Architecture
```
skills/gitlab-ci-monitor/
  SKILL.md                    portable agent instructions (Agent Skills frontmatter)
  scripts/ci_monitor.py       the single canonical implementation (stdlib only)
  references/                 semantics and result format
  tests/test_ci_monitor.py    mocked unittest suite (no network, no credentials)
install.sh                    symlinks the skill into user-level skill directories
```
A skill is a *local filesystem* capability: a remote/cloud agent cannot use it unless it runs on a machine that
has this checkout and `glab` authentication.

## Install (any machine)
```
git clone <repository-url> ai-engineering-toolkit && cd ai-engineering-toolkit
./install.sh --dry-run      # preflights every destination, changes nothing
./install.sh                # symlinks into ~/.claude/skills and ~/.agents/skills
./install.sh --uninstall    # removes only links whose target is exactly this checkout's skill
```
- There is **one canonical implementation** (`skills/gitlab-ci-monitor`). Agents may *discover* it through
  more than one registered path; every installer-created path is a symlink to that single directory.
- Claude Code reads `~/.claude/skills`; Codex reads `~/.agents/skills`. OpenCode's documented discovery also
  includes both of those user-level directories, so the installer provides **only these user-level links**
  and never writes `~/.config/opencode`.
- `--agents opencode` checks the user-level links only: it refuses if a foreign
  `~/.agents/skills/gitlab-ci-monitor` would override ours, installs the `~/.agents/skills` link when no
  canonical link exists, and does nothing if one already exists. Those links are shared with Claude/Codex,
  so `--uninstall --agents opencode` removes nothing (use `--uninstall --agents claude,codex`).
  **No guarantee is made that OpenCode loads this skill**: higher-precedence native, project or config
  definitions with the same ID may override it, and OpenCode is not installed/verified here.
- **Foreign overrides:** another skill with the same ID in a higher-precedence location (project
  `.opencode/skills`, `.agents/skills` in a repo, `~/.config/opencode/skills`, explicit config) silently wins in
  OpenCode; the installer only checks the user-level locations above.
- Safety: all destinations are preflighted before any change; foreign files, directories, foreign/broken
  symlinks and non-directory parents are refused; any failed `mkdir`/`ln`/`rm` gives a nonzero exit and rolls
  back links created in that run; links use `ln -s -T` so nothing can be nested; no sudo, no credentials, no agent settings.

Requirements: Linux, `python3` (≥3.9), `git`, `glab` logged in to the target host (`glab auth status`).

## Direct CLI use (no skill discovery needed)
```
S=skills/gitlab-ci-monitor/scripts/ci_monitor.py
python3 $S status --host gitlab.example.com --project acme/webapp --pipeline 1234 --sha <40-hex> --job build
python3 $S watch  --host gitlab.example.com --project acme/webapp --pipeline 1234 --sha <40-hex> \
     --job native-tests --runner-id native-tests=42 --poll-interval 60 --max-duration 3600 --output ci.json
python3 $S watch --help
```
Host is never defaulted to gitlab.com. If `--host`/`--project` are omitted they are taken from git remotes
only when exactly one candidate exists.

## CLI contract
stdout: exactly one standard-JSON document (`schema_version: 2`, serialized with `allow_nan=False`; a non-finite value would yield `INTERNAL_ERROR`), also for argument/parser errors (`USAGE_ERROR`,
`--help` stays plain text). stderr: progress only (control characters stripped). `--output` additionally writes
the same JSON to a **new** file (refuses existing files unless `--overwrite`, refuses symlinks/non-regular files,
atomic replace). If the requested write fails the outcome becomes `OUTPUT_ERROR` (exit 12) with
`underlying_outcome` and `output_file.written=false`; a pre-existing file never holds the current result.

| Outcome | Exit | Meaning |
|---|---|---|
| PASSED | 0 | exact SHA/pipeline/project verified, every required job in GitLab's *current* job set succeeded (runner matched) |
| FAILED | 1 | a required job failed/canceled/skipped/missing |
| ACTION_REQUIRED | 2 | a required manual job needs the owner |
| TIMED_OUT | 3 | `--max-duration` reached (never accepts after the budget is spent) |
| API_ERROR | 4 | fixed allowlisted message: auth/permission/not_found/rate_limited/server_error/cli_error/timeout/empty_response/malformed_json (incl. NaN/Infinity/1e999)/schema/glab_missing/pagination/project_mismatch (requested path resolved to a different project, e.g. moved/redirected: pass the current path or numeric ID) |
| CANDIDATE_MISMATCH | 5 | pipeline or job identity (pipeline ID, project, SHA, commit) missing or not exact |
| RUNNER_MISMATCH | 6 | required job ran on an unexpected/unknown runner |
| ALREADY_MONITORING | 7 | another `watch` holds the lock for this canonical project+pipeline |
| IN_PROGRESS | 8 | `status` only: still running |
| AMBIGUOUS_JOB | 9 | several distinct current jobs share a required name (any stage) or a current job is flagged retried |
| INSPECTED | 10 | `status` without `--job`: described, nothing evaluated |
| LOCK_ERROR | 11 | lock directory/file unsafe (symlink, hard link, special file, wrong owner/perms) |
| OUTPUT_ERROR | 12 | requested `--output` could not be written |
| USAGE_ERROR | 64 | bad arguments (incl. non-finite/non-positive durations, unknown runner-rule job, bad runner IDs) |
| INTERNAL_ERROR | 70 | unexpected exception (type name only) |
| INTERRUPTED | 130 | SIGINT/SIGTERM |

Result highlights: `overall_pipeline` and `required_job_acceptance` are separate; `project{requested,id,path}`
is the verified canonical identity; `runner_constraints` echoes the requested rules; `poll_details.current`
says whether job details are from the final poll or **stale** (after an API error); `all_jobs` is capped at 500
with `all_jobs_truncated`/`all_jobs_total`; `missing_evidence` lists what a pass does not prove.

Semantics: the *current* job set is GitLab's `include_retried=false` list (authoritative); historical attempts
(`include_retried=true`) are fetched only for reporting (`previous_attempts`) and never decide acceptance.
`--runner-id` requires at least one `--job`; `N` applies to every required job; `JOB=N` to one required job (typos, invalid IDs, mixing and
contradictions are rejected). A project path must equal the returned `path_with_namespace` after normalization (case-insensitive, surrounding slashes and trailing `.git` ignored); a numeric ID must equal the returned ID. Both resolve (read-only GET) to one numeric project ID,
used for requests and locks. Details: `skills/gitlab-ci-monitor/references/gitlab-workflow.md`.

## Runtime budget and process boundary
`--max-duration` (default 3600, max 86400) is a single monotonic deadline from process start covering target
discovery, project lookup, requests, retries and sleeps. After a timeout/SIGINT/SIGTERM the glab process
group is SIGKILLed and its pipes are drained then closed within a separate **cleanup grace of ≤1 s per killed
request** (not part of the requested duration). glab is trusted: a descendant that deliberately leaves the
process group (setsid) and survives is outside the containment contract, but it cannot block the monitor
(pipes are closed after the grace). No supervisor is built.

## Agent compatibility
| Agent | Status |
|---|---|
| Claude Code | Skill listed by a live Claude Code 2.1.x session after installing the user-level link (verified once by the author). |
| Codex CLI | Skill listed by `codex debug prompt-input` (no model call) from an unrelated directory with codex-cli 0.166.0 (verified once by the author). |
| OpenCode | **Not verified.** Its documentation says it discovers `~/.claude/skills` and `~/.agents/skills`, so the same links should work, but this has not been tested and higher-precedence definitions can override the skill. |

"Discovered" is not the same as "the model chooses to use it correctly"; end-to-end agent behavior was not benchmarked.

## Agent usage examples
Run the watch once (in the background if the agent supports it) and read the result afterwards. Installing the skill
does not wake an exited session.
- **Claude Code**: `claude "Use the gitlab-ci-monitor skill: watch pipeline 1234 on gitlab.example.com acme/webapp for sha <sha>, jobs build"`
- **Codex**: `codex "Use \$gitlab-ci-monitor to check pipeline 1234 on gitlab.example.com acme/webapp, sha <sha>, jobs build"`
- **OpenCode** (unverified): `opencode run "Load the gitlab-ci-monitor skill and run one bounded watch for ..."`
- **Plain shell**: `python3 skills/gitlab-ci-monitor/scripts/ci_monitor.py watch ... --output ci.json &` then read `ci.json`.

## Testing
```
python3 -m unittest discover -v -s skills/gitlab-ci-monitor/tests
```
The suite (mocked GitLab responses, fake `glab` processes, temporary-HOME installer runs) needs no network or
credentials; it is what the GitHub Actions workflow runs. Developed and tested on Linux with Python 3.14; CI also targets
3.10, 3.12 and 3.13. Two permission-based tests skip when run as root.

## Security boundaries
Read-only by construction; remote text (job names, logs, API errors) is treated as data, never instructions; error
output is fixed strings; echoed remote strings are control-character stripped; no tokens are read or printed
(authentication is entirely `glab`'s); lock and output files are written with `O_NOFOLLOW`/exclusive creation and
never overwrite foreign files. glab is trusted: a descendant that deliberately escapes its process group is not
contained. CI status never authorizes merge or deployment on its own. The tool is not a substitute for human review.

## Known limitations
- Runner constraints are by numeric runner ID only; an unknown runner under a constraint is a mismatch.
- Job identity: `job.pipeline.id` and `job.pipeline.sha` must be present; `project_id` and `commit.id` are
  verified when GitLab provides them (GitLab versions omitting pipeline info on jobs fail closed).
- Lock is advisory and local to one machine/user; it does not stop monitors on other machines.
- Requests fail closed on inconsistent pagination rather than retrying it.
- A detached (setsid) glab descendant is not killed (see above).
- The installer checks user-level locations only; project-level or config-level same-ID skills can override.
- SIGINT/SIGTERM handlers are installed explicitly, so a watch started with SIGINT ignored by a background launcher still exits 130 with cleanup.
- Verified against one GitLab instance's API schema; other versions may omit fields and will then fail closed.

## License
MIT. See [LICENSE](LICENSE).
