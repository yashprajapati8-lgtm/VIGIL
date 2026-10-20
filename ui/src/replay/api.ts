import { ApiError } from "../api/errors";
import { player, SCENARIO_TEXT } from "./player";

// The same routes the live app calls, answered from the recording. Anything that
// would change a real system (operator controls, approvals, the shop) says plainly
// that it needs the live stack.

const LIVE_ONLY = "This is a replay of a recorded run. Operator controls work on the live stack (docker compose up).";

export async function replayRequest<T>(method: string, url: string): Promise<T> {
  const path = url.split("?")[0];
  if (method === "GET") {
    if (path === "/api/health") return player.health() as T;
    if (path === "/api/control") return player.health()?.control as T;
    if (path === "/api/incidents") return { incidents: player.incidentList() } as T;
    const m = path.match(/^\/api\/incidents\/([A-Za-z0-9-]+)$/);
    if (m) {
      const hit = player.incident(m[1]);
      if (!hit) throw new ApiError(404, `${m[1]} is not part of this recording`);
      return hit as T;
    }
    if (path === "/chaos/scenarios") {
      const idx = await player.loadIndex();
      return Object.fromEntries(idx.chapters.map((c) => [c.scenario, { label: c.label, ...SCENARIO_TEXT[c.scenario] }])) as T;
    }
    if (path === "/chaos/status") return player.chaosStatus() as T;
  }
  if (method === "POST") {
    const inject = path.match(/^\/chaos\/scenarios\/([a-z-]+)\/inject$/);
    if (inject) {
      await player.start(inject[1]);
      return { injected: inject[1] } as T;
    }
    if (path === "/chaos/reset") {
      await player.reset();
      return { errors: {} } as T;
    }
    if (path === "/api/store/pay") throw new ApiError(503, "The demo shop needs the live stack: this page is a replay");
    throw new ApiError(403, LIVE_ONLY);
  }
  throw new ApiError(404, "Not part of the recording");
}
