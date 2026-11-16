# gitlab-ci-monitor reference

## Acceptance semantics (per required job, from GitLab's current job set)
| Job state | Verdict |
|---|---|
| created/pending/preparing/running/scheduled | pending (keep waiting; `watch` only) |
| success (runner matches if constrained) | OK; `allow_failure` does not matter once it actually passed |
| failed/canceled (even with `allow_failure: true`) | FAILED |
| skipped | FAILED (not accepted) |
| manual (required) | ACTION_REQUIRED |
| success on a runner other than the requested one, or runner unknown when constrained | RUNNER_MISMATCH |
| absent | pending while the pipeline runs; FAILED once the pipeline is terminal |
| >1 distinct current job with the name (any stage), or current job flagged `retried` | AMBIGUOUS_JOB |
| job's embedded pipeline ID / SHA missing or different; project/commit different when given | CANDIDATE_MISMATCH |

Optional manual jobs are not required and never affect acceptance. Pipeline ID, project and SHA must equal the request.
Priority: CANDIDATE_MISMATCH > FAILED > RUNNER_MISMATCH > AMBIGUOUS_JOB > ACTION_REQUIRED > pending > PASSED.

## Current vs historical attempts
`GET .../pipelines/N/jobs?include_retried=false` is authoritative for the current attempt. A second request with
`include_retried=true` fills `previous_attempts` for reporting only; its failure sets `history: "unavailable"` and
never changes acceptance.

## Result JSON (schema_version 2)
`outcome`, `exit_code`, `host`, `project{requested,id,path,source}`, `pipeline_id`, `expected_sha`, `runner_constraints`,
`overall_pipeline{status,sha,sha_verified}`, `required_job_acceptance{status,required_jobs[],reason,history}`,
`poll_details{current,last_successful_poll,note}`, `all_jobs[]`, `all_jobs_total`, `all_jobs_truncated`,
`missing_evidence[]`, `elapsed_seconds`, `polls`, `error`, and (with `--output`) `output_file`.
Acceptance status: ACCEPTED / NOT_ACCEPTED / PENDING / NOT_EVALUATED. When `poll_details.current` is false, any job
details are stale and must not be treated as the final state.

## Errors and sanitizing
Errors are fixed strings chosen from an allowlist; glab/GitLab output is classified internally and discarded.
Remote strings echoed back (stage, status, job list names) are stripped of control characters and bounded.

## Reliability
Per-request timeout; finite retries (default 3, max 10) with 1,2,4,…≤30 s backoff clipped to the deadline;
401/403/404, schema errors and inconsistent pagination are reported immediately. Pagination: 100 per page, de-duplicated by
job ID, max 50 pages. Lock: `flock` on a never-written file keyed by host|numeric project ID|pipeline in
`$XDG_RUNTIME_DIR/ci-monitor` (fallback `~/.cache/ci-monitor`); the directory must be a real directory owned by you and not
group/world-writable; the file must be a private regular file with one link.

## Agent examples
- Claude Code: ask it to "use the gitlab-ci-monitor skill to watch pipeline N"; run watch via a background Bash command.
- Codex: `$gitlab-ci-monitor` or a plain request; run the CLI in the shell tool.
- OpenCode (DeepSeek or other): skill is read from `~/.claude/skills` / `~/.agents/skills`; invoke the CLI via its bash tool.
