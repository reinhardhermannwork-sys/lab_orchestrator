// Typed calls to the frontend's own server (server/app.ts). The browser
// never talks to the orchestrator directly and never sends a username.

export interface Machine {
  machine_type: string;
  display_name: string;
}

export interface Instance {
  instance_id: string;
  machine_type: string;
  machine_name: string;
  state: string;
  hostname: string | null;
  ip: string | null;
  ssh: { username: string; port: number } | null;
  expires_at: string;
  failure_reason: string | null;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      data && typeof data === "object" && "error" in data && typeof data.error === "string"
        ? data.error
        : `request failed (${response.status})`;
    throw new ApiError(response.status, message);
  }
  return data as T;
}

export const api = {
  me: () => request<{ user: string }>("GET", "/api/me"),
  machines: () => request<Machine[]>("GET", "/api/machines"),
  currentInstance: () => request<{ instance: Instance | null }>("GET", "/api/instance"),
  getInstance: (id: string) => request<Instance>("GET", `/api/instances/${id}`),
  requestInstance: (machineType: string) =>
    request<{ instance_id: string; machine_type: string; state: string }>("POST", "/api/instances", {
      machine_type: machineType,
    }),
  releaseInstance: (id: string) => request<Instance>("DELETE", `/api/instances/${id}`),
};
