import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Explicitly selected native binding. Workflow and validation stay in the package.
export default function (pi) {
  const instruction = "__INSTRUCTION_SHA256__";
  const schema = JSON.parse(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "../tool-call.schema.json"), "utf8"));
  let root;
  let rootIdentity;
  let interpreter;
  let active = false;
  let stopActive;
  let activeClosed;

  pi.on("session_shutdown", async () => {
    stopActive?.();
    if (activeClosed) await activeClosed;
  });

  pi.on("session_start", async (_event, ctx) => {
    if (pi.getAllTools().some((tool) => tool.name === "evidence_wiki")) {
      throw new Error("EvidenceWiki tool name collision; choose one binding explicitly.");
    }
    root = realpathSync(ctx.cwd);
    rootIdentity = statSync(root, { bigint: true });
    interpreter = process.env.EVIDENCE_WIKI_PYTHON;
    if (!interpreter || !isAbsolute(interpreter) || !statSync(interpreter).isFile()) {
      throw new Error("EvidenceWiki requires an explicitly selected EVIDENCE_WIKI_PYTHON executable.");
    }
    pi.registerTool({
      name: "evidence_wiki",
      label: "EvidenceWiki",
      description: "Read installed guidance, inspect strict evidence, compute declared results, or export reviewed claims through canonical owners. Transport success does not prove evidence acceptance or host enforcement.",
      parameters: schema,
      async execute(toolCallId, params, signal, _onUpdate, context) {
        const current = statSync(root, { bigint: true });
        if (active || realpathSync(context.cwd) !== root || current.dev !== rootIdentity.dev || current.ino !== rootIdentity.ino ||
            params.instruction_sha256 !== instruction) {
          throw new Error("EvidenceWiki context, instruction identity or concurrent-call conflict.");
        }
        if (signal?.aborted) throw new Error("EvidenceWiki call cancelled before dispatch.");
        active = true;
        try {
          return await new Promise((fulfill, reject) => {
            const child = spawn(interpreter, ["-m", "evidence_wiki.cli", "agent", "invoke", "--target", root], {
              cwd: root, shell: false, detached: false,
              stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
            });
            let size = 0;
            let diagnosticSize = 0;
            let chunks = [];
            let stopped = false;
            let hardStop;
            let markClosed;
            activeClosed = new Promise((done) => { markClosed = done; });
            const stop = () => {
              stopped = true;
              try {
                child.kill("SIGTERM");
              } catch { /* Already exited. */ }
              hardStop ??= setTimeout(() => {
                try {
                  child.kill("SIGKILL");
                } catch { /* Already exited. */ }
              }, 7000);
              hardStop.unref();
            };
            stopActive = stop;
            const timer = setTimeout(stop, 70000);
            signal?.addEventListener("abort", stop, { once: true });
            child.stdout.on("data", (data) => {
              size += data.length;
              if (size > 2097152) stop();
              else chunks.push(data);
            });
            child.stderr.on("data", (data) => {
              diagnosticSize += data.length;
              if (diagnosticSize > 1048576) stop();
            });
            child.on("error", () => { stopped = true; });
            child.stdin.on("error", () => { stop(); });
            child.on("close", (code) => {
              markClosed();
              stopActive = undefined;
              clearTimeout(timer);
              clearTimeout(hardStop);
              signal?.removeEventListener("abort", stop);
              if (stopped || code !== 0) {
                chunks = [];
                reject(new Error("EvidenceWiki call interrupted or refused; inspect canonical state before retrying."));
                return;
              }
              try {
                const text = Buffer.concat(chunks).toString("utf8");
                const result = JSON.parse(text);
                const closing = statSync(root, { bigint: true });
                if (result.schema_version !== "evidence-framework-result/v1" || result.request_id !== params.request_id ||
                    closing.dev !== rootIdentity.dev || closing.ino !== rootIdentity.ino ||
                    result.operation !== params.operation || result.instruction_sha256 !== instruction || result.host_enforced !== false ||
                    typeof result.result_json !== "string" ||
                    createHash("sha256").update(result.result_json).digest("hex") !== result.result_sha256) {
                  throw new Error("Unqualified result");
                }
                fulfill({ content: [{ type: "text", text }], details: {
                  request_id: result.request_id, operation: result.operation,
                  result_sha256: result.result_sha256, host_enforced: false,
                  tool_call_binding: createHash("sha256").update(toolCallId).digest("hex"),
                } });
              } catch {
                reject(new Error("EvidenceWiki returned an invalid or incompatible result."));
              }
            });
            child.stdin.end(JSON.stringify(params));
          });
        } finally {
          active = false;
          activeClosed = undefined;
        }
      },
    });
  });
}
