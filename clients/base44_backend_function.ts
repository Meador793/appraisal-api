/**
 * Base44 backend function — proxy to the Appraisal Adjustment API.
 *
 * WHERE THIS GOES
 *   Base44 Dashboard -> Code -> Functions -> new function named "appraise"
 *   (Backend functions require a Builder plan or higher.)
 *
 * WHY A BACKEND FUNCTION AND NOT A DIRECT CALL FROM THE PAGE
 *   Anything in your React frontend ships to the user's browser. An API key in
 *   frontend code is not hidden by minification, by an environment variable, or
 *   by being "only used in a fetch call" -- open devtools, Network tab, read the
 *   request header. Whoever finds it can then run your ECS task on your bill
 *   until you rotate the key.
 *
 *   The backend function runs on Base44's servers. The browser calls the
 *   function; the function calls your API with the key. The key never leaves
 *   the server.
 *
 * SECRETS TO CREATE
 *   Base44 Dashboard -> Code -> Secrets
 *     APPRAISAL_API_URL   http://<your-ecs-public-ip>:8000   (or your domain)
 *     APPRAISAL_API_KEY   the key you generated for Base44
 *
 *   Give Base44 its OWN key, not the one you use from Postman or a script. The
 *   API accepts a comma-separated list, and separate keys mean you can revoke
 *   this one without breaking everything else.
 */

Deno.serve(async (req) => {
  // ---- CORS preflight (Base44's frontend calling this function) -----------
  if (req.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    });
  }

  if (req.method !== "POST") {
    return json({ error: "POST only" }, 405);
  }

  const API_URL = Deno.env.get("APPRAISAL_API_URL");
  const API_KEY = Deno.env.get("APPRAISAL_API_KEY");

  if (!API_URL || !API_KEY) {
    return json({ error: "APPRAISAL_API_URL / APPRAISAL_API_KEY secrets are not set" }, 500);
  }

  let body;
  try {
    body = await req.json();
  } catch {
    return json({ error: "Body must be JSON" }, 400);
  }

  // "json" for the data, "pdf" for the downloadable report
  const format = body.format === "pdf" ? "pdf" : "json";
  const endpoint = format === "pdf" ? "/report" : "/predict";

  // Only forward the fields the API knows about. Passing the whole request
  // body through means any junk your frontend adds becomes a 422 from the API,
  // and the error surfaces to the user as a confusing validation failure.
  const payload: Record<string, unknown> = {
    subject: body.subject,
    include_grid: body.include_grid ?? true,
    include_percent_grid: body.include_percent_grid ?? true,
    include_contributions: body.include_contributions ?? true,
  };
  if (body.scenarios) payload.scenarios = body.scenarios;
  if (format === "pdf") {
    payload.report_title = body.report_title ?? "Market-Derived Adjustment Analysis";
    payload.delivery = "attachment";
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${API_URL}${endpoint}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,          // <-- the key, added server-side only
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(30_000),
    });
  } catch (err) {
    // A scaled-to-zero ECS service looks exactly like this. Say so, because
    // "failed to fetch" sends people hunting for a bug that isn't there.
    return json({
      error: "Could not reach the appraisal API.",
      hint: "If you scaled the ECS service to 0 tasks, scale it back to 1. " +
            "If you recreated the task, its public IP changed -- update APPRAISAL_API_URL.",
      detail: String(err),
    }, 502);
  }

  if (!upstream.ok) {
    const text = await upstream.text();
    return json({ error: "Appraisal API returned an error", status: upstream.status, detail: text },
                upstream.status);
  }

  // ---- PDF: pass the bytes straight through ------------------------------
  if (format === "pdf") {
    const bytes = await upstream.arrayBuffer();
    return new Response(bytes, {
      status: 200,
      headers: {
        "Content-Type": "application/pdf",
        "Content-Disposition":
          upstream.headers.get("content-disposition") ??
          'attachment; filename="adjustment-analysis.pdf"',
        "X-Indicated-Value": upstream.headers.get("x-indicated-value") ?? "",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Disposition, X-Indicated-Value",
      },
    });
  }

  return json(await upstream.json(), 200);
});

function json(data: unknown, status: number) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
