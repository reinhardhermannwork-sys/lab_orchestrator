// Tests for the frontend server's security boundary (server/app.ts),
// against a stand-in orchestrator: a real HTTP server on a random port
// that records every request it receives, so these tests check what
// actually leaves the frontend — not just what it answers.

import Fastify, { type FastifyInstance } from "fastify";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { buildApp } from "../server/app.ts";
import type { Config } from "../server/config.ts";

const ID = "01M3TCP7NC58TXNRXGJGS7STZW";

interface Seen {
  method: string;
  url: string;
  body: unknown;
}

let orchestrator: FastifyInstance;
let seen: Seen[];
let reply: { status: number; body: unknown };

beforeEach(async () => {
  seen = [];
  reply = { status: 200, body: [] };
  orchestrator = Fastify();
  orchestrator.all("/*", async (request, res) => {
    seen.push({ method: request.method, url: request.url, body: request.body ?? null });
    return res.code(reply.status).send(reply.body);
  });
  await orchestrator.listen({ host: "127.0.0.1", port: 0 });
});

afterEach(async () => {
  await orchestrator.close();
});

function app(overrides: Partial<Config> = {}) {
  const address = orchestrator.server.address();
  if (address === null || typeof address === "string") throw new Error("no port");
  return buildApp(
    {
      orchestratorUrl: `http://127.0.0.1:${address.port}`,
      userHeader: "x-authentik-username",
      devUser: null,
      clientDir: null,
      host: "127.0.0.1",
      port: 0,
      ...overrides,
    },
    { logger: false },
  );
}

const asHermann = { "x-authentik-username": "hermann" };

describe("identity", () => {
  it("rejects /api requests without the identity header, without calling the orchestrator", async () => {
    const res = await app().inject({ method: "GET", url: "/api/machines" });
    expect(res.statusCode).toBe(401);
    expect(seen).toEqual([]);
  });

  it("rejects a malformed username", async () => {
    const res = await app().inject({
      method: "GET",
      url: "/api/me",
      headers: { "x-authentik-username": "her mann; rm -rf" },
    });
    expect(res.statusCode).toBe(401);
  });

  it("rejects the header sent twice", async () => {
    const res = await app().inject({
      method: "GET",
      url: "/api/me",
      headers: { "x-authentik-username": ["hermann", "mallory"] },
    });
    expect(res.statusCode).toBe(401);
  });

  it("reports the user from the header", async () => {
    const res = await app().inject({ method: "GET", url: "/api/me", headers: asHermann });
    expect(res.json()).toEqual({ user: "hermann" });
  });

  it("uses the dev user only when the header is absent", async () => {
    const dev = app({ devUser: "dev" });
    expect((await dev.inject({ method: "GET", url: "/api/me" })).json()).toEqual({ user: "dev" });
    expect(
      (await dev.inject({ method: "GET", url: "/api/me", headers: asHermann })).json(),
    ).toEqual({ user: "hermann" });
  });

  it("needs no identity for /healthz", async () => {
    const res = await app().inject({ method: "GET", url: "/healthz" });
    expect(res.statusCode).toBe(200);
  });
});

describe("forwarding", () => {
  it("lists machines", async () => {
    reply.body = [{ machine_type: "machine_1", display_name: "Machine 1" }];
    const res = await app().inject({ method: "GET", url: "/api/machines", headers: asHermann });
    expect(res.json()).toEqual(reply.body);
    expect(seen.map((s) => s.url)).toEqual(["/v1/machines"]);
  });

  it("returns the user's current instance, or null", async () => {
    reply.body = [{ instance_id: ID, state: "READY" }];
    const found = await app().inject({ method: "GET", url: "/api/instance", headers: asHermann });
    expect(found.json()).toEqual({ instance: { instance_id: ID, state: "READY" } });
    expect(seen[0]?.url).toBe("/v1/instances?user=hermann");

    reply.body = [];
    const none = await app().inject({ method: "GET", url: "/api/instance", headers: asHermann });
    expect(none.json()).toEqual({ instance: null });
  });

  it("creates an instance as the header user, whatever the body claims", async () => {
    reply.status = 202;
    reply.body = { instance_id: ID, machine_type: "machine_1", state: "PROVISIONING" };
    const res = await app().inject({
      method: "POST",
      url: "/api/instances",
      headers: asHermann,
      payload: { machine_type: "machine_1" },
    });
    expect(res.statusCode).toBe(202);
    expect(seen[0]).toMatchObject({
      method: "POST",
      url: "/v1/instances",
      body: { user: "hermann", machine_type: "machine_1" },
    });
  });

  it("refuses a body that tries to set the user", async () => {
    const res = await app().inject({
      method: "POST",
      url: "/api/instances",
      headers: asHermann,
      payload: { machine_type: "machine_1", user: "mallory" },
    });
    // Fastify's validation strips unknown body fields, so the forged
    // `user` is dropped and the call goes out as the header user.
    expect(res.statusCode).toBe(200);
    expect(seen).toHaveLength(1);
    expect(seen[0]?.body).toEqual({ user: "hermann", machine_type: "machine_1" });
  });

  it("passes the user on instance reads and releases", async () => {
    reply.body = { instance_id: ID, state: "READY" };
    await app().inject({ method: "GET", url: `/api/instances/${ID}`, headers: asHermann });
    reply.status = 202;
    await app().inject({ method: "DELETE", url: `/api/instances/${ID}`, headers: asHermann });
    expect(seen.map((s) => `${s.method} ${s.url}`)).toEqual([
      `GET /v1/instances/${ID}?user=hermann`,
      `DELETE /v1/instances/${ID}?user=hermann`,
    ]);
  });

  it("rejects malformed instance ids without calling the orchestrator", async () => {
    const res = await app().inject({
      method: "GET",
      url: "/api/instances/..%2Fmachines",
      headers: asHermann,
    });
    expect(res.statusCode).toBe(400);
    expect(seen).toEqual([]);
  });

  it("encodes unusual but valid usernames in the query", async () => {
    await app().inject({
      method: "GET",
      url: "/api/instance",
      headers: { "x-authentik-username": "a+b@example.org" },
    });
    expect(seen[0]?.url).toBe("/v1/instances?user=a%2Bb%40example.org");
  });

  it("has no generic proxy", async () => {
    const res = await app().inject({ method: "GET", url: "/api/v1/instances", headers: asHermann });
    expect(res.statusCode).toBe(404);
    expect(seen).toEqual([]);
  });
});

describe("errors", () => {
  it("passes a quota conflict's message through", async () => {
    reply.status = 409;
    reply.body = { detail: "user 'hermann' already has an active instance" };
    const res = await app().inject({
      method: "POST",
      url: "/api/instances",
      headers: asHermann,
      payload: { machine_type: "machine_1" },
    });
    expect(res.statusCode).toBe(409);
    expect(res.json()).toEqual({ error: "user 'hermann' already has an active instance" });
  });

  it("turns the orchestrator's 404 into a plain 404", async () => {
    reply.status = 404;
    reply.body = { detail: "instance not found" };
    const res = await app().inject({ method: "GET", url: `/api/instances/${ID}`, headers: asHermann });
    expect(res.statusCode).toBe(404);
  });

  it("answers 502 when the orchestrator is down", async () => {
    const frontend = app();
    await orchestrator.close();
    const res = await frontend.inject({ method: "GET", url: "/api/machines", headers: asHermann });
    expect(res.statusCode).toBe(502);
  });

  it("answers 502 for orchestrator server errors", async () => {
    reply.status = 500;
    reply.body = { detail: "boom" };
    const res = await app().inject({ method: "GET", url: "/api/machines", headers: asHermann });
    expect(res.statusCode).toBe(502);
    expect(JSON.stringify(res.json())).not.toContain("boom");
  });
});
