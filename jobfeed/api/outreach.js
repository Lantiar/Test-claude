// Where each job's recruiter outreach stands, and the one place to ask for it.
//
// The page cannot do this work itself: sourcing recruiters is Apify, sending
// is Gmail, and both are Python running elsewhere. So a click does not send
// anything -- it records an intent here, and the scheduled runner picks it up,
// does the work, and writes the outcome back. The button is a request, and the
// state you see is what actually happened.
//
// Same store and the same passphrase as stages.js, deliberately: two write
// paths on a public page with two different answers to "who may write" is one
// more than anybody can keep straight.

const KEY = "jobfeed:outreach";

// Short on purpose -- these are read in a table cell.
//   queued   you asked for it; the runner has not got to it yet
//   reached  mail is out
//   replied  a recruiter answered
//   held     the pipeline refused, and `note` says why
//   failed   something broke, and `note` says what
const STATES = ["queued", "reached", "replied", "held", "failed"];

function creds() {
  const env = process.env;
  const find = (suffix) => {
    const exact = Object.keys(env).find((k) => k === suffix && env[k]);
    if (exact) return env[exact];
    const key = Object.keys(env).find((k) => k.endsWith(suffix) && env[k]);
    return key ? env[key] : undefined;
  };
  const url = find("KV_REST_API_URL") || find("UPSTASH_REDIS_REST_URL")
           || find("REDIS_REST_URL");
  const token = find("KV_REST_API_TOKEN") || find("UPSTASH_REDIS_REST_TOKEN")
             || find("REDIS_REST_TOKEN");
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
      detail: "Add Upstash Redis to this Vercel project (Storage tab) and redeploy.",
    });
  }

  try {
    if (req.method === "GET") {
      const flat = (await redis(["HGETALL", KEY])) || [];
      const outreach = {};
      for (let i = 0; i < flat.length; i += 2) {
        try { outreach[flat[i]] = JSON.parse(flat[i + 1]); }
        catch (e) { outreach[flat[i]] = { state: String(flat[i + 1]) }; }
      }
      return res.status(200).json({ outreach, count: Object.keys(outreach).length });
    }

    if (req.method === "POST") {
      const secret = process.env.JOBFEED_PASSPHRASE;
      if (!secret) {
        return res.status(503).json({
          error: "no passphrase configured",
          detail: "Set JOBFEED_PASSPHRASE in the project's environment variables.",
        });
      }
      const body = typeof req.body === "string" ? JSON.parse(req.body) : (req.body || {});
      if (!sameSecret(body.passphrase, secret)) {
        return res.status(401).json({ error: "wrong passphrase" });
      }

      const key = body.key;
      if (!key || typeof key !== "string") {
        return res.status(400).json({ error: "which job? send a key" });
      }

      // The runner reports progress through the same endpoint, with a state.
      // A browser only ever asks, and asking is the one thing it may do:
      // letting a page declare "reached" would mean the record said mail went
      // out when nothing had.
      if (body.state !== undefined) {
        if (!STATES.includes(body.state)) {
          return res.status(400).json({ error: `unknown state ${JSON.stringify(body.state)}`,
                                        expected: STATES });
        }
        const record = { state: body.state, at: Math.floor(Date.now() / 1000) };
        if (body.note) record.note = String(body.note).slice(0, 300);
        if (body.thread) record.thread = String(body.thread).slice(0, 120);
        if (body.sent) record.sent = Number(body.sent) || 0;
        await redis(["HSET", KEY, key, JSON.stringify(record)]);
        return res.status(200).json({ saved: { key, ...record } });
      }

      // A request from the page. Refused if one is already in flight or done,
      // so a double click cannot queue a second batch to the same company.
      const existing = await redis(["HGET", KEY, key]);
      if (existing) {
        let current = {};
        try { current = JSON.parse(existing); } catch (e) { current = { state: existing }; }
        if (["queued", "reached", "replied"].includes(current.state)) {
          return res.status(409).json({ error: "already asked for", current });
        }
      }
      const record = { state: "queued", at: Math.floor(Date.now() / 1000) };
      await redis(["HSET", KEY, key, JSON.stringify(record)]);
      return res.status(200).json({ saved: { key, ...record } });
    }

    if (req.method === "DELETE") {
      const secret = process.env.JOBFEED_PASSPHRASE;
      const given = (req.headers["x-passphrase"] || "");
      if (!secret || !sameSecret(given, secret)) {
        return res.status(401).json({ error: "wrong passphrase" });
      }
      const key = (req.query && req.query.key) || "";
      if (!key) return res.status(400).json({ error: "which job? send ?key=" });
      await redis(["HDEL", KEY, key]);
      return res.status(200).json({ cleared: key });
    }

    res.setHeader("Allow", "GET, POST, DELETE");
    return res.status(405).json({ error: `${req.method} not allowed here` });
  } catch (err) {
    return res.status(502).json({ error: "the store could not be reached",
                                  detail: String(err.message || err) });
  }
}
