// SDK carrier validated against DSH 0.2.0-rc.2, using the public Agent resume API.
import { resolve } from "node:path";

export const name = "codex-dsh-session";
export const inject = ["agents", "llm", "sessionPersistence"];

export async function apply(ctx, config) {
  const root = new URL(config.moduleRoot);
  const { JsonRpcLineTransport } = await import(new URL(
    "@deepseek-ai/dsh-sdk-protocol/lib/index.js", root,
  ));
  const { createUserMessage } = await import(new URL(
    "@deepseek-ai/dsh-llm/lib/index.js", root,
  ));
  const { SessionPersistenceNotFoundError } = await import(new URL(
    "@deepseek-ai/dsh-session-persistence/lib/index.js", root,
  ));
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout);
  let route;
  let handle;
  let creation;
  let stopping = false;
  let teardown;
  const closeAgent = () => teardown ??= (async () => {
    if (creation) await creation.catch(() => {});
    if (handle) await handle.dispose();
  })();

  ctx.on("session/event", (session, event) => {
    if (String(session.id) === config.sessionId) {
      transport.notify("session.event", { sessionId: String(session.id), event });
    }
  });
  ctx.on("agent/status", ({ agent, status }) => {
    if (String(agent.session.id) === config.sessionId) {
      transport.notify("session.status", { sessionId: String(agent.session.id), status });
    }
  });

  async function openAgent() {
    if (handle) return handle;
    if (creation) return creation;
    creation = (async () => {
      let opened;
      let action;
      try {
        opened = await ctx.agents.resume({
          resumeSessionId: config.sessionId, agentOptions: route,
        });
        action = "resumed";
      } catch (error) {
        // Corruption, ownership and configuration errors must never create a replacement.
        if (!(error instanceof SessionPersistenceNotFoundError)) throw error;
        opened = await ctx.agents.create({
          sessionId: config.sessionId,
          meta: { cwd: resolve(config.workspace) }, agentOptions: route,
        });
        action = "created";
      }
      if (resolve(opened.agent.session.header.cwd ?? "") !== resolve(config.workspace)) {
        await opened.dispose();
        throw new Error("Persisted session belongs to a different workspace");
      }
      handle = opened;
      transport.notify("coordination.session", {
        sessionId: config.sessionId, action,
        eventCount: opened.agent.session.snapshotEvents().length,
      });
      return opened;
    })();
    return creation;
  }

  transport.onRequest(async (method, params) => {
    if (stopping) throw new Error("Coordination runtime is shutting down");
    if (method === "initialize") {
      if (route) throw new Error("Runtime is already initialized");
      await ctx.get("loader")?.await();
      if (resolve(params.cwd) !== resolve(config.workspace)) throw new Error("Unexpected workspace");
      if (typeof params.provider !== "string" || typeof params.model !== "string") {
        throw new Error("Provider and model must be specified");
      }
      if (!Number.isSafeInteger(params.maxTokens) || params.maxTokens <= 0) {
        throw new Error("maxTokens must be a positive safe integer");
      }
      const candidate = {
        provider: params.provider, model: params.model, maxTokens: params.maxTokens,
      };
      await ctx.llm.resolveCallConfig(candidate);
      route = candidate;
      return { serverInfo: { name: "codex-dsh-session-bridge", version: "0.1.0" } };
    }
    if (method === "session/prompt") {
      if (!route) throw new Error("Runtime is not initialized");
      if (params.sessionId !== config.sessionId) throw new Error("Unexpected session identity");
      if (!Array.isArray(params.contentBlocks) || params.contentBlocks.length !== 1
          || params.contentBlocks[0].type !== "text"
          || typeof params.contentBlocks[0].text !== "string") {
        throw new Error("This coordination entry accepts one text block per prompt");
      }
      const opened = await openAgent();
      const message = createUserMessage({ content: params.contentBlocks, source: { kind: "user" } });
      opened.agent.followup(message);
      return { messageId: message.id };
    }
    if (method === "shutdown") {
      stopping = true;
      await closeAgent();
      setImmediate(async () => {
        try {
          await transport.flush();
          await ctx.root.fiber.dispose();
          process.exit(0);
        } catch {
          process.exit(1);
        }
      });
      return {};
    }
    throw new Error(`Unsupported coordination method: ${method}`);
  });
  ctx.effect(() => {
    transport.start();
    return async () => {
      stopping = true;
      await closeAgent();
      transport.close();
    };
  }, "codex-dsh-session.serve");
}
