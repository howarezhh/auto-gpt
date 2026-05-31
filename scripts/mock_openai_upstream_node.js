const http = require("node:http");
const { randomUUID } = require("node:crypto");

function parseArgs() {
  const args = {
    host: "127.0.0.1",
    port: 18100,
    modelName: "gpt-4o-mini",
    delayMs: 0,
    streamChunks: 4,
    streamChunkDelayMs: 0,
    outputChars: 128,
  };
  for (let index = 2; index < process.argv.length; index += 1) {
    const key = process.argv[index];
    const next = process.argv[index + 1];
    if (key === "--host") {
      args.host = next;
      index += 1;
    } else if (key === "--port") {
      args.port = Number(next);
      index += 1;
    } else if (key === "--model-name") {
      args.modelName = next;
      index += 1;
    } else if (key === "--delay-ms") {
      args.delayMs = Number(next);
      index += 1;
    } else if (key === "--stream-chunks") {
      args.streamChunks = Number(next);
      index += 1;
    } else if (key === "--stream-chunk-delay-ms") {
      args.streamChunkDelayMs = Number(next);
      index += 1;
    } else if (key === "--output-chars") {
      args.outputChars = Number(next);
      index += 1;
    }
  }
  args.port = Math.max(1, Math.floor(args.port || 18100));
  args.delayMs = Math.max(0, Math.floor(args.delayMs || 0));
  args.streamChunks = Math.max(1, Math.floor(args.streamChunks || 1));
  args.streamChunkDelayMs = Math.max(0, Math.floor(args.streamChunkDelayMs || 0));
  args.outputChars = Math.max(1, Math.floor(args.outputChars || 128));
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function outputText(args) {
  return "x".repeat(args.outputChars);
}

function usage(promptText, completionText) {
  const promptTokens = Math.max(1, Math.floor(promptText.length / 4));
  const completionTokens = Math.max(1, Math.floor(completionText.length / 4));
  return {
    prompt_tokens: promptTokens,
    completion_tokens: completionTokens,
    total_tokens: promptTokens + completionTokens,
  };
}

function chunks(value, count) {
  const chunkSize = Math.max(1, Math.floor(value.length / count));
  const result = [];
  for (let index = 0; index < value.length; index += chunkSize) {
    result.push(value.slice(index, index + chunkSize));
  }
  return result;
}

function readJson(req) {
  return new Promise((resolve) => {
    const parts = [];
    req.on("data", (chunk) => parts.push(chunk));
    req.on("end", () => {
      try {
        const parsed = JSON.parse(Buffer.concat(parts).toString("utf8") || "{}");
        resolve(parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {});
      } catch {
        resolve({});
      }
    });
    req.on("error", () => resolve({}));
  });
}

function sendJson(res, payload, statusCode = 200, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(statusCode, {
    "content-type": "application/json",
    "content-length": body.length,
    connection: "keep-alive",
    ...extraHeaders,
  });
  res.end(body);
}

function chatPrompt(payload) {
  const messages = Array.isArray(payload.messages) ? payload.messages : [];
  const text = messages
    .map((item) => (item && typeof item.content === "string" ? item.content : ""))
    .filter(Boolean)
    .join(" ");
  return text || "benchmark";
}

function responsesPrompt(payload) {
  if (typeof payload.input === "string") {
    return payload.input;
  }
  if (Array.isArray(payload.input)) {
    const text = payload.input.filter((item) => typeof item === "string").join(" ");
    return text || "benchmark";
  }
  return "benchmark";
}

async function sendChat(args, res, payload) {
  const completionText = outputText(args);
  const responseId = `chatcmpl-${randomUUID().replaceAll("-", "")}`;
  const model = String(payload.model || args.modelName);
  const created = Math.floor(Date.now() / 1000);
  const usagePayload = usage(chatPrompt(payload), completionText);
  if (payload.stream === true) {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "close",
      "x-request-id": responseId,
    });
    if (args.delayMs) await sleep(args.delayMs);
    for (const part of chunks(completionText, args.streamChunks)) {
      res.write(`data: ${JSON.stringify({ id: responseId, object: "chat.completion.chunk", created, model, choices: [{ index: 0, delta: { content: part }, finish_reason: null }] })}\n\n`);
      if (args.streamChunkDelayMs) await sleep(args.streamChunkDelayMs);
    }
    res.write(`data: ${JSON.stringify({ id: responseId, object: "chat.completion.chunk", created, model, choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: usagePayload })}\n\n`);
    res.end("data: [DONE]\n\n");
    return;
  }
  if (args.delayMs) await sleep(args.delayMs);
  sendJson(
    res,
    {
      id: responseId,
      object: "chat.completion",
      created,
      model,
      choices: [{ index: 0, message: { role: "assistant", content: completionText }, finish_reason: "stop" }],
      usage: usagePayload,
    },
    200,
    { "x-request-id": responseId },
  );
}

async function sendResponses(args, res, payload) {
  const completionText = outputText(args);
  const responseId = `resp_${randomUUID().replaceAll("-", "")}`;
  const model = String(payload.model || args.modelName);
  const usagePayload = usage(responsesPrompt(payload), completionText);
  if (payload.stream === true) {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "close",
      "x-request-id": responseId,
    });
    if (args.delayMs) await sleep(args.delayMs);
    for (const part of chunks(completionText, args.streamChunks)) {
      res.write(`event: response.output_text.delta\ndata: ${JSON.stringify({ type: "response.output_text.delta", response_id: responseId, delta: part })}\n\n`);
      if (args.streamChunkDelayMs) await sleep(args.streamChunkDelayMs);
    }
    res.write(`event: response.completed\ndata: ${JSON.stringify({ type: "response.completed", response: { id: responseId, object: "response", created_at: Math.floor(Date.now() / 1000), model, status: "completed", output_text: completionText, usage: usagePayload } })}\n\n`);
    res.end("data: [DONE]\n\n");
    return;
  }
  if (args.delayMs) await sleep(args.delayMs);
  sendJson(
    res,
    {
      id: responseId,
      object: "response",
      created_at: Math.floor(Date.now() / 1000),
      model,
      status: "completed",
      output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: completionText }] }],
      output_text: completionText,
      usage: usagePayload,
    },
    200,
    { "x-request-id": responseId },
  );
}

async function main() {
  const args = parseArgs();
  const server = http.createServer(async (req, res) => {
    const path = (req.url || "/").split("?", 1)[0];
    if (req.method === "GET" && path === "/v1/models") {
      sendJson(res, { object: "list", data: [{ id: args.modelName, object: "model", created: Math.floor(Date.now() / 1000), owned_by: "mock-upstream" }] });
      return;
    }
    if (req.method !== "POST") {
      sendJson(res, { error: { message: "not found" } }, 404);
      return;
    }
    const payload = await readJson(req);
    if (path === "/v1/chat/completions") {
      await sendChat(args, res, payload);
      return;
    }
    if (path === "/v1/responses") {
      await sendResponses(args, res, payload);
      return;
    }
    sendJson(res, { error: { message: "not found" } }, 404);
  });
  server.keepAliveTimeout = 60_000;
  server.headersTimeout = 65_000;
  server.maxRequestsPerSocket = 0;
  server.listen(args.port, args.host, () => {
    console.log(`mock_openai_upstream_node listening on http://${args.host}:${args.port}/v1`);
  });
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
