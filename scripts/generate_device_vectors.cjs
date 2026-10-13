#!/usr/bin/env node
/**
 * Produces tests/fixtures/device_envelope_vectors.json: the `[[SALT-DEVICE ...]]`
 * envelope and the device-task shapes, written by the REAL compiled TypeScript SDK
 * (salt-agent-sdk `src/device/protocol.ts`), so the Python port (`saltapp.device`)
 * can be held to it byte for byte.
 *
 * The same file is committed, unchanged, in salt-agent-sdk
 * (tests/fixtures/device_envelope_vectors.json) and both suites run it:
 *   - TypeScript: encodeDeviceMessage(header, body) must equal `text`; decode must
 *     give back header/body; every normalisation vector must give its `expect`.
 *   - Python: the same, in saltapp.device.
 * So an envelope encoded in TS decodes in Python to the same header and body, and an
 * envelope encoded in Python is the same bytes TS wrote.
 *
 * Run from this repo, after `npm run build` in the TS SDK:
 *   SALT_AGENT_SDK_DIR=../salt-agent-sdk node scripts/generate_device_vectors.cjs
 * (default SALT_AGENT_SDK_DIR is the sibling ../salt-agent-sdk), then copy the
 * file into salt-agent-sdk/tests/fixtures/. Fixed strings only: no clock, no random.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const SDK_DIR = path.resolve(process.env.SALT_AGENT_SDK_DIR || path.join(__dirname, "..", "..", "salt-agent-sdk"));
const sdk = require(path.join(SDK_DIR, "dist", "index.js"));

const SESSION = "0b6c2a3e-4f5d-4e7a-9a11-2c3d4e5f6a7b";
const hdr = (o) => ({ v: 1, id: "01J9ZQ3M8K0000000000000000", seq: 1, session: SESSION, ...o });
const rep = (s, n) => s.repeat(n);

// ---- encode / decode vectors -------------------------------------------------
const envelopes = [
  ["observe: marker alone, no body", hdr({ op: "observe", exp: 1760000030000 }), undefined],
  ["click: a body and an exp", hdr({ op: "click", seq: 4, exp: 1760000030000 }), { x: 10, y: 20, button: "left" }],
  ["task: every argument", hdr({ op: "task", seq: 2, exp: 1760000030000 }), { goal: "Open saltapp.ai in Firefox", done_when: "the page title says saltapp.ai", max_steps: 20, timeout_s: 120 }],
  ["task: goal only", hdr({ op: "task", seq: 3, exp: 1760000030000 }), { goal: "Empty the Downloads folder" }],
  ["cancel: names a running task in re and in the body", hdr({ op: "cancel", seq: 3, id: "01J9ZQ3M8K1111111111111111", re: "01J9ZQ3M8K0000000000000000" }), { id: "01J9ZQ3M8K0000000000000000" }],
  ["progress: a non-final result with the task's re", hdr({ op: "progress", id: "01J9ZQ3M8K2222222222222222", seq: 7, re: "01J9ZQ3M8K0000000000000000" }), { step: 2, of: 5, line: "Opening Firefox", waiting: false }],
  [
    "task_result: a checked receipt",
    hdr({ op: "task_result", id: "01J9ZQ3M8K3333333333333333", seq: 9, re: "01J9ZQ3M8K0000000000000000" }),
    {
      task_id: "t_0a1b2c3d4e5f",
      status: "done",
      summary: "Done. Results for saltapp.ai are open in Firefox.",
      checked: { ok: true, what: "the page title says saltapp.ai" },
      steps: [
        { n: 1, kind: "apps", line: "Focus Firefox", ok: true },
        { n: 2, kind: "command", line: "open https://saltapp.ai", ok: true },
        { n: 3, kind: "check", line: "Read the page title", ok: true },
      ],
      commands: 1,
      duration_ms: 8420,
    },
  ],
  ["error: refusal with a reason", hdr({ op: "error", id: "01J9ZQ3M8K4444444444444444", seq: 5, re: "01J9ZQ3M8K0000000000000000" }), { code: "out_of_scope", message: "action budget spent" }],
  ["ended: the session is over", hdr({ op: "ended", id: "01J9ZQ3M8K5555555555555555", seq: 11 }), { reason: "stopped_by_agent" }],
  [
    "body: escapes, quotes, non-ASCII and an emoji survive byte for byte",
    hdr({ op: "task", seq: 6, exp: 1760000030000 }),
    { goal: "Say \"hi\" to Zoë — it’s a “test”\nline two\ttab \\ backslash \u0001 control 😀 end", done_when: "ok" },
  ],
].map(([name, header, body]) => {
  const text = sdk.encodeDeviceMessage(header, body);
  const decoded = sdk.decodeDeviceMessage(text);
  const entry = { name, header, text, decoded_header: decoded.header };
  if (body !== undefined) entry.body = body;
  if (decoded.body !== undefined) entry.decoded_body = decoded.body;
  return entry;
});

// ---- decode-only vectors (text a peer might send) -----------------------------
const M = (fields, rest = "") => `[[SALT-DEVICE ${fields}]]${rest}`;
const BASE = `v=1 id=abc seq=3 op=task session=${SESSION}`;
const decodeOnly = [
  ["leading whitespace and a trailing newline are tolerated", `  \n${M(BASE + " exp=1760000030000", '\n{"goal":"x"}\n')}`],
  ["blank lines between the marker and the body", M(BASE, '\n\n\n{"goal":"x"}')],
  ["not a device message at all", "hello, how are you?"],
  ["a marker that is not at the start is chat text", `see ${M(BASE)}`],
  ["unknown op passes through (the host answers unsupported)", M(`v=1 id=abc seq=1 op=teleport session=${SESSION}`)],
  ["another protocol version still decodes (the host answers unsupported)", M(`v=2 id=abc seq=1 op=observe session=${SESSION}`)],
  ["no closing brackets", `[[SALT-DEVICE ${BASE}`],
  ["missing a required field", M(`v=1 id=abc seq=1 session=${SESSION}`)],
  ["seq 0 is refused", M(`v=1 id=abc seq=0 op=observe session=${SESSION}`)],
  ["seq must be a number", M(`v=1 id=abc seq=x op=observe session=${SESSION}`)],
  ["exp must be a number", M(`${BASE} exp=soon`)],
  ["a body that is not JSON", M(BASE, "\n{goal: x}")],
  ["two spaces between fields do not match the grammar", M(`v=1  id=abc seq=1 op=observe session=${SESSION}`)],
].map(([name, text]) => {
  try {
    const m = sdk.decodeDeviceMessage(text);
    if (m === null) return { name, text, expect: null };
    const expect = { header: m.header };
    if (m.body !== undefined) expect.body = m.body;
    return { name, text, expect };
  } catch (e) {
    return { name, text, expect_error: e.code };
  }
});

// ---- task arguments -----------------------------------------------------------
const taskArgsIn = [
  ["goal only gets the defaults", { goal: "Open the page" }],
  ["unknown keys are dropped", { goal: "g", extra: 1, shell: "rm -rf /" }],
  ["numbers are clamped into range", { goal: "g", done_when: "it is open", max_steps: 500, timeout_s: 1 }],
  ["numbers clamped from below and above", { goal: "g", max_steps: 0, timeout_s: 9999 }],
  ["an empty done_when is no done_when", { goal: "g", done_when: "" }],
  ["null optional fields are absent", { goal: "g", done_when: null, max_steps: null, timeout_s: null }],
  ["a goal of exactly 2000 characters", { goal: rep("a", 2000) }],
  ["2000 emoji are 2000 characters, not 4000", { goal: rep("😀", 2000) }],
  ["2001 emoji are over", { goal: rep("😀", 2001) }],
  ["2001 characters are over", { goal: rep("a", 2001) }],
  ["done_when of exactly 500", { goal: "g", done_when: rep("é", 500) }],
  ["done_when over 500", { goal: "g", done_when: rep("é", 501) }],
  ["no goal", {}],
  ["empty goal", { goal: "" }],
  ["blank goal", { goal: "  \n " }],
  ["goal not a string", { goal: 5 }],
  ["done_when not a string", { goal: "g", done_when: 4 }],
  ["max_steps not whole", { goal: "g", max_steps: 1.5 }],
  ["timeout_s a string", { goal: "g", timeout_s: "30" }],
  ["body is a list", ["goal"]],
  ["body is a string", "goal"],
].map(([name, input]) => {
  try {
    return { name, input, expect: sdk.parseTaskArgs(input) };
  } catch (e) {
    return { name, input, expect_error: e.code };
  }
});

// ---- progress -----------------------------------------------------------------
const progressIn = [
  ["every field", { step: 2, of: 5, line: "Opening Firefox", waiting: true }],
  ["only what is allowed gets through", { step: 1, line: "x", secret: "no", of: 3 }],
  ["a long line is cut to 140 characters", { step: 1, line: rep("y", 300) }],
  ["a long line of emoji is cut by characters", { step: 1, line: rep("😀", 300) }],
  ["of below 1 is dropped, waiting must be a boolean", { step: 0, line: "start", of: 0, waiting: "yes" }],
  ["step is not a number", { step: "1", line: "a" }],
  ["step is not whole", { step: 1.5, line: "a" }],
  ["step is negative", { step: -1, line: "a" }],
  ["no line", { step: 1 }],
  ["no step", { line: "a" }],
  ["not an object", "progress"],
  ["null", null],
].map(([name, input]) => ({ name, input, expect: sdk.normalizeProgress(input) }));

// ---- task_result --------------------------------------------------------------
const stepRows = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({ n: i + 1, kind: "command", line: "step " + (i + 1), ok: i % 2 === 0, ...extra }));
const resultIn = [
  ["a clean result passes through unchanged", { task_id: "t_000000000001", status: "done", summary: "Done.", checked: { ok: true, what: "title ok" }, steps: [{ n: 1, kind: "check", line: "Read the title", ok: true }], commands: 1, duration_ms: 1200 }],
  [
    "extra fields never travel (output, screenshots, anything else)",
    { task_id: "t_000000000002", status: "failed", summary: "Could not.", checked: { ok: false, what: "no", screenshot_b64: "AAAA" }, steps: [{ n: 1, kind: "command", line: "ls", ok: false, output: "SECRET OUTPUT" }], commands: 1, duration_ms: 5, output: "rm -rf", image_b64: "AAAA" },
  ],
  ["summary 500, what 200, task_id 64 are the caps", { task_id: rep("t", 100), status: "done", summary: rep("s", 900), checked: { ok: true, what: rep("w", 400) }, steps: [], commands: 0, duration_ms: 0 }],
  ["caps count characters: emoji are cut by code point", { task_id: "t_000000000003", status: "done", summary: rep("😀", 700), checked: { ok: true, what: rep("é", 250) }, steps: [{ n: 1, kind: "look", line: rep("😀", 200), ok: true }], commands: 0, duration_ms: 0 }],
  ["80 steps keep the first 60", { task_id: "t_000000000004", status: "done", summary: "many", checked: null, steps: stepRows(80), commands: 80, duration_ms: 9 }],
  [
    "bad step rows are dropped, a missing n is its place in the list",
    { task_id: "t_000000000005", status: "done", summary: "s", checked: null, steps: [{ n: 1, kind: "command", line: "ok", ok: true }, { n: 2, kind: "shell", line: "bad kind", ok: true }, "not a row", { n: 3, kind: "look", ok: true }, { kind: "ask", line: "no n", ok: "yes" }, { n: -4, kind: "act", line: "negative n", ok: true }], commands: 1, duration_ms: 1 },
  ],
  ["an unknown status reads failed", { task_id: "t_000000000006", status: "great", summary: "s", checked: null, steps: [], commands: 0, duration_ms: 0 }],
  ["every status is allowed", { task_id: "t_000000000007", status: "paused_out", summary: "s", checked: null, steps: [], commands: 0, duration_ms: 0 }],
  ["bad types fall back: no summary, no checked, no steps, no counts", { task_id: "t_000000000008", status: "done", summary: 5, checked: { ok: "yes", what: "x" }, steps: "no", commands: -4, duration_ms: 1.5 }],
  ["checked.ok must be a boolean; what may be missing", { task_id: "t_000000000009", status: "done", summary: "s", checked: { ok: true }, steps: [], commands: 3, duration_ms: 77 }],
  ["whole numbers: 2.0 is whole, 2.5 is not", { task_id: "t_00000000000a", status: "done", summary: "s", checked: null, steps: [], commands: 2.0, duration_ms: 2.5 }],
].map(([name, input]) => ({ name, input, expect: sdk.normalizeTaskResult(input) }));

const out = {
  generated_by: "scripts/generate_device_vectors.cjs (saltapp-python), from salt-agent-sdk src/device/protocol.ts",
  note: "Committed identically in salt-agent-sdk and saltapp-python. Do not edit by hand: regenerate.",
  session: SESSION,
  envelopes,
  decode_only: decodeOnly,
  task_args: taskArgsIn,
  progress: progressIn,
  task_result: resultIn,
  constants: {
    DEVICE_PROTOCOL_VERSION: sdk.DEVICE_PROTOCOL_VERSION,
    MAX_DEVICE_PLAINTEXT_BYTES: sdk.MAX_DEVICE_PLAINTEXT_BYTES,
    MAX_COMMANDS_PER_SECOND: sdk.MAX_COMMANDS_PER_SECOND,
    MAX_OBSERVE_PER_SECOND: sdk.MAX_OBSERVE_PER_SECOND,
    COMMAND_TTL_MS: sdk.COMMAND_TTL_MS,
    RESULT_CACHE_SIZE: sdk.RESULT_CACHE_SIZE,
    MUTATING_OPS: [...sdk.MUTATING_OPS],
    AGENT_OPS: [...sdk.AGENT_OPS],
    DEVICE_RESULT_OPS: [...sdk.DEVICE_RESULT_OPS],
    DEVICE_ERROR_CODES: [...sdk.DEVICE_ERROR_CODES],
    DEVICE_COUNT_CLASSES: [...sdk.DEVICE_COUNT_CLASSES],
    CAPABILITY_FOR_OP: { ...sdk.CAPABILITY_FOR_OP },
    TASK_GOAL_MAX: sdk.TASK_GOAL_MAX,
    TASK_DONE_WHEN_MAX: sdk.TASK_DONE_WHEN_MAX,
    TASK_MAX_STEPS_MIN: sdk.TASK_MAX_STEPS_MIN,
    TASK_MAX_STEPS_MAX: sdk.TASK_MAX_STEPS_MAX,
    TASK_MAX_STEPS_DEFAULT: sdk.TASK_MAX_STEPS_DEFAULT,
    TASK_TIMEOUT_S_MIN: sdk.TASK_TIMEOUT_S_MIN,
    TASK_TIMEOUT_S_MAX: sdk.TASK_TIMEOUT_S_MAX,
    TASK_TIMEOUT_S_DEFAULT: sdk.TASK_TIMEOUT_S_DEFAULT,
    PROGRESS_LINE_MAX: sdk.PROGRESS_LINE_MAX,
    TASK_SUMMARY_MAX: sdk.TASK_SUMMARY_MAX,
    TASK_STEP_LINE_MAX: sdk.TASK_STEP_LINE_MAX,
    TASK_RESULT_STEPS_MAX: sdk.TASK_RESULT_STEPS_MAX,
    TASK_CHECKED_WHAT_MAX: sdk.TASK_CHECKED_WHAT_MAX,
    TASK_ID_MAX: sdk.TASK_ID_MAX,
    PROGRESS_MIN_INTERVAL_MS: sdk.PROGRESS_MIN_INTERVAL_MS,
    TASK_DEVICE_GRACE_MS: sdk.TASK_DEVICE_GRACE_MS,
    TASK_CLIENT_GRACE_MS: sdk.TASK_CLIENT_GRACE_MS,
    TASK_STATUSES: [...sdk.TASK_STATUSES],
    TASK_STEP_KINDS: [...sdk.TASK_STEP_KINDS],
  },
};

const dest = path.join(__dirname, "..", "tests", "fixtures", "device_envelope_vectors.json");
fs.writeFileSync(dest, JSON.stringify(out, null, 2) + "\n");
console.log(`wrote ${dest}: ${envelopes.length} envelopes, ${decodeOnly.length} decode-only, ${taskArgsIn.length} task args, ${progressIn.length} progress, ${resultIn.length} task results`);
