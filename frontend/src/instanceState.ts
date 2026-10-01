// What each orchestrator lease state means to a user, and how often to
// poll it. Pure functions, so the UI logic is testable without React.

export type Phase = "starting" | "ready" | "ending" | "failed" | "ended";

interface StateInfo {
  phase: Phase;
  label: string;
  /** 1-based progress step while starting up; null otherwise. */
  step: number | null;
}

export const STARTUP_STEPS = ["Queued", "Creating the VM", "Starting", "Booting"] as const;

const STATES: Record<string, StateInfo> = {
  REQUESTED: { phase: "starting", label: "Queued", step: 1 },
  PROVISIONING: { phase: "starting", label: "Creating the VM", step: 2 },
  STARTING: { phase: "starting", label: "Starting", step: 3 },
  WAITING_READY: { phase: "starting", label: "Booting", step: 4 },
  READY: { phase: "ready", label: "Ready", step: null },
  CONNECTED: { phase: "ready", label: "Connected", step: null },
  DISCONNECTED_GRACE: { phase: "ready", label: "Disconnected — reconnect soon", step: null },
  DESTROYING: { phase: "ending", label: "Shutting down", step: null },
  FAILED: { phase: "failed", label: "Failed — cleaning up", step: null },
  CLEANUP: { phase: "failed", label: "Failed — cleaning up", step: null },
  DESTROYED: { phase: "ended", label: "Ended", step: null },
};

export function describeState(state: string): StateInfo {
  return STATES[state] ?? { phase: "starting", label: state, step: null };
}

/** Milliseconds until the next status poll, or null to stop polling. */
export function pollInterval(state: string): number | null {
  switch (describeState(state).phase) {
    case "starting":
    case "ending":
    case "failed":
      return 2_000;
    case "ready":
      return 15_000; // still watch for expiry or teardown
    case "ended":
      return null;
  }
}

/** "3h 52m", "12m", "less than a minute", or "expired". */
export function formatRemaining(expiresAt: string, now: Date = new Date()): string {
  const ms = new Date(expiresAt).getTime() - now.getTime();
  if (Number.isNaN(ms)) return "unknown";
  if (ms <= 0) return "expired";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "less than a minute";
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
}
