import type { Health, Incident, Point, StateResponse, StreamMessage, VigilEvent } from "../api/types";

// Replay mode (`vite build --mode replay`): the hosted link plays back real runs
// recorded by the Proving Ground on the team's laptop. Every number on screen comes
// from a recording: the snapshot the UI loaded at that moment, then every live
// message in the order and at the pace it arrived. Nothing is generated here; the
// only change is moving the clock so the recording happens "now".

export const REPLAY = import.meta.env.MODE === "replay";
export const REPO_URL: string = import.meta.env.VITE_REPO_URL ?? "";

export interface Chapter {
  scenario: string;
  label: string;
  file: string;
  result?: { pass?: boolean; mttd_s?: number | null; mttr_s?: number | null; actions?: string[]; root?: string | null };
}
interface Index { recorded_at: number; chapters: Chapter[] }
interface Tape {
  scenario: string;
  label: string;
  inject_ts: number;
  end_ts: number;
  snapshot: { state: StateResponse; history: Point[]; events: VigilEvent[]; incidents: Incident[]; health: Health; taken_at: number };
  messages: Array<{ t: number; m: StreamMessage }>;
}
export interface Snapshot { state: StateResponse; history: Point[]; events: VigilEvent[]; incidents: Incident[] }
export type Phase = "loading" | "idle" | "playing" | "done";

/** Move every epoch-seconds timestamp by `offset`. Only numbers in the epoch range move. */
function shift<T>(value: T, offset: number): T {
  if (typeof value === "number") return (value > 1.5e9 && value < 2.5e9 ? value + offset : value) as T;
  if (Array.isArray(value)) return value.map((v) => shift(v, offset)) as T;
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = shift(v, offset);
    return out as T;
  }
  return value;
}

type MsgFn = (m: StreamMessage) => void;
type SnapFn = (s: Snapshot) => void;

class Player {
  index: Index | null = null;
  current: Chapter | null = null;
  phase: Phase = "loading";
  private tapes = new Map<string, Tape>();
  private timers: number[] = [];
  private msgFns = new Set<MsgFn>();
  private snapFns = new Set<SnapFn>();
  private stateFns = new Set<() => void>();
  private snap: Snapshot | null = null;
  private incidents = new Map<string, Incident>();
  private events: VigilEvent[] = [];
  private touched = false;
  startedAt = 0;
  injectAt = 0;

  async loadIndex(): Promise<Index> {
    if (this.index) return this.index;
    const r = await fetch("/replay/index.json", { cache: "no-store" });
    if (!r.ok) throw new Error("No recordings found in this build");
    this.index = (await r.json()) as Index;
    return this.index;
  }

  private async tape(ch: Chapter): Promise<Tape> {
    const hit = this.tapes.get(ch.scenario);
    if (hit) return hit;
    const r = await fetch(`/replay/${ch.file}`);
    if (!r.ok) throw new Error(`Recording for ${ch.label} is missing`);
    const t = (await r.json()) as Tape;
    this.tapes.set(ch.scenario, t);
    return t;
  }

  chapter(scenario: string): Chapter | undefined {
    return this.index?.chapters.find((c) => c.scenario === scenario);
  }

  onMessage(fn: MsgFn): () => void { this.msgFns.add(fn); return () => { this.msgFns.delete(fn); }; }
  onSnapshot(fn: SnapFn): () => void { this.snapFns.add(fn); return () => { this.snapFns.delete(fn); }; }
  onChange(fn: () => void): () => void { this.stateFns.add(fn); return () => { this.stateFns.delete(fn); }; }
  private changed() { for (const fn of this.stateFns) fn(); }

  /** The calm moment at the start of a recording, shifted to now. */
  private async stage(ch: Chapter): Promise<{ tape: Tape; snap: Snapshot; offset: number }> {
    const tape = await this.tape(ch);
    const offset = Date.now() / 1000 - tape.snapshot.taken_at;
    const s = shift(tape.snapshot, offset);
    const since = s.taken_at - 60;
    const snap: Snapshot = {
      // Only what happens in this recording: earlier incidents and events would mix stories.
      state: { ...s.state, open_incidents: [] },
      history: s.history,
      events: s.events.filter((e) => e.ts >= since && !e.incident_id),
      incidents: [],
    };
    return { tape, snap, offset };
  }

  async snapshot(): Promise<Snapshot> {
    if (this.snap) return this.snap;
    const idx = await this.loadIndex();
    const first = idx.chapters[0];
    const { snap } = await this.stage(first);
    this.current = first;
    this.setSnap(snap);
    this.phase = "idle";
    this.changed();
    return snap;
  }

  private setSnap(snap: Snapshot) {
    this.snap = snap;
    this.incidents = new Map(snap.incidents.map((i) => [i.id, i]));
    this.events = [...snap.events];
  }

  private clear() {
    for (const t of this.timers) window.clearTimeout(t);
    this.timers = [];
  }

  /** Play one recording from its calm start to its settled end. */
  async start(scenario: string, byUser = true): Promise<void> {
    if (byUser) this.touched = true;
    const ch = this.chapter(scenario);
    if (!ch) throw new Error(`No recording for ${scenario}`);
    this.clear();
    const { tape, snap, offset } = await this.stage(ch);
    this.current = ch;
    this.setSnap(snap);
    for (const fn of this.snapFns) fn(snap);
    this.phase = "playing";
    this.startedAt = Date.now();
    this.injectAt = (tape.inject_ts + offset) * 1000;
    this.changed();
    const t0 = tape.snapshot.taken_at;
    for (const { t, m } of tape.messages) {
      this.timers.push(window.setTimeout(() => this.emit(shift(m, offset)), Math.max(0, (t - t0) * 1000)));
    }
    this.timers.push(window.setTimeout(() => { this.phase = "done"; this.changed(); }, Math.max(0, (tape.end_ts - t0) * 1000)));
  }

  private emit(m: StreamMessage) {
    if (m.type === "event") this.events = [...this.events, m.event].slice(-400);
    if (m.type === "incident") this.incidents.set(m.incident.id, m.incident);
    if (m.type === "tick" && this.snap) this.snap = { ...this.snap, state: { ...this.snap.state, latest: m.point, verdicts: m.verdicts } };
    for (const fn of this.msgFns) fn(m);
  }

  /** Back to the calm start of the current recording, nothing playing. */
  async reset(): Promise<void> {
    this.touched = true;
    this.clear();
    const ch = this.current ?? (await this.loadIndex()).chapters[0];
    const { snap } = await this.stage(ch);
    this.setSnap(snap);
    for (const fn of this.snapFns) fn(snap);
    this.phase = "idle";
    this.changed();
  }

  /** Start the first recording by itself if the visitor hasn't picked one (off with ?autoplay=0). */
  autoplay(delayMs = 2500) {
    if (new URLSearchParams(window.location.search).get("autoplay") === "0") return;
    window.setTimeout(() => {
      if (!this.touched && this.phase === "idle" && this.index?.chapters[0]) void this.start(this.index.chapters[0].scenario, false);
    }, delayMs);
  }

  incident(id: string): { incident: Incident; events: VigilEvent[] } | null {
    const inc = this.incidents.get(id);
    return inc ? { incident: inc, events: this.events.filter((e) => e.incident_id === id) } : null;
  }

  incidentList(): Incident[] {
    return [...this.incidents.values()];
  }

  health(): Health | null {
    return this.snap?.state.health ?? null;
  }

  /** What Chaos Lab shows as "running": the fault of the recording being played. */
  chaosStatus(): Record<string, Record<string, number | boolean>> {
    const on = this.phase === "playing" && Date.now() >= this.injectAt ? this.current?.scenario : null;
    return {
      inventory: { latency_ms: on === "slow-inventory" ? 1500 : 0 },
      payment: { leak_mb_per_s: on === "payment-leak" ? 3 : 0, cpu_burn: on === "payment-runaway" },
      loadgen: { rps: on === "flash-sale" ? 120 : 20 },
      deploy: { v2: on === "bad-deploy" },
    };
  }
}

export const player = new Player();

export const SCENARIO_TEXT: Record<string, { description: string; target: string }> = {
  "slow-inventory": { description: "Inventory requests stall for 1.5 s. Payment starts timing out on it, but every health check stays green.", target: "inventory" },
  "payment-leak": { description: "Payment leaks 3 MB/s and would hit its 256 MB limit in about a minute.", target: "payment" },
  "flash-sale": { description: "Real demand: checkout traffic jumps 6x, more than one payment instance can serve. Nothing is broken.", target: "loadgen" },
  "bad-deploy": { description: "A developer ships payment v2 with a bug baked in: 40% of payments fail. A restart brings it back.", target: "payment" },
  "payment-runaway": { description: "A busy loop burns a core in payment. Traffic is flat, CPU per request jumps.", target: "payment" },
};
