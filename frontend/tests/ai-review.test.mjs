import { readFile } from "node:fs/promises";
import test from "node:test";
import assert from "node:assert/strict";

test("renders optional AI review without treating it as verification evidence", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");

  assert.match(source, /result\.ai_review\?\.status === "SUGGESTED"/);
  assert.match(source, /AI findings are SUGGESTED until independently tested/);
  assert.match(source, /result\.verification\?\.status/);
  assert.match(source, /Approve patch/);
  assert.match(source, /Reject patch/);
  assert.match(source, /\/api\/approval/);
  assert.match(source, /verification_id: result\.verification_id/);
  assert.match(source, /X-Traceforge-Approval-Token/);
  assert.match(source, /X-Traceforge-Approval-Token/);
  assert.doesNotMatch(source, /decision, token: approvalToken/);
  assert.match(source, /decide\("APPROVED"\)/);
  assert.match(source, /decide\("REJECTED"\)/);
});
