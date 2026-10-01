// @vitest-environment jsdom
//
// The main user flow through the real App, with fetch mocked at the
// boundary to the frontend's own server.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App.tsx";

const ID = "01M3TCP7NC58TXNRXGJGS7STZW";

function instance(state: string, extra: Record<string, unknown> = {}) {
  return {
    instance_id: ID,
    machine_type: "machine_1",
    machine_name: "Machine 1",
    state,
    hostname: null,
    ip: null,
    ssh: null,
    expires_at: new Date(Date.now() + 4 * 3600_000).toISOString(),
    failure_reason: null,
    ...extra,
  };
}

function mockServer(routes: Record<string, () => { status?: number; body: unknown }>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${url}`;
      calls.push(key);
      const route = routes[key];
      if (!route) return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
      const { status = 200, body } = route();
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("lists machines and starts one", async () => {
    const calls = mockServer({
      "GET /api/me": () => ({ body: { user: "hermann" } }),
      "GET /api/machines": () => ({ body: [{ machine_type: "machine_1", display_name: "Machine 1" }] }),
      "GET /api/instance": () => ({ body: { instance: null } }),
      "POST /api/instances": () => ({
        status: 202,
        body: { instance_id: ID, machine_type: "machine_1", state: "PROVISIONING" },
      }),
      [`GET /api/instances/${ID}`]: () => ({ body: instance("PROVISIONING") }),
    });
    render(<App />);

    expect(await screen.findByText("hermann")).toBeTruthy();
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));

    expect((await screen.findByTestId("state-label")).textContent).toBe("Creating the VM");
    expect(calls).toContain("POST /api/instances");
  });

  it("resumes the user's running instance after a reload", async () => {
    mockServer({
      "GET /api/me": () => ({ body: { user: "hermann" } }),
      "GET /api/machines": () => ({ body: [] }),
      "GET /api/instance": () => ({
        body: { instance: instance("READY", { ip: "10.28.28.42", ssh: { username: "labuser", port: 22 } }) },
      }),
    });
    render(<App />);

    expect(await screen.findByText("Your machine is ready.")).toBeTruthy();
    expect(screen.getByText("ssh labuser@10.28.28.42")).toBeTruthy();
    expect(screen.getByRole("button", { name: "End session" })).toBeTruthy();
  });

  it("shows the reason when provisioning failed", async () => {
    mockServer({
      "GET /api/me": () => ({ body: { user: "hermann" } }),
      "GET /api/machines": () => ({ body: [] }),
      "GET /api/instance": () => ({
        body: { instance: instance("DESTROYED", { failure_reason: "readiness criteria not met" }) },
      }),
    });
    render(<App />);

    expect(await screen.findByText("The machine could not be started.")).toBeTruthy();
    expect(screen.getByText("readiness criteria not met")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Back to machines" })).toBeTruthy();
  });

  it("explains a quota conflict", async () => {
    mockServer({
      "GET /api/me": () => ({ body: { user: "hermann" } }),
      "GET /api/machines": () => ({ body: [{ machine_type: "machine_1", display_name: "Machine 1" }] }),
      "GET /api/instance": () => ({ body: { instance: null } }),
      "POST /api/instances": () => ({
        status: 409,
        body: { error: "3 instances are already active system-wide" },
      }),
    });
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "3 instances are already active system-wide",
    );
  });

  it("says so when the user isn't signed in", async () => {
    mockServer({});
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ error: "not authenticated" }), { status: 401 })),
    );
    render(<App />);
    expect((await screen.findByRole("alert")).textContent).toBe("You are not signed in.");
  });
});
