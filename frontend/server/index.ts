// Entry point: `node server/index.ts` (Node runs TypeScript directly by
// stripping types, so the server needs no build step).

import { buildApp } from "./app.ts";
import { loadConfig } from "./config.ts";

const config = loadConfig();
const app = buildApp(config);

if (config.devUser !== null) {
  app.log.warn(
    `LAB_FRONTEND_DEV_USER is set: requests without ${config.userHeader} run as ` +
      `"${config.devUser}". Local development only — never in a deployment.`,
  );
}

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, () => {
    app.close().then(
      () => process.exit(0),
      () => process.exit(1),
    );
  });
}

await app.listen({ host: config.host, port: config.port });
