// Environment-driven settings for the frontend server (M9).
//
// LAB_FRONTEND_DEV_USER exists only for local development without
// traefik/authentik. It must never be set in a deployment: with it, a
// request without the identity header runs as that user.

import { fileURLToPath } from "node:url";

export interface Config {
  /** Where the orchestrator API lives, on the internal lab network. */
  orchestratorUrl: string;
  /** Header traefik copies from authentik's forward-auth response (lowercase). */
  userHeader: string;
  /** Local-dev fallback user when the header is absent; null in deployments. */
  devUser: string | null;
  /** Built React app to serve; null to serve the API only (tests). */
  clientDir: string | null;
  host: string;
  port: number;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const port = Number(env.LAB_FRONTEND_PORT ?? "3000");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`LAB_FRONTEND_PORT is not a valid port: ${env.LAB_FRONTEND_PORT}`);
  }
  return {
    orchestratorUrl: (env.LAB_FRONTEND_ORCHESTRATOR_URL ?? "http://lab-orchestrator:8000").replace(
      /\/+$/,
      "",
    ),
    userHeader: (env.LAB_FRONTEND_USER_HEADER ?? "x-authentik-username").toLowerCase(),
    devUser: env.LAB_FRONTEND_DEV_USER?.trim() || null,
    clientDir:
      env.LAB_FRONTEND_CLIENT_DIR ?? fileURLToPath(new URL("../dist/client", import.meta.url)),
    host: env.LAB_FRONTEND_HOST ?? "0.0.0.0",
    port,
  };
}
