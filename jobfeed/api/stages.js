// The central record of where each application stands.
//
// Stages used to live in each browser's localStorage, which meant there was no
// single answer to "have I applied to this": a laptop and a phone each held
// their own, and the database behind the local viewer held a third. A page can
// only write somewhere that accepts a POST, and static hosting does not, so
// this is the smallest thing that does.
//
// Stored as a Redis hash rather than one JSON blob, which removes the
// read-modify-write entirely: two devices setting two different jobs at the
// same moment touch two different fields and neither can lose the other's
// edit. A blob would have made that a race whose loser vanished silently.
//
// No dependencies and no package.json on purpose -- Upstash speaks HTTP, and
// jobfeed is standard library everywhere else.

const KEY = "jobfeed:stages";

const STAGES = ["interested", "applied", "oa", "interview", "final", "offer",
                "accepted", "rejected"];

function creds() {
  // Vercel's Upstash integration injects one pair or the other depending on
  // how the store was added. Accepting both is cheaper than telling someone
  // their correctly-connected database is missing.
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  return url && token ? { url, token } : null;
}

async function redis(command) {
  const c = creds();
  if (!c) throw new Error("no store connected");
  const r = await fetch(c.url, {
    method: "POST",
    headers: { Authorization: `Bearer ${c.token}`, "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  if (!r.ok) throw new Error(`store returned ${r.status}`);
  const body = await r.json();
  if (body.error) throw new Error(body.error);
  return body.result;
}

// Constant-time-ish compare, so a wrong passphrase cannot be found one
// character at a time by timing the response.
function sameSecret(given, expected) {
  if (typeof given !== "string" || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");

  if (!creds()) {
    return res.status(503).json({
      error: "no store connected",
      detail: "Add Upstash Redis to this Vercel project (Storage tab) and " +
              "redeploy; it injects KV_REST_API_URL and KV_REST_API_TOKEN.",
    });
  }

  try {
    if (req.method === "GET") {
      const flat = (await redis(["HGETALL", KEY])) || [];
      const stages = {};
      for (let i = 0; i < flat.length; i += 2) stages[flat[i]] = flat[i + 1];
      return res.status(200).json({ stages, count: Object.keys(stages).length });
    }

    if (req.method === "POST") {
      const secret = process.env.JOBFEED_PASSPHRASE;
      if (!secret) {
        // Refusing rather than defaulting to open: this endpoint is on a
        // public site, and an unset variable must not quietly mean "anyone".
        return res.status(503).json({
          error: "no passphrase configured",
          detail: "Set JOBFEED_PASSPHRASE in the project's environment " +
                  "variables. Writing is refused until it exists.",
        });
      }
      const body = typeof req.body === "string" ? JSON.parse(req.body) : (req.body || {});
      if (!sameSecret(body.passphrase, secret)) {
        return res.status(401).json({ error: "wrong passphrase" });
      }
      const { key, stage } = body;
      if (!key || typeof key !== "string") {
        return res.status(400).json({ error: "which job? send a key" });
      }
      if (stage && !STAGES.includes(stage)) {
        return res.status(400).json({
          error: `unknown stage ${JSON.stringify(stage)}`,
          expected: STAGES,
        });
      }
      if (stage) await redis(["HSET", KEY, key, stage]);
      else await redis(["HDEL", KEY, key]);
      return res.status(200).json({ saved: { key, stage: stage || "" } });
    }

    res.setHeader("Allow", "GET, POST");
    return res.status(405).json({ error: `${req.method} not allowed here` });
  } catch (err) {
    // Said plainly. A tracker that silently fails to save a stage is worse
    // than one that refuses, because you find out weeks later.
    return res.status(502).json({ error: "the store could not be reached",
                                  detail: String(err.message || err) });
  }
}
