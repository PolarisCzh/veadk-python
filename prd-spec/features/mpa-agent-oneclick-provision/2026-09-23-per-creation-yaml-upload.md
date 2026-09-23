# Per-creation YAML upload for Studio MPA creation

[中文版](2026-09-23-per-creation-yaml-upload.zh.md)

- Change ID: `per-creation-yaml-upload`
- Created/revised: 2026-09-23
- Status: implemented; local verification complete, cloud/browser verification pending
- Component: [Studio MPA creation](../../../specs/studio-mpa-creation/README.md)
- Approval: the user requested uploading YAML for one creation on 2026-09-23. Administrator-only upload is the conservative security boundary while the optional role question remains unanswered.

## Background and evidence

`GET /web/mpa-creation/config` currently loads only `VEADK_MPA_CREATE_CONFIG` or `mpa-create.config.yaml`. A cloud Studio without that file disables creation. `POST /web/mpa-creation/tasks` accepts only a small JSON request and passes the server-selected path to a child process. The task service uses a local SQLite database and an in-process supervisor; a VeFaaS instance loss already interrupts a task. The YAML may contain secrets and server-environment references. Uploading it to a global path or storing it in browser/session storage would broaden exposure and affect unrelated creations.

## Goals and non-goals

- Allow an authorized Studio administrator to select a YAML file in the MPA creation dialog and use it for this creation only, even when the default server profile is absent.
- Validate the uploaded profile before starting the task, bind the exact bytes to the task, and remove temporary content after success, failure, cancellation, or rejected submission.
- Preserve the existing server-profile and CLI flows.
- Do not persist YAML in browser storage, task SQLite, logs, Runtime output, or a global Studio profile; do not provision missing PostgreSQL, IAM, model, or network prerequisites.
- Do not change the existing task supervisor's cross-instance/restart recovery contract.

## Scenarios and requirements

- **FR-1:** Given an administrator with an absent default profile, selecting a UTF-8 `.yaml`/`.yml` file of at most 256 KiB enables creation using only that file. The UI shows the filename and upload/validation errors without showing file contents.
- **FR-2:** Given an invalid, oversized, wrong-region, or unsupported uploaded profile, the server rejects it before creating a task. Uploaded `managed.template-file` and `managed.credential-file` are rejected because the browser cannot supply trusted companion server files.
- **FR-3:** Given a non-administrator, uploading a profile is forbidden; ordinary agent-management users retain the server-profile creation path.
- **FR-4:** Given a valid upload, the task receives a private mode-0600 temporary file containing the submitted bytes. The task owns that path until its child exits. The file is removed on all terminal and pre-start paths. The task's persisted payload contains only a digest, never YAML or secrets.
- **FR-5:** Given a repeated request ID, the same input and YAML digest preserve idempotency; changed YAML conflicts. After a lost response or dialog reopen, the browser must reselect the file to retry; the file never enters session storage.
- **FR-6:** When a file is selected, blank MPA/Worker image fields mean defaults from that uploaded file, not the server profile. Selecting a file clears only untouched server-prefilled image values; deliberate image edits remain explicit overrides.

## Design and contract impact

The dialog uses the existing input and button styles, a native file input, and an explicit filename/validation message. The existing configuration response adds a nonsecret `uploadAllowed` flag. `POST /web/mpa-creation/tasks` retains JSON compatibility and accepts optional `configYaml` text; the request cap increases only for uploaded requests. The server performs the existing agent-management check, then a separate administrator check before using uploaded bytes. It validates syntax, managed schema, region, server prerequisites, and unsupported file references. Its mode-0600 temporary file is created alongside the task store, not at the configured global path. The task service owns cleanup, including duplicate and error paths. The child runner remains path-based. Existing CLI and non-upload API clients are unchanged.

An uploaded profile takes precedence over a configured server profile for this task only. On file selection, untouched image fields prefilled from the server profile become blank so that the uploaded defaults are used; explicit user edits are retained. No upload preflight endpoint or permanent configuration update is added.

Configuration content is not returned by any endpoint. The upload is not a substitute for `DEPLOYMENT_DATABASE_ADMIN_URL`, `SHARED_APIG_DATABASE_URL`, cloud credentials/IAM role, or other server dependencies. An interrupted VeFaaS instance still interrupts the existing supervisor; the upload is bound to a task within that lifecycle, not a new durable job system. The file input is not persisted across reloads.

## Tasks and verification

| Task | Requirements | Files / checks |
| --- | --- | --- |
| `T-1` | `FR-1`–`FR-5` | Add failing route, task, and dialog tests for success, authorization, size, invalid YAML, region, duplicate digest, cleanup, and retry. |
| `T-2` | `FR-2`–`FR-5` | Update `frontend/server/mpa_creation.py`, task ownership/cleanup, and Studio role wiring. Preserve CLI behavior. |
| `T-3` | `FR-1`, `FR-5`, `FR-6` | Update frontend client/dialog/localization; keep YAML out of session storage and preserve explicit image overrides. |
| `T-4` | all | Run targeted Python tests, Ruff/Pyright, frontend tests/build, browser check, secret scan, and reconcile bilingual docs/spec. |

| Requirement | Acceptance | Verification | Result |
| --- | --- | --- | --- |
| `FR-1` | `AC-1`: admin can create with an uploaded profile and missing global file | route/dialog tests and browser | pass in local route/dialog tests; browser not_run |
| `FR-2` | `AC-2`: invalid/oversized/file-reference profiles fail safely before task start | route tests | pass |
| `FR-3` | `AC-3`: non-admin upload is rejected | route test | pass |
| `FR-4` | `AC-4`: YAML is absent from snapshots/storage and temporary files are removed | task tests | pass |
| `FR-5` | `AC-5`: same digest retries, changed digest conflicts, browser reselects after reopen | task/dialog tests | pass |
| `FR-6` | `AC-6`: uploaded defaults win over untouched server-prefilled images | dialog tests | pass |

## Risks, review and delivery

Design review checked the administrator permission boundary, request-size handling, idempotent cleanup, error redaction, and bilingual equivalence. An error-path test found a temporary-file cleanup gap before task ownership; the implementation was corrected and the test now passes. Uploaded YAML can still reference server environment variables in the existing profile contract; only trusted administrators may upload it. Cloud E2E requires configured server database URLs and a prepared account; local tests cannot prove those prerequisites. No cloud deployment is authorized by this design alone. Local checks on 2026-09-23: 78 focused Python/CLI tests pass, including a route-to-child test proving the exact uploaded file is read and removed; 1215 frontend unit tests and 31 Vitest tests pass; frontend build, packaged-asset validation, i18n validation, Ruff changed-file checks and secret scan pass; Python changed-line coverage is 96% and frontend changed-line coverage is 97%. The broader Python regression yielded 4873 passed, 8 skipped, 2 xfailed, and 2 failed; both failures are existing flat-mode `test_service.py` cases using `agent` where the unchanged `mpa_provision.py` validator requires `mi-[0-9a-z]{12}`. Pyright passes for the two changed backend/task files; the large existing `cli_frontend.py` file has 36 type errors outside the new callback wiring. Real browser and cloud E2E remain not_run.
