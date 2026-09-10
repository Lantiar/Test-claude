// Where each job's recruiter outreach stands, and the one place to ask for it.
//
// The page cannot do this work itself: sourcing recruiters is Apify, sending
// is Gmail, and both are Python running elsewhere. So a click does not send
// anything -- it records an intent here, the runner picks it up, does the work,
// and writes the outcome back. The button is a request, and the state you see
// is what actually happened. Given a GitHub token the request also wakes the
// runner so that happens in a minute rather than at the next half hour.
//
// Same store and the same passphrase as stages.js, deliberately: two write
// paths on a public page with two different answers to "who may write" is one
// more than anybody can keep straight.

const KEY = "jobfeed:outreach";
const STATE_KEY = "jobfeed:outreach:state";
// Addresses found without writing a letter. Its records name real people,
// so unlike the outreach hash it is only ever served behind the passphrase.
const FIND_KEY = "jobfeed:outreach:finds";
const CMD_KEY = "jobfeed:outreach:cmds";
const PROFILE_KEY = "jobfeed:outreach:profile";
const RESUME_KEY = "jobfeed:outreach:resume";

// Roughly a megabyte of PDF once base64 has added a third. Bigger than any
// resume needs to be, and small enough that the store stays quick.
const MAX_RESUME = 1_400_000;

// What a browser may ask the runner to do. Everything here is a request that
// the runner applies against its own database on the next pass -- the page
// never edits outreach directly, because the page is not what sends.
const ACTIONS = ["cancel", "reschedule", "send_now", "retry", "edit"];

// Short on purpose -- these are read in a table cell.
//   queued   you asked for it; the runner has not got to it yet
//   reached  mail is out
//   replied  a recruiter answered
//   held     the pipeline refused, and `note` says why
//   failed   something broke, and `note` says what
const STATES = ["queued", "reached", "replied", "held", "failed"];

// Nothing watches the store between scheduled runs, so a request written here
// would sit until the next half hour. This wakes the runner instead: the work
// still happens there, on the machine that holds the API keys, but it happens
// now. Optional -- without a token the request simply waits, which is what it
// did before this existed.
const GH_TOKEN = process.env.GH_PAT || process.env.GITHUB_DISPATCH_TOKEN;
const GH_REPO = process.env.GITHUB_REPO || "Lantiar/Test-claude";
const GH_REF = process.env.GITHUB_REF || "claude/plan-reasoning-verification-7i3zz7";

async function wakeRunner() {
  if (!GH_TOKEN) return "waiting for the next run";
  const url = `https://api.github.com/repos/${GH_REPO}` +
              `/actions/workflows/outreach-now.yml/dispatches`;
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jobfeed",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: GH_REF }),
    });
    // 204 is the only success GitHub returns here. Anything else is reported
    // rather than swallowed: a button that says "running now" over a dispatch
    // the API refused is the failure this whole project keeps guarding
    // against -- the request is still safely recorded either way.
    return r.status === 204 ? "running now"
                            : `waiting for the next run (dispatch ${r.status})`;
  } catch (e) {
    return "waiting for the next run (dispatch unreachable)";
  }
}

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
      // The detailed view carries recruiters' names and addresses. This page
      // is on a public URL beside a public job feed, so that half is behind
      // the passphrase -- the coarse per-job state, which names nobody, is
      // not, because the buttons on the job board need it.
      if (req.query && req.query.detail) {
        const secret = process.env.JOBFEED_PASSPHRASE;
        if (!secret || !sameSecret(req.headers["x-passphrase"] || "", secret)) {
          return res.status(401).json({ error: "wrong passphrase" });
        }
        const [blob, cmds, prof, foundFlat] = await Promise.all([
          redis(["GET", STATE_KEY]),
          redis(["HGETALL", CMD_KEY]),
          redis(["GET", PROFILE_KEY]),
          redis(["HGETALL", FIND_KEY]),
        ]);
        const finds = {};
        for (let i = 0; i < (foundFlat || []).length; i += 2) {
          try { finds[foundFlat[i]] = JSON.parse(foundFlat[i + 1]); } catch (e) {}
        }
        const pending = [];
        for (let i = 0; i < (cmds || []).length; i += 2) {
          try { pending.push({ id: cmds[i], ...JSON.parse(cmds[i + 1]) }); } catch (e) {}
        }
        let state = {};
        try { state = blob ? JSON.parse(blob) : {}; } catch (e) { state = {}; }
        let settings = {};
        try { settings = prof ? JSON.parse(prof) : {}; } catch (e) { settings = {}; }
        return res.status(200).json({ state, pending, settings, finds });
      }
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

      // The settings the runner applies before it renders anything.
      if (body.settings) {
        const s = body.settings;
        const keep = {};
        for (const k of ["grad", "portfolio", "linkedin", "gpa", "honors"]) {
          if (typeof s[k] === "string" && s[k].trim()) keep[k] = s[k].trim().slice(0, 200);
        }
        if (Array.isArray(s.wins)) {
          keep.wins = s.wins
            .filter((w) => Array.isArray(w) && String(w[0]).trim() && String(w[1]).trim())
            .slice(0, 6)
            .map((w) => [String(w[0]).trim().slice(0, 80), String(w[1]).trim().slice(0, 600)]);
        }
        if (s.attach_resume !== undefined) keep.attach_resume = !!s.attach_resume;
        if (typeof s.resume_name === "string" && s.resume_name.trim()) {
          keep.resume_name = s.resume_name.trim().slice(0, 120);
        }
        await redis(["SET", PROFILE_KEY, JSON.stringify(keep)]);
        return res.status(200).json({ saved: keep });
      }

      // A replacement resume, as base64. Stored rather than pointed at: the
      // runner has no filesystem that outlives a run, so a path set here
      // would mean nothing to it and the attachment would quietly stop.
      if (body.resume) {
        const data = String(body.resume.data || "");
        if (data.length > MAX_RESUME) {
          return res.status(413).json({ error: "that PDF is too large (about 1MB max)" });
        }
        // "%PDF" base64-encodes to this prefix whatever follows it.
        if (!data.startsWith("JVBERi")) {
          return res.status(400).json({ error: "that does not look like a PDF" });
        }
        await redis(["SET", RESUME_KEY, JSON.stringify({
          name: String(body.resume.name || "resume.pdf").slice(0, 120),
          data, at: Math.floor(Date.now() / 1000),
        })]);
        return res.status(200).json({ saved: { resume: body.resume.name, bytes: data.length } });
      }

      // An instruction for the runner: cancel a draft, move it, send it now.
      // Queued rather than applied, because the database it applies to is on
      // the runner and is rebuilt from the store on every pass.
      if (body.action) {
        if (!ACTIONS.includes(body.action)) {
          return res.status(400).json({ error: `unknown action ${JSON.stringify(body.action)}`,
                                        expected: ACTIONS });
        }
        if (!body.email) {
          return res.status(400).json({ error: "which recruiter? send an email" });
        }
        const cmd = { action: body.action, email: String(body.email).slice(0, 200),
                      at: Math.floor(Date.now() / 1000) };
        if (body.job_key) cmd.job_key = String(body.job_key).slice(0, 400);
        if (body.step !== undefined) cmd.step = Number(body.step) || 0;
        if (body.action === "edit") {
          const subject = String(body.subject ?? "").trim();
          const text = String(body.body ?? "");
          if (!subject || !text.trim()) {
            return res.status(400).json({ error: "an edit needs a subject and a body" });
          }
          // Generous, but bounded: this ends up in a Redis value the runner
          // reads on every pass, and an accidental paste of something huge
          // should be refused here rather than wedging the store.
          if (subject.length > 400 || text.length > 20000) {
            return res.status(400).json({ error: "that is too long to send" });
          }
          cmd.subject = subject;
          cmd.body = text;
        }
        if (body.action === "reschedule") {
          const when = Number(body.when);
          if (!when || !isFinite(when)) {
            return res.status(400).json({ error: "reschedule needs a `when` timestamp" });
          }
          cmd.when = Math.floor(when);
        }
        const id = `${cmd.at}-${Math.random().toString(36).slice(2, 8)}`;
        await redis(["HSET", CMD_KEY, id, JSON.stringify(cmd)]);
        return res.status(200).json({ queued: { id, ...cmd } });
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

      // Addresses only: no draft, no send. The page asks; the runner writes
      // the answer straight to the store, so this end only ever takes the ask.
      if (body.find) {
        await redis(["HSET", FIND_KEY, key, JSON.stringify(
          { state: "asked", at: Math.floor(Date.now() / 1000) })]);
        return res.status(200).json({ saved: { key, state: "asked" },
                                      runner: await wakeRunner() });
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
      return res.status(200).json({ saved: { key, ...record },
                                    runner: await wakeRunner() });
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
      await redis(["HDEL", FIND_KEY, key]);
      return res.status(200).json({ cleared: key });
    }

    res.setHeader("Allow", "GET, POST, DELETE");
    return res.status(405).json({ error: `${req.method} not allowed here` });
  } catch (err) {
    return res.status(502).json({ error: "the store could not be reached",
                                  detail: String(err.message || err) });
  }
}
