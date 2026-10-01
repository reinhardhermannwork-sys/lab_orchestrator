import { useEffect, useState } from "react";

import { api, type Instance } from "../api.ts";
import { describeState, formatRemaining, pollInterval, STARTUP_STEPS } from "../instanceState.ts";

interface Props {
  initial: Instance;
  onDone: () => void;
}

export function InstanceView({ initial, onDone }: Props) {
  const [instance, setInstance] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [releasing, setReleasing] = useState(false);
  const [now, setNow] = useState(() => new Date());

  // Poll this instance until it has ended; faster while it's changing.
  useEffect(() => {
    const interval = pollInterval(instance.state);
    if (interval === null) return;
    const timer = setTimeout(async () => {
      try {
        setInstance(await api.getInstance(instance.instance_id));
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "could not refresh status");
        setNow(new Date()); // re-arms this effect, so polling retries
      }
    }, interval);
    return () => clearTimeout(timer);
  }, [instance, now]);

  // Keep the remaining-time display current.
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(timer);
  }, []);

  async function release() {
    if (!window.confirm("End this session? The VM and everything on it will be deleted.")) return;
    setReleasing(true);
    try {
      setInstance(await api.releaseInstance(instance.instance_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not end the session");
    } finally {
      setReleasing(false);
    }
  }

  const info = describeState(instance.state);

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h2 className="text-lg font-semibold">{instance.machine_name}</h2>
        <span className="text-sm text-slate-600" data-testid="state-label">
          {info.label}
        </span>
      </div>

      {info.phase === "starting" && (
        <ol className="mb-4 flex gap-2" aria-label="progress">
          {STARTUP_STEPS.map((name, index) => (
            <li
              key={name}
              className={`flex-1 rounded px-2 py-1 text-center text-xs ${
                info.step !== null && index < info.step
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-100 text-slate-500"
              }`}
            >
              {name}
            </li>
          ))}
        </ol>
      )}

      {info.phase === "ready" && (
        <div className="mb-4 rounded-md bg-emerald-50 p-4 text-sm">
          <p className="mb-2 font-medium text-emerald-800">Your machine is ready.</p>
          {/* M11 replaces this with the Guacamole session. */}
          <p className="text-slate-700">
            Browser access is not available yet. Connection details for testing:
          </p>
          <code className="mt-2 block text-slate-800">
            ssh {instance.ssh?.username ?? "labuser"}@{instance.ip ?? instance.hostname}
          </code>
        </div>
      )}

      {(info.phase === "failed" || (info.phase === "ended" && instance.failure_reason)) && (
        <div className="mb-4 rounded-md bg-red-50 p-4 text-sm text-red-800">
          <p className="font-medium">The machine could not be started.</p>
          {instance.failure_reason && <p className="mt-1 break-words">{instance.failure_reason}</p>}
        </div>
      )}

      {info.phase === "ended" && !instance.failure_reason && (
        <p className="mb-4 text-sm text-slate-600">This session has ended and the VM was deleted.</p>
      )}

      {error && (
        <p className="mb-4 text-sm text-amber-700" role="alert">
          {error}
        </p>
      )}

      <div className="flex items-center justify-between">
        {info.phase === "starting" || info.phase === "ready" ? (
          <>
            <span className="text-sm text-slate-600">
              Ends in {formatRemaining(instance.expires_at, now)}
            </span>
            <button
              type="button"
              onClick={release}
              disabled={releasing}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50"
            >
              End session
            </button>
          </>
        ) : info.phase === "ended" ? (
          <button
            type="button"
            onClick={onDone}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
          >
            Back to machines
          </button>
        ) : (
          <span className="text-sm text-slate-600">Cleaning up…</span>
        )}
      </div>
    </section>
  );
}
