// The frontend's server side (M9): serves the React app and a short,
// fixed list of /api routes, each forwarding to the orchestrator.
//
// This file is the security boundary between the browser and the
// orchestrator (architecture doc §4, §17):
//
//   - `user` comes only from the identity header that traefik copies from
//     authentik's forward-auth response. Traefik overwrites any value a
//     client sends, which is why the container must never be reachable
//     except through traefik. Nothing the browser sends — body, query,
//     path — can choose the user.
//   - Only the routes below exist. There is no generic proxy, so the
//     browser can't reach any other orchestrator endpoint.
//   - Every orchestrator call that touches an instance passes `user`, and
//     the orchestrator answers 404 for someone else's instance.

import { existsSync } from "node:fs";

import fastifyStatic from "@fastify/static";
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";

import type { Config } from "./config.ts";

declare module "fastify" {
  interface FastifyRequest {
    user: string;
  }
}

// authentik usernames: letters, digits and a few separators. Anything
// else (including a header sent twice) is refused rather than forwarded.
const USERNAME = /^[A-Za-z0-9@._+-]{1,150}$/;

const machineTypeSchema = { type: "string", pattern: "^[A-Za-z0-9_-]{1,64}$" } as const;
// Instance ids are ULIDs (orchestrator's create_instance).
const instanceIdParams = {
  type: "object",
  required: ["id"],
  properties: { id: { type: "string", pattern: "^[0-9A-HJKMNP-TV-Z]{26}$" } },
} as const;

const ORCHESTRATOR_TIMEOUT_MS = 10_000;

interface OrchestratorResult {
  status: number;
  body: unknown;
}

export function buildApp(config: Config, options: { logger?: boolean } = {}): FastifyInstance {
  const app = Fastify({ logger: options.logger ?? true });

  app.decorateRequest("user", "");

  app.addHook("onRequest", async (request, reply) => {
    if (!request.url.startsWith("/api/")) return;
    const user = userFrom(request, config);
    if (user === null) {
      return reply.code(401).send({ error: "not authenticated" });
    }
    request.user = user;
  });

  async function orchestrator(
    method: "GET" | "POST" | "DELETE",
    path: string,
    body?: unknown,
  ): Promise<OrchestratorResult> {
    const response = await fetch(`${config.orchestratorUrl}${path}`, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(ORCHESTRATOR_TIMEOUT_MS),
    });
    const text = await response.text();
    return { status: response.status, body: text ? JSON.parse(text) : null };
  }

  // Forward an orchestrator call, translating its errors into responses
  // that are safe and meaningful for the browser.
  async function forward(
    request: FastifyRequest,
    reply: FastifyReply,
    call: () => Promise<OrchestratorResult>,
    transform: (body: unknown) => unknown = (body) => body,
  ) {
    let result: OrchestratorResult;
    try {
      result = await call();
    } catch (error) {
      request.log.error({ err: error }, "orchestrator unreachable");
      return reply.code(502).send({ error: "the lab service is not reachable" });
    }
    const { status, body } = result;
    if (status >= 200 && status < 300) {
      return reply.code(status).send(transform(body));
    }
    request.log.warn({ status, body }, "orchestrator returned an error");
    if (status === 404) return reply.code(404).send({ error: "not found" });
    if (status === 409 || status === 400) {
      return reply.code(status).send({ error: detailOf(body) ?? "request rejected" });
    }
    return reply.code(502).send({ error: "the lab service returned an error" });
  }

  const userQuery = (request: FastifyRequest) => `user=${encodeURIComponent(request.user)}`;

  app.get("/api/me", async (request) => ({ user: request.user }));

  app.get("/api/machines", (request, reply) =>
    forward(request, reply, () => orchestrator("GET", "/v1/machines")),
  );

  // The user's current (active) instance, or null — lets a page reload
  // find a running VM.
  app.get("/api/instance", (request, reply) =>
    forward(
      request,
      reply,
      () => orchestrator("GET", `/v1/instances?${userQuery(request)}`),
      (body) => ({ instance: Array.isArray(body) ? (body[0] ?? null) : null }),
    ),
  );

  app.post<{ Body: { machine_type: string } }>(
    "/api/instances",
    {
      schema: {
        body: {
          type: "object",
          required: ["machine_type"],
          additionalProperties: false,
          properties: { machine_type: machineTypeSchema },
        },
      },
    },
    (request, reply) =>
      forward(request, reply, () =>
        orchestrator("POST", "/v1/instances", {
          user: request.user,
          machine_type: request.body.machine_type,
        }),
      ),
  );

  app.get<{ Params: { id: string } }>(
    "/api/instances/:id",
    { schema: { params: instanceIdParams } },
    (request, reply) =>
      forward(request, reply, () =>
        orchestrator("GET", `/v1/instances/${request.params.id}?${userQuery(request)}`),
      ),
  );

  app.delete<{ Params: { id: string } }>(
    "/api/instances/:id",
    { schema: { params: instanceIdParams } },
    (request, reply) =>
      forward(request, reply, () =>
        orchestrator("DELETE", `/v1/instances/${request.params.id}?${userQuery(request)}`),
      ),
  );

  // Container healthcheck; outside /api so it needs no identity.
  app.get("/healthz", async () => ({ status: "ok" }));

  if (config.clientDir !== null && existsSync(config.clientDir)) {
    app.register(fastifyStatic, { root: config.clientDir, wildcard: false });
  }

  app.setNotFoundHandler((request, reply) => {
    // Unknown /api routes stay JSON 404s; any other GET is a client-side
    // route of the single-page app.
    if (
      request.method === "GET" &&
      !request.url.startsWith("/api/") &&
      config.clientDir !== null &&
      existsSync(config.clientDir)
    ) {
      return reply.sendFile("index.html");
    }
    return reply.code(404).send({ error: "not found" });
  });

  return app;
}

function userFrom(request: FastifyRequest, config: Config): string | null {
  const raw = request.headers[config.userHeader];
  if (Array.isArray(raw)) return null;
  const value = raw?.trim();
  if (value) return USERNAME.test(value) ? value : null;
  return config.devUser;
}

function detailOf(body: unknown): string | null {
  if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
    return body.detail;
  }
  return null;
}
