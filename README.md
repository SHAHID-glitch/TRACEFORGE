# TRACEFORGE 2.0

**AI writes the fix. TRACEFORGE proves it.**

This MVP verifies an uploaded project ZIP and `.patch`/`.diff` file without changing the uploaded source. It creates separate baseline and patched copies, runs detected tests in Docker with no network, compares the results, and exposes a JSON Repair Passport. Supported verification profiles currently include Python and Node.js; other languages are reported as unsupported.

## What exists

- Simple Mode and Engineering Mode dashboard.
- ZIP validation against path traversal, symlink, and suspicious archive entries.
- Patch validation and application with `git apply` in a disposable patched copy.
- Python and Node test detection.
- Docker execution with network disabled, CPU, memory, process, and timeout limits.
- Baseline/patched comparison and explicit approval/rejection controls.
- JSON Repair Passport download.

AI Patch Review is an optional advisory stage after verification. Select the backend-only provider with `TRACEFORGE_AI_REVIEW_PROVIDER=groq` or `baseten`. Groq uses its official OpenAI-compatible endpoint (`https://api.groq.com/openai/v1/chat/completions`), `TRACEFORGE_AI_REVIEW_API_KEY`, and a required configured `TRACEFORGE_AI_REVIEW_MODEL`. For Baseten, keep using `TRACEFORGE_AI_REVIEW_URL`, `TRACEFORGE_AI_REVIEW_API_KEY`, and `TRACEFORGE_AI_REVIEW_MODEL`; the existing Muse default is retained for compatibility. Example Groq configuration:

```powershell
$env:TRACEFORGE_AI_REVIEW_PROVIDER="groq"
$env:TRACEFORGE_AI_REVIEW_API_KEY="your-groq-key"
$env:TRACEFORGE_AI_REVIEW_MODEL="your-verified-groq-model"
```

To switch back to Baseten, set `TRACEFORGE_AI_REVIEW_PROVIDER="baseten"` and configure the Baseten HTTPS URL and model. The reviewer receives bounded patch text, changed paths, and test-result summaries—not secrets, environment variables, or the original project archive. It must return structured JSON findings and recommendations. AI output is always labeled `SUGGESTED`; it cannot apply patches, alter test results, approve changes, or turn unexecuted code into `VERIFIED`. If configuration is missing, the endpoint is not HTTPS, the provider fails, or its response is invalid, the normal verification workflow still completes and `ai_review.status` is `UNKNOWN`.

## Repair Passport

Every completed verification response is a JSON Repair Passport and can be downloaded from the results screen. The documented schema is [`backend/passport.schema.json`](./backend/passport.schema.json). It includes a unique `verification_id`, UTC `created_at`, project and patch metadata, dependency preparation diagnostics, normalized baseline/patched test evidence and counts, tool versions, overall result, AI review findings, and `human_approval.status: "PENDING"`. The server and download action emit the same data with stable key ordering; IDs and timestamps are intentionally unique per verification.

Example (abridged, synthetic data only):

```json
{
  "schema_version": "2.1",
  "product": "TRACEFORGE",
  "verification_id": "00000000-0000-4000-8000-000000000000",
  "created_at": "2026-11-02T00:00:00Z",
  "verification": {
    "status": "TESTS_PASSED",
    "tests_passed": true,
    "complete_correctness": "UNKNOWN"
  },
  "patch": {
    "status": "VERIFIED",
    "applied": true,
    "changed_files": ["src/app.py"]
  },
  "baseline": {
    "status": "FAILED",
    "counts": {"passed": 3, "failed": 1, "skipped": 0, "total": 4},
    "exit_code": 1,
    "duration_ms": 842
  },
  "patched": {
    "status": "VERIFIED",
    "counts": {"passed": 4, "failed": 0, "skipped": 0, "total": 4},
    "exit_code": 0,
    "duration_ms": 790
  },
  "ai_review": {
    "status": "SUGGESTED",
    "findings": [
      {
        "status": "SUGGESTED",
        "severity": "LOW",
        "title": "Add a focused edge-case test",
        "explanation": "The changed path would benefit from an explicit boundary test.",
        "files": ["src/app.py"],
        "confidence": "MEDIUM"
      }
    ],
    "recommendations": [
      {"status": "SUGGESTED", "text": "Add and run a focused boundary test."}
    ]
  },
  "human_approval": {"status": "PENDING"}
}
```

`VERIFIED` means directly executed or measured evidence; `FAILED` means a check executed and did not pass; `UNKNOWN` means evidence was unavailable or not run; and `SUGGESTED` is reserved for untested AI findings and recommendations. In this synthetic example, the patch and patched tests are `VERIFIED`, the baseline tests are `FAILED`, and the AI review is `SUGGESTED`. Passing tests never establishes complete correctness.

Human review is explicit and separate from verification. Each passport starts with `human_approval.decision: "PENDING"`; the UI records one final `APPROVED` or `REJECTED` decision against that passport's verification ID after the user reviews the evidence and any AI suggestions. Decisions are immutable, and approval never changes test results or proves correctness. Each verification also receives a cryptographically random, verification-scoped approval capability delivered in a response header; it is required for approval and passport retrieval, expires after `TRACEFORGE_APPROVAL_TOKEN_TTL_SECONDS` (default 1 hour), remains valid after a final decision until expiry, and is never included in the passport, logs, or AI context. Passports, token hashes, expiry timestamps, and decisions are stored in SQLite at `TRACEFORGE_DATABASE_PATH` (default `backend/traceforge.sqlite3`), allowing restarts and multiple workers to share state. Back up the database, `-wal`, and `-shm` files together using SQLite-aware procedures, protect the directory with a service-account-only ACL (on Windows, use `icacls`; on Linux, use mode `700` for the directory and `600` for the database), and use a durable private volume in deployments. The token is held only in frontend component memory and is not recoverable after a page reload or expiry; it is never stored in localStorage or sessionStorage. This local-demo mechanism is not a substitute for production authentication or a multi-user identity system. When the frontend is served over HTTPS, it refuses an HTTP API URL.

For production, set `TRACEFORGE_ENV=production`, `TRACEFORGE_API_URL` to an HTTPS URL, and `TRACEFORGE_ALLOWED_ORIGINS` to a comma-separated explicit list of HTTPS frontend origins. Production rejects wildcard CORS origins and non-HTTPS API URLs. The frontend build should set `NEXT_PUBLIC_TRACEFORGE_ENV=production` and `NEXT_PUBLIC_API_URL` to the same HTTPS API URL. HTTP localhost remains supported in development.

## Run locally

### Backend

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Docker Desktop must be running. The backend builds missing sandbox images automatically, or they can be built explicitly from the repository root:

```powershell
docker build -t traceforge-python:3.12-pytest -f backend/sandbox/python.Dockerfile backend/sandbox
docker build -t traceforge-node:22 -f backend/sandbox/node.Dockerfile backend/sandbox
docker build -t traceforge-node-proxy:1 -f backend/sandbox/proxy.Dockerfile backend/sandbox
```

Python projects use the Python sandbox image; it includes pytest and runs with the project mounted at `/workspace` on `PYTHONPATH`. Node.js projects use the Node sandbox image. Node dependencies are prepared once in a disposable writable copy, using `npm ci` only after a valid `package-lock.json` has been checked for registry-hosted integrity hashes. Preparation uses the configured trusted HTTPS registry (`TRACEFORGE_NPM_REGISTRY`, checked against `TRACEFORGE_NPM_REGISTRY_ALLOWLIST`) through a disposable allowlisting proxy. The installer is attached only to an internal Docker network shared with that proxy; it has no direct external network route, so registry configuration is not the security boundary. The proxy permits only the approved registry host, rejects other CONNECT and HTTP targets, rejects IP literals and private/link-local/loopback/multicast/reserved resolutions, and connects to the already-validated address to avoid DNS rebinding between validation and connection. Lifecycle scripts, audit, and funding requests are disabled. Projects with external dependencies must provide a lockfile; packages whose installation requires lifecycle scripts are reported as preparation failures under the default policy rather than being executed. The prepared dependency tree is copied into separate baseline and patched workspaces. Both test containers are read-only and use `--network none`. Installation diagnostics include Node/npm versions, npm configuration, package metadata sizes, and the final install debug-log tail. Installation and test evidence are captured separately, with configurable `TRACEFORGE_INSTALL_TIMEOUT_SECONDS` and `TRACEFORGE_TEST_TIMEOUT_SECONDS` values.

Python projects with `requirements.txt` use a separate dependency-preparation container. Set `TRACEFORGE_PYPI_INDEX` and `TRACEFORGE_PYPI_ALLOWLIST` to select an approved HTTPS package index (defaults to PyPI plus `files.pythonhosted.org`). Requirements are installed with `pip --target` using `--only-binary=:all:`; direct URLs, VCS references, nested requirement/constraint files, alternate indexes, trusted-host options, and remote package directories are rejected. Preparation uses the same disposable internal Docker network and allowlisting proxy boundary as Node, and fails closed if that boundary cannot be established. Fully pinned `==`/`===` requirements are marked reproducible; unpinned requirements are accepted but explicitly marked non-reproducible. Preparation output, stderr, exit code, duration, and errors are recorded separately. A failed preparation leaves both test results `UNKNOWN`; test containers remain network-disabled. Docker may need to pull the base images and build these sandbox images once.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:3000. The backend is expected at http://localhost:8000 (override with `NEXT_PUBLIC_API_URL`).

### Configuration reference

| Variable | Purpose | Default |
|---|---|---|
| `TRACEFORGE_AI_REVIEW_PROVIDER` | `groq` or `baseten` | `baseten` |
| `TRACEFORGE_AI_REVIEW_API_KEY` | Backend-only provider key | none |
| `TRACEFORGE_AI_REVIEW_MODEL` | Provider model; required for Groq | none for Groq; Muse default for Baseten |
| `TRACEFORGE_AI_REVIEW_URL` | Baseten HTTPS endpoint | none |
| `TRACEFORGE_DATABASE_PATH` | Absolute SQLite database path | `backend/traceforge.sqlite3` |
| `TRACEFORGE_APPROVAL_TOKEN_TTL_SECONDS` | Capability-token lifetime | `3600` |
| `TRACEFORGE_ENV` | Set to `production` for production checks | `development` |
| `TRACEFORGE_API_URL` | Backend URL required in production | none |
| `TRACEFORGE_ALLOWED_ORIGINS` | Comma-separated CORS origins | `http://localhost:3000` |
| `NEXT_PUBLIC_API_URL` | Frontend API URL, embedded at build time | `http://localhost:8000` |
| `NEXT_PUBLIC_TRACEFORGE_ENV` | Frontend production HTTPS check | `development` |

Never commit `.env` files or API keys. For production, build the frontend after setting its `NEXT_PUBLIC_*` values; changing them after the Next.js build does not update the browser bundle. Use an explicit HTTPS API URL and HTTPS CORS origins. Localhost HTTP remains supported for development.

## Limitations

This is an MVP, not production-hardened. Uploads are stored only in temporary directories during a request. Docker isolation is a defense-in-depth measure, but a production deployment needs a dedicated worker boundary, quotas, observability, image pinning, and operational database backup/restore procedures. Passing tests is not proof that a patch is completely correct. AI findings and recommendations are always `SUGGESTED`, never execution evidence. Human `APPROVED` or `REJECTED` decisions record review intent but do not establish correctness. Groq and Baseten integrations are optional and must be configured and tested separately; this repository does not claim that a live provider request has succeeded.
