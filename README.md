# TRACEFORGE 2.0

**AI writes the fix. TRACEFORGE proves it.**

### The problem

AI coding tools can generate code fixes quickly, but how do you know the fix actually works?

A patch can pass one test and still introduce bugs, miss edge cases, or hide problems. Developers need evidence—not just an AI's confidence.

### Our solution

**TRACEFORGE is an independent verification layer for AI-generated code fixes.**

Upload your original project and a proposed patch. TRACEFORGE:

1. Runs the original project's tests to establish a baseline.
2. Applies the patch to a separate copy—not your original project.
3. Runs the tests again in an isolated Docker sandbox.
4. Compares the before-and-after results.
5. Generates a downloadable **Repair Passport** with the evidence, AI suggestions, and human review decision.

### Key features

- **Safe patch verification:** Original source files are not modified.
- **Isolated testing:** Docker-based execution with network-disabled test containers and resource limits.
- **Before-and-after comparison:** See whether the proposed fix improves the test results.
- **AI Patch Review:** Get additional suggestions and potential issues to investigate.
- **Human approval:** Review the evidence and explicitly approve or reject a patch.
- **Repair Passport:** Download a JSON record of the verification results.

Currently supported verification profiles: **Python and Node.js**.

Meta AI integration

TRACEFORGE is designed to use Meta Muse Spark 1.3 through the official Meta Model API.

Model: muse-spark-1.3

API base URL: https://api.meta.ai/v1

Chat Completions endpoint: https://api.meta.ai/v1/chat/completions

The Meta model provides an additional AI review layer that analyzes patch information and test summaries and returns structured suggestions.

Configure the following environment variables:

$env:TRACEFORGE_AI_REVIEW_PROVIDER="meta"
$env:MODEL_API_KEY="YOUR_META_API_KEY"
$env:TRACEFORGE_AI_REVIEW_MODEL="muse-spark-1.3"

Keep your API key private. Never commit API keys or other secrets to GitHub.

Important: AI review is advisory. AI-generated suggestions remain SUGGESTED until independently checked. A successful API connection alone does not mean a patch is verified.

### Evidence you can understand

| Status      | Meaning                                                        |
| ----------- | -------------------------------------------------------------- |
| `VERIFIED`  | A check was executed and passed.                               |
| `FAILED`    | A check was executed and failed.                               |
| `UNKNOWN`   | There is not enough evidence.                                  |
| `SUGGESTED` | An AI recommendation that has not been independently verified. |

**Passing tests does not prove complete correctness. AI suggestions are not execution evidence. Human approval does not prove correctness.**

### Run locally

**Requirements:** Python, Node.js, Docker Desktop, and Git.

**Backend:**

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Frontend** (open a second terminal):

```powershell
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

Docker Desktop must be running for sandbox verification. The backend builds missing sandbox images automatically.

### Project limitations

TRACEFORGE 2.0 is a hackathon MVP. It currently supports Python and Node.js verification, not every programming language. AI review is optional and depends on a working provider configuration. Passing tests cannot guarantee that software is bug-free.

### Vision Of the Project

**Don't just trust an AI-generated fix. Verify the evidence.**

TRACEFORGE — Evidence over assumptions.
