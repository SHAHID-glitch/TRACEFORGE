"use client";

import { ChangeEvent, FormEvent, useState } from "react";

type Result = Record<string, any>;

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const DEPLOYMENT_ENV = process.env.NEXT_PUBLIC_TRACEFORGE_ENV ?? "development";

function apiUrl(): string {
  if (DEPLOYMENT_ENV === "production" && !API.startsWith("https://")) {
    throw new Error("TRACEFORGE requires an HTTPS API in production.");
  }
  if (typeof window !== "undefined" && window.location.protocol === "https:" && API.startsWith("http://")) {
    throw new Error("TRACEFORGE requires an HTTPS API when the frontend is served over HTTPS.");
  }
  return API;
}

export default function Home() {
  const [project, setProject] = useState<File | null>(null);
  const [patch, setPatch] = useState<File | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"simple" | "engineering">("simple");
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [approvalToken, setApprovalToken] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!project || !patch) return setError("Choose both a project ZIP and a patch file.");
    setBusy(true); setError(""); setResult(null); setApprovalToken("");
    const form = new FormData(); form.append("project_zip", project); form.append("patch_file", patch);
    try {
      const response = await fetch(`${apiUrl()}/api/verify`, { method: "POST", body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Verification failed.");
      const token = response.headers.get("X-Traceforge-Approval-Token");
      if (!token) throw new Error("Verification did not return approval authorization.");
      setApprovalToken(token);
      setResult(data);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Verification failed."); }
    finally { setBusy(false); }
  }

  async function decide(decision: "APPROVED" | "REJECTED") {
    if (!result || result.human_approval?.decision !== "PENDING") return;
    setDecisionBusy(true); setError("");
    try {
      const response = await fetch(`${apiUrl()}/api/approval`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Traceforge-Approval-Token": approvalToken,
        },
        body: JSON.stringify({ verification_id: result.verification_id, decision }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not record human decision.");
      setResult(data);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not record human decision.");
    } finally { setDecisionBusy(false); }
  }

  const choose = (setter: (file: File | null) => void) => (event: ChangeEvent<HTMLInputElement>) => setter(event.target.files?.[0] ?? null);
  const stableStringify = (value: unknown): string => {
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.keys(value as Record<string, unknown>).sort().map(key => `${JSON.stringify(key)}:${stableStringify((value as Record<string, unknown>)[key])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  };
  const download = () => {
    if (!result) return;
    const url = URL.createObjectURL(new Blob([stableStringify(result)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "repair-passport.json"; link.click(); URL.revokeObjectURL(url);
  };
  const statusClass = (status: string) => status === "VERIFIED" ? "verified" : status === "FAILED" ? "failed" : "unknown";
  const outcomeLabel = (status: string) => status === "TESTS_PASSED" ? "TESTS PASSED" : status === "TESTS_FAILED" ? "TESTS FAILED" : status === "PATCH_APPLICATION_FAILED" ? "PATCH APPLICATION FAILED" : "UNKNOWN";

  return <main>
    <header><div className="brand"><span className="mark">TF</span><div><strong>TRACEFORGE</strong><small>Independent verification for AI code</small></div></div><div className="mode"><button className={mode === "simple" ? "active" : ""} onClick={() => setMode("simple")}>Simple Mode</button><button className={mode === "engineering" ? "active" : ""} onClick={() => setMode("engineering")}>Engineering Mode</button></div></header>
    <section className="hero"><p className="eyebrow">AI writes the fix. TRACEFORGE proves it.</p><h1>Know if the fix<br /><em>actually works.</em></h1><p className="lead">Upload your original project and a proposed patch. We run the evidence in an isolated Docker sandbox—without changing your source.</p></section>
    <section className="workspace">
      <div className="card upload-card"><div className="card-heading"><span className="step">01</span><div><h2>Check an AI-generated code fix</h2><p>We compare your project before and after the patch.</p></div></div>
        <form onSubmit={submit}><label className="drop"><span className="icon">◈</span><strong>{project ? project.name : "Drop your project ZIP here"}</strong><small>{project ? "Ready to verify" : "or click to browse · max 50 MB"}</small><input type="file" accept=".zip" onChange={choose(setProject)} /></label>
        <label className="drop"><span className="icon">⌁</span><strong>{patch ? patch.name : "Drop your patch here"}</strong><small>{patch ? "Ready to verify" : ".patch or .diff · max 50 MB"}</small><input type="file" accept=".patch,.diff" onChange={choose(setPatch)} /></label>
        {error && <p className="error">{error}</p>}<button className="primary" disabled={busy}>{busy ? "Running verification…" : "Run independent verification →"}</button></form>
      </div>
      <aside className="side-card"><h3>More ways to use TRACEFORGE</h3><div className="coming"><b>⌕</b><span><strong>Inspect an existing project</strong><small>Coming soon</small></span></div><div className="coming"><b>✦</b><span><strong>Improve my code</strong><small>Coming soon</small></span></div><div className="coming"><b>⑂</b><span><strong>Review a GitHub pull request</strong><small>Coming soon</small></span></div></aside>
    </section>
    {result && <section className="results">    <div className="result-head"><div><p className="eyebrow">{outcomeLabel(result.verification?.status)}</p><h2>Your Repair Passport</h2></div><button className="secondary" onClick={download}>Download JSON ↗</button></div>
      <div className="comparison"><div><span>BASELINE</span><strong className={statusClass(result.baseline.status)}>{result.baseline.status}</strong><small>{result.baseline.duration_ms} ms</small></div><div className="arrow">→</div><div><span>PATCHED</span><strong className={statusClass(result.patched.status)}>{result.patched.status}</strong><small>{result.patched.duration_ms} ms</small></div></div>
      <div className="passport-grid"><div><span className="label">VERIFICATION ID</span><p>{result.verification_id}</p><small>{result.created_at}</small></div><div><span className="label">PROJECT</span><p>{result.project.language}</p><small>{result.project.root}</small></div><div><span className="label">TEST COMMAND</span><p>{result.project.test_command || "No runnable tests"}</p></div><div><span className="label">DEPENDENCIES</span><p>{result.dependency_preparation?.status || result.installation?.baseline?.status || "Not required"}</p><small>{result.dependency_preparation?.failure_reason || result.installation?.baseline?.failure_reason || "Dependencies ready or not required"}</small></div><div><span className="label">PATCH FILES</span><p>{result.patch.changed_files.length || "None detected"}</p></div><div><span className="label">HUMAN DECISION</span><p className={result.human_approval?.decision === "APPROVED" ? "verified" : result.human_approval?.decision === "REJECTED" ? "failed" : "unknown"}>{result.human_approval?.decision || "PENDING"}</p><small>{result.human_approval?.decided_at || "Review before deciding"}</small></div></div>
      <div className="passport-grid"><div><span className="label">AI PATCH REVIEW</span><p>{result.ai_review?.status === "SUGGESTED" ? `${result.ai_review.findings?.length || 0} suggested finding(s)` : "Unavailable"}</p><small>{result.ai_review?.message || "AI suggestions are not execution evidence."}</small></div></div>
      {result.ai_review?.status === "SUGGESTED" && <details><summary>AI review suggestions</summary><ul>{result.ai_review.findings?.map((finding: Result, index: number) => <li key={index}><strong>{finding.severity}: {finding.title}</strong><br />{finding.explanation}</li>)}</ul></details>}
      {result.human_approval?.decision === "PENDING" && <div className="decision"><p className="disclaimer">AI suggestions are advisory. Your decision records your review and does not prove complete correctness.</p><button className="secondary" disabled={decisionBusy} onClick={() => decide("REJECTED")}>Reject patch</button><button className="primary decision-button" disabled={decisionBusy} onClick={() => decide("APPROVED")}>Approve patch</button></div>}
      {result.human_approval?.decision !== "PENDING" && <p className="disclaimer">Human decision recorded as {result.human_approval.decision}. This does not change the measured verification result or prove complete correctness.</p>}
      {mode === "engineering" && <details open><summary>Technical evidence</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
      <p className="disclaimer">Evidence labels describe what was measured. Passing tests does not prove complete correctness. AI findings are SUGGESTED until independently tested.</p>
    </section>}
    <footer><span>TRACEFORGE 2.0</span><span>Evidence over assumptions · Never modifies your original project</span></footer>
  </main>;
}
