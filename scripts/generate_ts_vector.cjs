#!/usr/bin/env node
/**
 * Produces tests/fixtures/webhook_signature_vector.json: a set of
 * X-Salt-Signature / body pairs, PROVEN byte-compatible with the real
 * TypeScript SDK by feeding them through salt-agent-sdk's own compiled
 * `createWebhookServer` (dist/webhook.js) and recording whether IT accepted
 * or rejected each one (its own HTTP status code, 200 vs 401 -- see
 * webhook.ts's app.post("/") handler, which responds before running any
 * decrypt/dispatch logic).
 *
 * Each vector also carries `now_unix`, the wall-clock second this script
 * captured right when it built that vector's signature. The Python side
 * (tests/test_webhook_vector.py) replays verification pinned to that exact
 * moment via `verify_signature(..., now=vector["now_unix"])`, rather than
 * the real wall clock -- a "fresh" (t=now) signature is only fresh for the
 * 300s tolerance window, so without pinning, this fixture would start
 * failing on its own five minutes after being committed.
 *
 * Run once from this repo:
 *   node scripts/generate_ts_vector.cjs
 *
 * Requires ../salt-agent-sdk to be built (dist/ present) and its
 * node_modules installed (both already true in this workspace).
 */
"use strict";

const http = require("node:http");
const crypto = require("node:crypto");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");

const SDK_DIR = path.join(__dirname, "..", "..", "salt-agent-sdk");
const { createWebhookServer } = require(path.join(SDK_DIR, "dist", "webhook.js"));

const SECRET = "vector-secret-do-not-use-in-prod";
const AGENT_ID = "11111111-1111-1111-1111-111111111111";
const BODY = JSON.stringify({
  message: {
    message_id: 424242,
    chat_id: "22222222-2222-2222-2222-222222222222",
    message: "-----BEGIN PGP MESSAGE-----\nnot a real ciphertext, only signature verification is under test\n-----END PGP MESSAGE-----",
    user: { id: "33333333-3333-3333-3333-333333333333", display_name: "Vector Sender", account_type: "User" },
  },
  chat: {},
});

function sign(secret, t, raw) {
  return crypto.createHmac("sha256", secret).update(`${t}.${raw}`).digest("hex");
}

// Minimal stand-ins for the two collaborators createWebhookServer needs.
// Only getWebhookSecret is ever called on this path (signature check runs
// before any decrypt/dispatch), so everything else can throw if touched.
function stubClient(secret) {
  return {
    async getWebhookSecret() {
      return secret;
    },
    getChatMembers() { throw new Error("not stubbed"); },
    getChatMessages() { throw new Error("not stubbed"); },
    postMessage() { throw new Error("not stubbed"); },
    signalTyping() { throw new Error("not stubbed"); },
    trackEvent() {},
    whoAmI: async () => ({ agent_id: AGENT_ID, webhook_secret: secret }),
  };
}

function stubIdentities() {
  const identity = {
    saltAppId: AGENT_ID,
    username: "vector_agent",
    apiKey: "vector-api-key",
    // Never actually used to decrypt in this script (verification happens
    // before dispatch, and dispatch's decrypt failure is swallowed and
    // logged) -- a syntactically-armored placeholder is enough.
    publicKey: "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n-----END PGP PUBLIC KEY BLOCK-----",
    privateKey: "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n-----END PGP PRIVATE KEY BLOCK-----",
  };
  const map = new Map([[AGENT_ID.toLowerCase(), identity]]);
  return {
    register(i) { map.set(String(i.saltAppId).toLowerCase(), i); },
    get(id) { return map.get(String(id).toLowerCase()); },
    all() { return Array.from(map.values()); },
    reassignId() { return undefined; },
  };
}

async function postTo(server, headers, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { hostname: "127.0.0.1", port: server.address().port, path: "/", method: "POST", headers: { "Content-Type": "application/json", ...headers } },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode, body: data }));
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

async function main() {
  const { app } = createWebhookServer({
    client: stubClient(SECRET),
    identities: stubIdentities(),
    pgpPassphrase: "unused",
    verifySignatures: true,
    signatureToleranceSeconds: 300,
    logger: { info() {}, error() {} }, // keep the fixture generator's own output quiet
  });

  const server = app.listen(0);
  await new Promise((resolve) => server.once("listening", resolve));

  const vectors = [];

  // 1. Valid signature, fresh timestamp -> accepted (200).
  {
    const nowAtGeneration = Math.floor(Date.now() / 1000);
    const t = nowAtGeneration;
    const v1 = sign(SECRET, t, BODY);
    const header = `t=${t},v1=${v1}`;
    const res = await postTo(server, { "X-Salt-Agent-Id": AGENT_ID, "X-Salt-Signature": header }, BODY);
    vectors.push({
      name: "valid_fresh",
      description: "Correct HMAC, timestamp now -- salt-agent-sdk accepts it.",
      secret: SECRET,
      agent_id: AGENT_ID,
      signature_header: header,
      body: BODY,
      now_unix: nowAtGeneration,
      ts_sdk_status: res.status,
      expect_valid: true,
    });
  }

  // 2. Tampered digest -> rejected (401).
  {
    const nowAtGeneration = Math.floor(Date.now() / 1000);
    const t = nowAtGeneration;
    const v1 = sign(SECRET, t, BODY);
    const tampered = v1.slice(0, -2) + (v1.slice(-2) === "00" ? "11" : "00");
    const header = `t=${t},v1=${tampered}`;
    const res = await postTo(server, { "X-Salt-Agent-Id": AGENT_ID, "X-Salt-Signature": header }, BODY);
    vectors.push({
      name: "tampered_digest",
      description: "Digest bytes flipped -- salt-agent-sdk rejects it.",
      secret: SECRET,
      agent_id: AGENT_ID,
      signature_header: header,
      body: BODY,
      now_unix: nowAtGeneration,
      ts_sdk_status: res.status,
      expect_valid: false,
    });
  }

  // 3. Stale timestamp (well past the 300s tolerance) -> rejected (401).
  {
    const nowAtGeneration = Math.floor(Date.now() / 1000);
    const t = nowAtGeneration - 3600;
    const v1 = sign(SECRET, t, BODY);
    const header = `t=${t},v1=${v1}`;
    const res = await postTo(server, { "X-Salt-Agent-Id": AGENT_ID, "X-Salt-Signature": header }, BODY);
    vectors.push({
      name: "stale_timestamp",
      description: "Correct digest for its own timestamp, but 3600s old -- past the 300s replay window.",
      secret: SECRET,
      agent_id: AGENT_ID,
      signature_header: header,
      body: BODY,
      now_unix: nowAtGeneration,
      ts_sdk_status: res.status,
      expect_valid: false,
    });
  }

  // 4. Wrong secret -> rejected (401).
  {
    const nowAtGeneration = Math.floor(Date.now() / 1000);
    const t = nowAtGeneration;
    const v1 = sign("a-completely-different-secret", t, BODY);
    const header = `t=${t},v1=${v1}`;
    const res = await postTo(server, { "X-Salt-Agent-Id": AGENT_ID, "X-Salt-Signature": header }, BODY);
    vectors.push({
      name: "wrong_secret",
      description: "Digest computed with a different secret than the one salt-agent-sdk fetches for this agent.",
      secret: SECRET,
      agent_id: AGENT_ID,
      signature_header: header,
      body: BODY,
      now_unix: nowAtGeneration,
      ts_sdk_status: res.status,
      expect_valid: false,
    });
  }

  // 5. Missing signature header entirely -> rejected (401).
  {
    const nowAtGeneration = Math.floor(Date.now() / 1000);
    const res = await postTo(server, { "X-Salt-Agent-Id": AGENT_ID }, BODY);
    vectors.push({
      name: "missing_signature",
      description: "No X-Salt-Signature header at all.",
      secret: SECRET,
      agent_id: AGENT_ID,
      signature_header: null,
      body: BODY,
      now_unix: nowAtGeneration,
      ts_sdk_status: res.status,
      expect_valid: false,
    });
  }

  server.close();

  const failed = vectors.filter((v) => (v.ts_sdk_status === 200) !== v.expect_valid);
  if (failed.length) {
    console.error("Vector generation produced a result that disagrees with its own expectation:", failed);
    process.exit(1);
  }

  const out = {
    generated_by: "salt-agent-sdk dist/webhook.js (createWebhookServer), via saltapp-python/scripts/generate_ts_vector.cjs",
    generated_at: new Date().toISOString(),
    sdk_version: require(path.join(SDK_DIR, "package.json")).version,
    algorithm: "HMAC-SHA256(secret, `${t}.${raw_body}`) -> hex, header `X-Salt-Signature: t=<t>,v1=<hex>`",
    tolerance_seconds: 300,
    vectors,
  };

  const outPath = path.join(__dirname, "..", "tests", "fixtures", "webhook_signature_vector.json");
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2) + "\n");
  console.log(`Wrote ${outPath} (${vectors.length} vectors, all agreed with the real TS SDK).`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
