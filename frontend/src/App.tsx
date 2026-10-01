import { useEffect, useState } from "react";

import { api, ApiError, type Instance, type Machine } from "./api.ts";
import { InstanceView } from "./components/InstanceView.tsx";
import { MachinePicker } from "./components/MachinePicker.tsx";

type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; user: string; machines: Machine[] };

export function App() {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [instance, setInstance] = useState<Instance | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [me, machines, current] = await Promise.all([
          api.me(),
          api.machines(),
          api.currentInstance(),
        ]);
        setInstance(current.instance);
        setLoad({ status: "ready", user: me.user, machines });
      } catch (e) {
        setLoad({
          status: "error",
          message:
            e instanceof ApiError && e.status === 401
              ? "You are not signed in."
              : "The lab service is not available right now.",
        });
      }
    })();
  }, []);

  async function requestMachine(machineType: string) {
    setRequesting(true);
    setRequestError(null);
    try {
      const created = await api.requestInstance(machineType);
      setInstance(await api.getInstance(created.instance_id));
    } catch (e) {
      setRequestError(e instanceof Error ? e.message : "could not start the machine");
    } finally {
      setRequesting(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <header className="mb-8 flex items-baseline justify-between">
        <h1 className="text-2xl font-bold">Lab Machines</h1>
        {load.status === "ready" && <span className="text-sm text-slate-600">{load.user}</span>}
      </header>

      {load.status === "loading" && <p className="text-slate-600">Loading…</p>}
      {load.status === "error" && (
        <p className="text-red-700" role="alert">
          {load.message}
        </p>
      )}
      {load.status === "ready" &&
        (instance ? (
          <InstanceView
            key={instance.instance_id}
            initial={instance}
            onDone={() => setInstance(null)}
          />
        ) : (
          <>
            {requestError && (
              <p className="mb-4 text-sm text-red-700" role="alert">
                {requestError}
              </p>
            )}
            <MachinePicker
              machines={load.machines}
              busy={requesting}
              onRequest={requestMachine}
            />
          </>
        ))}
    </div>
  );
}
