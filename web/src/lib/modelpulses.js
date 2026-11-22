/** Group only pulses with an observed busy turn, across all workspaces.
 * Prefer the live socket status; heartbeat is a fallback, not an override
 * for a newer idle response. Merely having a running process does not count.
 */
export function groupModelPulses(pulses = []) {
  const groups = new Map();
  for (const pulse of pulses) {
    if (!pulse?.id || pulse.state !== 'running') continue;
    const activity = pulse.live?.socket_state ?? pulse.live?.heartbeat?.state;
    if (activity !== 'busy') continue;
    const model = pulse.model || 'Unknown model';
    if (!groups.has(model)) groups.set(model, []);
    groups.get(model).push(pulse);
  }
  return [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([model, members]) => ({
    model,
    pulses: [...members].sort((a, b) =>
      Number(b.state === 'running') - Number(a.state === 'running') ||
      (a.title || a.id).localeCompare(b.title || b.id))
  }));
}
