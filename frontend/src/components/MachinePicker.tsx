import type { Machine } from "../api.ts";

interface Props {
  machines: Machine[];
  busy: boolean;
  onRequest: (machineType: string) => void;
}

export function MachinePicker({ machines, busy, onRequest }: Props) {
  if (machines.length === 0) {
    return <p className="text-slate-600">No machines are available right now.</p>;
  }
  return (
    <section>
      <h2 className="mb-1 text-lg font-semibold">Choose a machine</h2>
      <p className="mb-4 text-sm text-slate-600">
        You get a fresh VM with that machine's software for up to 4 hours.
      </p>
      <ul className="grid gap-3 sm:grid-cols-2">
        {machines.map((machine) => (
          <li
            key={machine.machine_type}
            className="flex items-center justify-between rounded-lg border border-slate-200 bg-white p-4 shadow-sm"
          >
            <span className="font-medium">{machine.display_name}</span>
            <button
              type="button"
              disabled={busy}
              onClick={() => onRequest(machine.machine_type)}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Start
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
