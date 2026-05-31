const { performance } = require("node:perf_hooks");

function parseArgs() {
  const args = {
    baseUrl: "http://127.0.0.1:8000",
    endpoint: "chat",
    stream: false,
    requests: 1000,
    concurrency: 100,
    modelName: "gpt-4o-mini",
    rawApiKey: "sk-aotu-benchmark-0123456789abcdefghijklmnopqrstuvwxyz",
  };
  for (let index = 2; index < process.argv.length; index += 1) {
    const key = process.argv[index];
    const next = process.argv[index + 1];
    if (key === "--base-url") {
      args.baseUrl = next;
      index += 1;
    } else if (key === "--endpoint") {
      args.endpoint = next;
      index += 1;
    } else if (key === "--stream") {
      args.stream = true;
    } else if (key === "--requests") {
      args.requests = Number(next);
      index += 1;
    } else if (key === "--concurrency") {
      args.concurrency = Number(next);
      index += 1;
    } else if (key === "--model-name") {
      args.modelName = next;
      index += 1;
    } else if (key === "--raw-api-key") {
      args.rawApiKey = next;
      index += 1;
    }
  }
  args.requests = Math.max(1, Math.floor(args.requests || 1));
  args.concurrency = Math.max(1, Math.floor(args.concurrency || 1));
  return args;
}

function buildPayload({ endpoint, modelName, stream }) {
  if (endpoint === "responses") {
    return {
      model: modelName,
      input: "benchmark ping",
      stream,
      max_output_tokens: 128,
    };
  }
  return {
    model: modelName,
    messages: [{ role: "user", content: "benchmark ping" }],
    stream,
    max_tokens: 128,
  };
}

function percentile(values, pct) {
  if (!values.length) {
    return 0;
  }
  const index = Math.max(0, Math.min(values.length - 1, Math.floor((pct / 100) * (values.length - 1))));
  return values[index];
}

async function readResponse(response) {
  let bytes = 0;
  if (!response.body) {
    return bytes;
  }
  const reader = response.body.getReader();
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    bytes += value.byteLength;
  }
  return bytes;
}

async function main() {
  const args = parseArgs();
  const path = args.endpoint === "responses" ? "/v1/responses" : "/v1/chat/completions";
  const baseUrls = args.baseUrl
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const urls = (baseUrls.length ? baseUrls : [args.baseUrl]).map((baseUrl) => `${baseUrl.replace(/\/$/, "")}${path}`);
  const payload = JSON.stringify(buildPayload(args));
  const headers = {
    authorization: `Bearer ${args.rawApiKey}`,
    "content-type": "application/json",
  };
  let nextIndex = 0;
  let success = 0;
  let failed = 0;
  let totalBytes = 0;
  const latencies = [];
  const statusCounts = new Map();
  const errorCounts = new Map();

  async function worker() {
    while (true) {
      const current = nextIndex;
      nextIndex += 1;
      if (current >= args.requests) {
        return;
      }
      const started = performance.now();
      try {
        const url = urls[current % urls.length];
        const response = await fetch(url, { method: "POST", headers, body: payload });
        const bytes = await readResponse(response);
        totalBytes += bytes;
        statusCounts.set(response.status, (statusCounts.get(response.status) || 0) + 1);
        if (response.status < 400) {
          success += 1;
        } else {
          failed += 1;
          const key = `http_${response.status}`;
          errorCounts.set(key, (errorCounts.get(key) || 0) + 1);
        }
      } catch (error) {
        failed += 1;
        const key = `${error.name || "Error"}: ${error.message || String(error)}`;
        errorCounts.set(key, (errorCounts.get(key) || 0) + 1);
      } finally {
        latencies.push(performance.now() - started);
      }
    }
  }

  const started = performance.now();
  await Promise.all(Array.from({ length: args.concurrency }, worker));
  const wallTimeSeconds = (performance.now() - started) / 1000;
  latencies.sort((left, right) => left - right);
  const sumLatency = latencies.reduce((sum, value) => sum + value, 0);
  const statusObject = Object.fromEntries([...statusCounts.entries()].map(([key, value]) => [String(key), value]));
  const errorObject = Object.fromEntries(errorCounts.entries());

  console.log(`[benchmark]`);
  console.log(`total_requests=${args.requests}`);
  console.log(`success_requests=${success}`);
  console.log(`failed_requests=${failed}`);
  console.log(`wall_time_s=${wallTimeSeconds.toFixed(3)}`);
  console.log(`throughput_rps=${(args.requests / wallTimeSeconds).toFixed(2)}`);
  console.log(`latency_avg_ms=${(sumLatency / Math.max(1, latencies.length)).toFixed(2)}`);
  console.log(`latency_p50_ms=${percentile(latencies, 50).toFixed(2)}`);
  console.log(`latency_p95_ms=${percentile(latencies, 95).toFixed(2)}`);
  console.log(`latency_max_ms=${(latencies.at(-1) || 0).toFixed(2)}`);
  console.log(`total_bytes=${totalBytes}`);
  console.log(`status_counts=${JSON.stringify(statusObject)}`);
  if (errorCounts.size > 0) {
    console.log(`errors=${JSON.stringify(errorObject)}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
