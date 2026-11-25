// Relay architecture as a machine: a cam-drum sequencer bench feeding a sealed glovebox.
// Run: node build.mjs            -> relay-architecture.svg (standalone, dark)
//      node build.mjs --light    -> relay-architecture-light.svg
//      node build.mjs --html     -> also writes relay-architecture.html for screenshots
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as k from "./iso-kit.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const theme = process.argv.includes("--light") ? "light" : "dark";

const WIDTH = 1000;
const HEIGHT = 640;

// ---- the model (numbers from src/relay/core and src/relay/security) ----
const BUDGET = { usd: 1.0, tokens: 1_000_000, steps: 80, wall_s: 1800 }; // core/budget.py defaults
const SPENT = { usd: 0.42, tokens: 310_000, steps: 37, wall_s: 610 }; // a run paused at step 37
const STUCK = { window: 12, repeat: 3 }; // core/stuck.py
const AGENTS = ["coding", "review", "support", "meetings", "workflows"]; // config/policies/*.yaml
const LOADED = 0; // the coding cartridge is on the spindle
const fractions = ["usd", "tokens", "steps", "wall_s"].map((key) => SPENT[key] / BUDGET[key]);

// ---- world layout (x right along the bench, y toward the front, z up) ----
const PLATE = { x: -200, y: -98, w: 400, d: 188, r: 10 };
const PLATE_H = 10;
const FOOT_H = 5;

const MANIFOLD = { x: -186, y: -26, w: 30, d: 90, r: 3 };
const CHUTES = [-10, 18, 46]; // github, slack, cron

const DRUM = [-104, -8];
const DRUM_R = 20;
const DRUM_Z = 16;
const DRUM_TOP = 76;

const RACK = { x: -168, y: -92, w: 172, d: 36, r: 4 };
const SLOTS = [-148, -114, -80, -46, -12];
const CART_R = 14;

const GOV = [-40, -6];
const RAIL_Y = [33, 45];
const RAIL = { from: -156, to: 62 };
const SHUTTLE = { x: -36, w: 22 };
const GATE_X = 4;

const BOX = { x: 70, y: -66, w: 116, d: 88, r: 4 };
const BOX_Z = 12;
const BOX_TOP = 98;
const LOCK = { x: 60, y: 22, w: 46, d: 40, r: 3 };
const LOCK_TOP = 42;
const STACK = [160, -40];

const P = k.fitProjection(
  [...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0), [STACK[0], STACK[1], 156], [-171, CHUTES[0], 96], [GOV[0], GOV[1], 104]],
  WIDTH,
  HEIGHT,
  { pad: 70, azimuth: 52 },
);

const at = (point) => k.iso(point, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (points, style) => k.dotsSvg(points, style);
const byDepth = (points) => [...points].sort((a, b) => k.depthOf([a[0], a[1], 0], P) - k.depthOf([b[0], b[1], 0], P));
const V = k.towardViewer(P);
// rectangle outline on a face of constant y or constant x
const yRect = (y, x0, x1, z0, z1) => k.pathOf([at([x0, y, z0]), at([x1, y, z0]), at([x1, y, z1]), at([x0, y, z1])], true);
const xRect = (x, y0, y1, z0, z1) => k.pathOf([at([x, y0, z0]), at([x, y1, z0]), at([x, y1, z1]), at([x, y0, z1])], true);
// visible points on a vertical cylinder's side, faded toward the limb
const onDrum = (cx, cy, r, angle, z) => {
  const facing = Math.cos(angle) * V[0] + Math.sin(angle) * V[1];
  const p = at([cx + r * Math.cos(angle), cy + r * Math.sin(angle), z]);
  return facing > 0.05 ? [p[0], p[1], Math.min(1, (facing - 0.05) * 2.2)] : null;
};

function plate() {
  const out = [`<path class="iso-halo" d="${k.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(PLATE, 24))) {
    out.push(S(k.cylinder(x, y, 7, -PLATE_H - FOOT_H, 1.6, P, 28), { tone: "lo" }));
    out.push(S(k.cylinder(x, y, 5.4, -PLATE_H - FOOT_H + 1.6, FOOT_H - 1.6, P, 28), { tone: "mid" }));
  }
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.6), { tone: "mid", inner: L(k.sideSeam(PLATE, -4, P, 8), { tone: "faint" }) }));
  out.push(L(k.planOutline(k.insetPlan(PLATE, 5), 0, P), { tone: "lo" }));
  const screws = k.corners(PLATE, 11);
  out.push(L(screws.map(([x, y]) => k.ring(x, y, 2.2, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5 }));
  // ruler strip along the front edge: the run's steps
  const strip = { x: -40, y: PLATE.y + PLATE.d - 16, w: 224, d: 7, r: 1 };
  out.push(S(k.slabOf(strip, 0, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const ticks = k.topTicks(strip.x + 4, strip.x + strip.w - 4, 4, 5, strip.y + 0.8, 0.6, [2, 3.6], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(D([at([strip.x + 2, strip.y + 3.5, 0.6]), at([strip.x + strip.w - 2, strip.y + 3.5, 0.6])], { size: 0.45 }));
  // label plate with the one engraved word
  const label = { x: -186, y: PLATE.y + PLATE.d - 22, w: 46, d: 13, r: 1.4 };
  out.push(S(k.slabOf(label, 0, 0.8, P, 3), { tone: "lo", crease: "none" }));
  out.push(L(k.planOutline(k.insetPlan(label, 1.4), 0.8, P, 3), { tone: "faint" }));
  out.push(k.faceTextSvg(k.topMatrix([label.x + 4, label.y + 6.4], 0.8, P, "x"), "RELAY", { size: 5, tone: "mid" }));
  out.push(L([30, 20].map((w, i) => k.lineOnTop([label.x + 4, label.y + 8.6 + i * 2], [label.x + 4 + w, label.y + 8.6 + i * 2], 0.8, P)).join(""), { tone: "faint", free: true }));
  out.push(D([at([label.x + label.w - 3, label.y + 3, 0.8]), at([label.x + label.w - 3, label.y + label.d - 3, 0.8])], { size: 0.45 }));
  return out.join("");
}

// five interchangeable program cartridges; slot LOADED is empty (that one is on the spindle)
function rack() {
  const out = [`<path class="iso-halo" d="${k.haloOf(RACK, 0, 4, P)}"/>`];
  out.push(S(k.slabOf(RACK, 0, 6, P, 4, 0.8), { tone: "mid", inner: L(k.sideSeam(RACK, 2, P, 4), { tone: "faint" }) }));
  // back rail on two posts
  for (const x of [RACK.x + 4, RACK.x + RACK.w - 10]) out.push(S(k.slabOf({ x, y: RACK.y + 2, w: 6, d: 5, r: 1 }, 6, 44, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.slabOf({ x: RACK.x + 2, y: RACK.y + 1, w: RACK.w - 4, d: 7, r: 1.5 }, 50, 5, P, 3, 0.6), { tone: "mid" }));
  out.push(D([RACK.x + 7, RACK.x + RACK.w - 7].map((x) => at([x, RACK.y + 4.5, 55])), { size: 0.45 }));
  const cy = RACK.y + 22;
  SLOTS.forEach((x, index) => {
    out.push(L(k.ring(x, cy, CART_R + 2.5, 6, P, 40), { tone: "lo" }));
    out.push(D(k.pointsAt([[x - CART_R - 4.5, cy - 0, 6]], P), { size: 0.4 }));
    if (index === LOADED) {
      out.push(L(k.ring(x, cy, 3, 6, P, 16), { tone: "lo" }));
      return;
    }
    out.push(S(k.cylinder(x, cy, CART_R + 1.2, 6, 3, P, 40), { tone: "lo" }));
    out.push(S(k.cylinder(x, cy, CART_R, 9, 34, P, 48, 0.5), { tone: "mid", crease: "lo", inner: L(k.sideArc(x, cy, CART_R, 26, P, 32), { tone: "faint" }) }));
    out.push(S(k.cylinder(x, cy, CART_R + 1.2, 43, 3, P, 40, 0.4), { tone: "mid" }));
    // each cartridge has its own pin program
    const pins = [];
    for (let row = 0; row < 6; row++) for (let col = 0; col < 18; col++) {
      if ((row * 7 + col * (index + 3) + index) % 5 > 1) continue;
      const p = onDrum(x, cy, CART_R, (col / 18) * Math.PI * 2, 13 + row * 4.6);
      if (p) pins.push(p);
    }
    out.push(D(pins, { size: 0.55, tone: "lo" }));
    out.push(L(k.ring(x, cy, 3, 46, P, 16), { tone: "lo" }));
    out.push(D([at([x, cy, 46])], { size: 0.5 }));
  });
  return out.join("");
}

// three trigger chutes (GitHub, Slack, cron) dropping into one intake manifold
function intake() {
  const out = [`<path class="iso-halo" d="${k.haloOf(MANIFOLD, 0, 6, P)}"/>`];
  out.push(S(k.slabOf(MANIFOLD, 0, 16, P, 4, 0.8), { tone: "mid", inner: L(k.sideSeam(MANIFOLD, 4, P, 4), { tone: "faint" }) }));
  out.push(L(yRect(MANIFOLD.y + MANIFOLD.d, MANIFOLD.x + 8, MANIFOLD.x + 22, 7, 12), { tone: "lo" }));
  out.push(D([at([MANIFOLD.x + 4, MANIFOLD.y + 4, 16]), at([MANIFOLD.x + 26, MANIFOLD.y + 4, 16]), at([MANIFOLD.x + 26, MANIFOLD.y + MANIFOLD.d - 4, 16])], { size: 0.45 }));
  // output mouth toward the rail
  out.push(S(k.slabOf({ x: MANIFOLD.x + MANIFOLD.w, y: RAIL_Y[0] - 4, w: 6, d: RAIL_Y[1] - RAIL_Y[0] + 11, r: 1 }, 2, 12, P, 3, 0.4), { tone: "mid" }));
  CHUTES.forEach((y, index) => {
    const cx = MANIFOLD.x + 15;
    const tall = 74 - index * 8;
    out.push(S(k.slabOf({ x: cx - 10, y: y - 10, w: 20, d: 20, r: 2 }, 16, 4, P, 3, 0.4), { tone: "lo" }));
    out.push(S(k.slabOf({ x: cx - 7, y: y - 7, w: 14, d: 14, r: 1.4 }, 20, tall - 20, P, 3, 0.5), { tone: "mid", inner: L(k.sideSeam({ x: cx - 7, y: y - 7, w: 14, d: 14, r: 1.4 }, 34, P, 3), { tone: "faint" }) }));
    // hopper funnel: stepped flare
    out.push(S(k.slabOf({ x: cx - 9, y: y - 9, w: 18, d: 18, r: 1.6 }, tall, 6, P, 3, 0.4), { tone: "mid" }));
    out.push(S(k.slabOf({ x: cx - 12, y: y - 12, w: 24, d: 24, r: 2 }, tall + 6, 6, P, 3, 0.8), { tone: "mid" }));
    out.push(L(k.planOutline({ x: cx - 8, y: y - 8, w: 16, d: 16, r: 1.4 }, tall + 12, P, 3), { tone: "lo" }));
    // a token waiting in the mouth, one per trigger
    out.push(S(k.cylinder(cx, y, 4.2, tall + 12, 1.6, P, 20), { tone: "hi" }));
    // trigger-specific bit: a sight slot on the chute's front face
    out.push(L(k.pathOf(k.roundedPlan({ x: cx - 2, y: 28, w: 4, d: tall - 40, r: 2 }, 4).map(([u, z]) => at([u, y + 7, z])), true), { tone: "lo" }));
    out.push(D([at([cx - 7.6, y + 4, 18]), at([cx + 7.6, y - 4, 18])], { size: 0.4 }));
  });
  return out.join("");
}

// the runtime: one cam drum stepping through its program, a ratchet that never turns back
function drum() {
  const [x, y] = DRUM;
  const out = [`<path class="iso-halo" d="${k.haloOf({ x: x - 28, y: y - 28, w: 56, d: 56, r: 28 }, 0, 4, P)}"/>`];
  out.push(S(k.cylinder(x, y, 28, 0, 6, P, 64, 0.8), { tone: "lo" }));
  out.push(D(Array.from({ length: 8 }, (_, i) => (i / 8) * Math.PI * 2).map((a) => onDrum(x, y, 24.5, a, 6) && at([x + 24.5 * Math.cos(a), y + 24.5 * Math.sin(a), 6])).filter(Boolean), { size: 0.45 }));
  out.push(S(k.cylinder(x, y, 23, 6, 10, P, 64, 0.6), { tone: "mid", inner: L(k.sideArc(x, y, 23, 10, P, 40), { tone: "faint" }) }));
  // drum (the loaded cartridge) with pins = the run's program
  out.push(S(k.cylinder(x, y, DRUM_R, DRUM_Z, DRUM_TOP - DRUM_Z, P, 72, 0.8), {
    tone: "hi",
    crease: "lo",
    inner: L([28, 46, 64].map((z) => k.sideArc(x, y, DRUM_R, z, P, 48)).join(""), { tone: "faint" }),
  }));
  const pins = [];
  const lead = [];
  for (let row = 0; row < 11; row++) for (let col = 0; col < 24; col++) {
    if ((row * 5 + col * 3) % 7 > 2) continue;
    const p = onDrum(x, y, DRUM_R + 0.2, (col / 24) * Math.PI * 2, DRUM_Z + 4 + row * 5);
    if (p) (col === 4 ? lead : pins).push(p);
  }
  out.push(D(pins, { size: 0.75, tone: "mid" }), D(lead, { size: 0.85, tone: "hi" }));
  // ratchet wheel on top: 80 teeth = 80 steps
  out.push(S(k.cylinder(x, y, DRUM_R + 4, DRUM_TOP, 5, P, 80, 0.5), { tone: "mid" }));
  out.push(L(k.knurl(x, y, DRUM_R + 4, DRUM_TOP, DRUM_TOP + 5, BUDGET.steps, P, 0.3), { tone: "lo" }));
  const teeth = k.radialTicks(x, y, DRUM_R + 3.6, BUDGET.steps, 10, DRUM_TOP + 5, [1.4, 3], P);
  out.push(L(teeth.minor, { tone: "lo" }), L(teeth.major, { tone: "mid" }));
  out.push(L(k.ring(x, y, DRUM_R - 2, DRUM_TOP + 5, P, 64), { tone: "lo" }));
  // checkpoint index: a counter wheel and hub
  out.push(S(k.cylinder(x, y, 8, DRUM_TOP + 5, 4, P, 40, 0.4), { tone: "mid" }));
  out.push(L(k.knurl(x, y, 8, DRUM_TOP + 5, DRUM_TOP + 9, 28, P), { tone: "lo" }));
  out.push(S(k.cylinder(x, y, 3, DRUM_TOP + 9, 4, P, 20), { tone: "mid" }));
  const stepAngle = (SPENT.steps / BUDGET.steps) * Math.PI * 2;
  out.push(L(k.lineOnTop([x + Math.sin(stepAngle) * 9, y - Math.cos(stepAngle) * 9], [x + Math.sin(stepAngle) * (DRUM_R + 2), y - Math.cos(stepAngle) * (DRUM_R + 2)], DRUM_TOP + 5.1, P), { tone: "hi" }));
  return out.join("");
}

// pawl post beside the drum: lets it advance, never reverse (durable checkpoints)
function pawl() {
  const [x, y] = [DRUM[0] - 30, DRUM[1] + 14];
  const out = [];
  out.push(S(k.cylinder(x, y, 5, 0, 4, P, 24), { tone: "lo" }));
  out.push(S(k.cylinder(x, y, 3, 4, DRUM_TOP - 4, P, 20), { tone: "mid", crease: "none" }));
  out.push(S(k.slabOf({ x: x - 4, y: y - 4, w: 13, d: 8, r: 2 }, DRUM_TOP - 1, 4, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.slabOf({ x: x + 6, y: y - 2, w: 4, d: 4, r: 1 }, DRUM_TOP + 2, 3, P, 2), { tone: "hi" }));
  out.push(D([at([x, y, DRUM_TOP + 3])], { size: 0.55 }));
  return out.join("");
}

// cam follower: reads the drum and pushes the next tool call onto the rail
function follower() {
  const [x] = DRUM;
  const out = [];
  const py = 28;
  out.push(S(k.cylinder(x, py, 5, 0, 3, P, 24), { tone: "lo" }));
  out.push(S(k.cylinder(x, py, 2.6, 3, 33, P, 20), { tone: "mid", crease: "none" }));
  out.push(S(k.slabOf({ x: x - 3, y: DRUM[1] + DRUM_R + 2, w: 6, d: py - DRUM[1] - DRUM_R + 1, r: 1 }, 34, 4, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.cylinder(x, DRUM[1] + DRUM_R + 2.6, 2.6, 30, 10, P, 20), { tone: "hi" }));
  out.push(D([at([x, py, 38])], { size: 0.5 }));
  return out.join("");
}

// flyball governor: the budget; its sleeve drives the brake link to the drum
function governor() {
  const [x, y] = GOV;
  const out = [`<path class="iso-halo" d="${k.haloOf({ x: x - 16, y: y - 12, w: 32, d: 24, r: 3 }, 0, 4, P)}"/>`];
  const base = { x: x - 14, y: y - 10, w: 28, d: 20, r: 3 };
  out.push(S(k.slabOf(base, 0, 14, P, 4, 0.8), { tone: "mid", inner: L(k.sideSeam(base, 5, P, 4), { tone: "faint" }) }));
  out.push(D(k.corners(base, 3.5).map(([cx, cy]) => at([cx, cy, 14])), { size: 0.45 }));
  // bevel gear box
  out.push(S(k.cylinder(x, y, 8, 14, 8, P, 40, 0.5), { tone: "mid" }));
  out.push(L(k.knurl(x, y, 8, 18, 22, 40, P), { tone: "lo" }));
  out.push(S(k.cylinder(x, y, 2.4, 22, 68, P, 20), { tone: "mid", crease: "none" }));
  // sleeve that rises with speed
  const SLEEVE = 52;
  out.push(S(k.cylinder(x, y, 4.6, SLEEVE, 9, P, 28, 0.4), { tone: "mid" }));
  out.push(L(k.sideArc(x, y, 4.6, SLEEVE + 4.5, P, 20), { tone: "faint" }));
  // arms and balls
  const head = [x, y, 88];
  const arms = [];
  const balls = [];
  for (const side of [-1, 1]) {
    const bx = x + side * 17;
    arms.push(k.segment(head, [bx, y, 72], P), k.segment([x + side * 4.6, y, SLEEVE + 9], [x + side * 9.5, y, 88 - 16 * (9.5 / 17)], P));
    balls.push([bx, y]);
  }
  out.push(L(arms.join(""), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, 3.8, 86, 5, P, 24, 0.4), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, 2, 91, 6, P, 16), { tone: "mid" }));
  for (const [bx, by] of balls) {
    out.push(S(k.cylinder(bx, by, 6.2, 66, 4, P, 32), { tone: "hi", crease: "lo" }));
    out.push(S(k.cylinder(bx, by, 5, 70, 4, P, 32, 0.4), { tone: "hi", crease: "lo" }));
  }
  out.push(D(balls.map(([bx, by]) => at([bx, by, 74])), { size: 0.5 }));
  // brake link to the drum's brake shoe
  const linkY = DRUM[1] + 2;
  out.push(S(k.slabOf({ x: DRUM[0] + DRUM_R + 6, y: linkY - 2, w: x - 4.6 - DRUM[0] - DRUM_R - 6, d: 4, r: 1 }, SLEEVE + 2, 3, P, 2, 0.3), { tone: "mid" }));
  return out.join("");
}

function brakeShoe() {
  const [x, y] = DRUM;
  const out = [];
  // shoe pressed toward the drum flank, on a small pivot post
  out.push(S(k.slabOf({ x: x + DRUM_R + 0.6, y: y - 6, w: 5.4, d: 16, r: 1.2 }, 44, 16, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.cylinder(x + DRUM_R + 9, y + 2, 3, 0, 56, P, 20), { tone: "mid", crease: "none" }));
  out.push(S(k.cylinder(x + DRUM_R + 9, y + 2, 5, 0, 3, P, 24), { tone: "lo" }));
  out.push(D([at([x + DRUM_R + 3.3, y - 3, 60]), at([x + DRUM_R + 3.3, y + 7, 60])], { size: 0.4 }));
  return out.join("");
}

function rails() {
  const out = [];
  for (const y of RAIL_Y) {
    out.push(S(k.slabOf({ x: RAIL.from, y: y - 1.5, w: RAIL.to - RAIL.from, d: 3, r: 0.8 }, 0, 4, P, 3, 0.4), { tone: "mid" }));
    const bolts = [];
    for (let x = RAIL.from + 8; x < RAIL.to - 4; x += 24) bolts.push(at([x, y, 4]));
    out.push(D(bolts, { size: 0.4 }));
  }
  // sleepers between the rails
  for (let x = RAIL.from + 14; x < RAIL.to - 6; x += 24) out.push(L(k.planOutline({ x: x - 2, y: RAIL_Y[0] + 2, w: 4, d: RAIL_Y[1] - RAIL_Y[0] - 4, r: 0.8 }, 0, P, 2), { tone: "faint" }));
  return out.join("");
}

// the tool call in flight: the one lit part
function shuttle() {
  const out = [`<path class="iso-halo" d="${k.haloOf({ x: SHUTTLE.x, y: 30, w: SHUTTLE.w, d: 18, r: 2 }, 0, 3, P)}" style="filter:blur(3px)"/>`];
  const body = { x: SHUTTLE.x, y: RAIL_Y[0] - 3, w: SHUTTLE.w, d: RAIL_Y[1] - RAIL_Y[0] + 6, r: 2 };
  out.push(S(k.slabOf(body, 4, 6, P, 3, 0.5), { tone: "hi", lit: true }));
  out.push(S(k.slabOf({ x: SHUTTLE.x + 4, y: 34, w: 14, d: 10, r: 1.4 }, 10, 6, P, 3, 0.4), { tone: "hi", lit: true }));
  // key tooth that must match the gate's allowlist
  out.push(S(k.slabOf({ x: SHUTTLE.x + SHUTTLE.w - 2, y: 37.5, w: 6, d: 3, r: 0.6 }, 12, 2.5, P, 2), { tone: "hi", lit: true }));
  out.push(D([at([SHUTTLE.x + 6.5, 36.5, 16]), at([SHUTTLE.x + 6.5, 41.5, 16])], { size: 0.45, tone: "lo" }));
  return out.join("");
}

// the policy gate: posts, a drop blade, an allowlist card, a taint flag and an approval key lock
function gateBack() {
  const out = [];
  const post = { x: GATE_X, y: 22, w: 8, d: 6, r: 1 };
  out.push(S(k.slabOf({ x: GATE_X - 3, y: 19, w: 14, d: 12, r: 1.4 }, 0, 3, P, 3, 0.4), { tone: "lo" }));
  out.push(S(k.slabOf(post, 3, 57, P, 3, 0.5), { tone: "mid" }));
  return out.join("");
}

function gateFront() {
  const out = [];
  const post = { x: GATE_X, y: 50, w: 8, d: 6, r: 1 };
  // blade raised (tool allowed); it drops between the posts to stop a call
  const blade = { x: GATE_X + 2.6, y: 28, w: 2.8, d: 22, r: 0.4 };
  out.push(S(k.slabOf(blade, 26, 32, P, 2, 0.3), { tone: "mid" }));
  out.push(L(Array.from({ length: 4 }, (_, i) => k.segment([GATE_X + 5.4, 30 + i * 6, 26], [GATE_X + 5.4, 30 + i * 6, 58], P)).join(""), { tone: "faint" }));
  // crossbar and allowlist card (punched holes)
  out.push(S(k.slabOf({ x: GATE_X - 1, y: 20, w: 10, d: 38, r: 1.2 }, 58, 6, P, 3, 0.6), { tone: "mid" }));
  const card = { x: GATE_X - 0.4, y: 25, w: 8.8, d: 26, r: 0.6 };
  out.push(S(k.slabOf(card, 64, 0.8, P, 2), { tone: "hi", crease: "none" }));
  const holes = [];
  for (let i = 0; i < 9; i++) for (let j = 0; j < 2; j++) if ((i * 3 + j) % 4 !== 1) holes.push(at([card.x + 2.6 + j * 3.6, card.y + 2 + i * 2.75, 64.8]));
  out.push(D(holes, { size: 0.55, tone: "lo" }));
  // taint flag on a mast at the back: lowered = clean; raised = approval required
  out.push(S(k.cylinder(GATE_X + 4, 23, 1, 64, 18, P, 12), { tone: "mid", crease: "none" }));
  out.push(S(k.slabOf({ x: GATE_X + 3.4, y: 24, w: 1.2, d: 10, r: 0.3 }, 74, 7, P, 2), { tone: "mid" }));
  out.push(D([at([GATE_X + 4, 23, 82])], { size: 0.5 }));
  // front post with the approval lock
  out.push(S(k.slabOf({ x: GATE_X - 3, y: 47, w: 14, d: 12, r: 1.4 }, 0, 3, P, 3, 0.4), { tone: "lo" }));
  out.push(S(k.slabOf(post, 3, 55, P, 3, 0.5), { tone: "mid" }));
  const lock = { x: GATE_X - 1, y: 56, w: 10, d: 4, r: 1 };
  out.push(S(k.slabOf(lock, 26, 14, P, 3, 0.4), { tone: "mid" }));
  out.push(L(k.sideRing([GATE_X + 4, 60, 33], 3, "y", P, 24), { tone: "lo" }));
  out.push(L(k.sideRing([GATE_X + 4, 60, 33], 1.6, "y", P, 16), { tone: "lo" }));
  // the human approval key, in the lock
  out.push(S(k.slabOf({ x: GATE_X + 3.4, y: 60, w: 1.2, d: 5, r: 0.3 }, 32.2, 1.6, P, 2), { tone: "hi" }));
  out.push(S(k.slabOf({ x: GATE_X + 1.4, y: 65, w: 5.2, d: 5, r: 2 }, 30.6, 4.8, P, 4, 0.3), { tone: "hi" }));
  out.push(D([at([GATE_X + 4, 67.5, 35.4])], { size: 0.45, tone: "lo" }));
  out.push(D([at([GATE_X + 1, 58, 39]), at([GATE_X + 7, 58, 27])], { size: 0.4 }));
  return out.join("");
}

// sealed glovebox: the sandbox. Only way out is the egress filter stack on top.
function glovebox() {
  const out = [`<path class="iso-halo" d="${k.haloOf(BOX, 0, 8, P)}"/>`];
  // legs and plinth
  for (const [x, y] of byDepth(k.corners(BOX, 8))) out.push(S(k.slabOf({ x: x - 4, y: y - 4, w: 8, d: 8, r: 1 }, 0, BOX_Z, P, 3, 0.4), { tone: "lo" }));
  out.push(S(k.slabOf(k.insetPlan(BOX, -2), BOX_Z - 3, 4, P, 4, 0.6), { tone: "mid" }));
  out.push(S(k.slabOf(BOX, BOX_Z + 1, BOX_TOP - BOX_Z - 1, P, 4, 1.2), { tone: "mid", inner: L(k.sideSeam(BOX, BOX_TOP - 10, P, 4), { tone: "lo" }) }));
  const front = BOX.y + BOX.d;
  const side = BOX.x + BOX.w;
  // front window frame and glass
  const win = { x0: 108, x1: 178, z0: 26, z1: 82 };
  out.push(S(k.slabOf({ x: win.x0 - 4, y: front, w: win.x1 - win.x0 + 8, d: 2, r: 0.6 }, win.z0 - 4, win.z1 - win.z0 + 8, P, 2, 0.4), { tone: "mid" }));
  out.push(`<path class="iso-shade" data-shade="3" d="${yRect(front + 2, win.x0, win.x1, win.z0, win.z1)}" style="opacity:.55"/>`);
  out.push(L(yRect(front + 2, win.x0, win.x1, win.z0, win.z1), { tone: "lo" }));
  out.push(L(k.segment([win.x0 + 6, front + 2, win.z1], [win.x0 + 30, front + 2, win.z0], P) + k.segment([win.x0 + 14, front + 2, win.z1], [win.x0 + 38, front + 2, win.z0], P), { tone: "faint" }));
  const frameBolts = [];
  for (let x = win.x0; x <= win.x1 + 0.1; x += 10) frameBolts.push(at([x, front + 2, win.z1 + 2]), at([x, front + 2, win.z0 - 2]));
  out.push(D(frameBolts, { size: 0.4 }));
  // two glove ports with rings and gloves hanging into the box
  for (const gx of [124, 162]) {
    const c = [gx, front + 2, 52];
    out.push(L(k.sideRing(c, 10, "y", P, 40), { tone: "mid" }));
    out.push(L(k.sideRing(c, 8, "y", P, 40), { tone: "lo" }));
    out.push(D(Array.from({ length: 8 }, (_, i) => (i / 8) * Math.PI * 2).map((a) => at([gx + 9 * Math.cos(a), front + 2, 52 + 9 * Math.sin(a)])), { size: 0.4 }));
    out.push(`<path class="iso-shade" data-shade="1" d="${k.sideRing(c, 8, "y", P, 40)}" style="opacity:.8"/>`);
  }
  // side face: service panel with louvres
  out.push(L(xRect(side, BOX.y + 12, BOX.y + 52, 24, 78), { tone: "lo" }));
  out.push(L(Array.from({ length: 9 }, (_, i) => k.pathOf(k.roundedPlan({ x: BOX.y + 18, y: 30 + i * 5.4, w: 28, d: 1.6, r: 0.8 }, 3).map(([u, z]) => at([side, u, z])), true)).join(""), { tone: "faint" }));
  out.push(D([[BOX.y + 15, 27], [BOX.y + 49, 27], [BOX.y + 15, 75], [BOX.y + 49, 75]].map(([u, z]) => at([side, u, z])), { size: 0.45 }));
  // top: lid groove, bolt row, pressure gauge
  out.push(L(k.planOutline(k.insetPlan(BOX, 5), BOX_TOP, P, 4), { tone: "lo" }));
  const lidBolts = [];
  for (let x = BOX.x + 10; x < side - 6; x += 12) lidBolts.push([x, BOX.y + 2.5], [x, front - 2.5]);
  out.push(D(lidBolts.map(([x, y]) => at([x, y, BOX_TOP])), { size: 0.4 }));
  const g = [96, -2];
  out.push(S(k.cylinder(g[0], g[1], 5, BOX_TOP, 4, P, 24), { tone: "mid" }));
  out.push(S(k.cylinder(g[0], g[1], 11, BOX_TOP + 4, 5, P, 56, 0.6), { tone: "hi", crease: "lo" }));
  out.push(L(k.ring(g[0], g[1], 9.4, BOX_TOP + 9, P, 48), { tone: "lo" }));
  const gt = k.radialTicks(g[0], g[1], 9, 24, 4, BOX_TOP + 9, [1.2, 2.4], P, -Math.PI * 0.75, Math.PI * 1.5);
  out.push(L(gt.minor, { tone: "lo" }), L(gt.major, { tone: "mid" }));
  const ga = -Math.PI * 0.75 + Math.PI * 1.5 * 0.18; // slight negative pressure: sealed
  out.push(L(k.lineOnTop([g[0] - Math.sin(ga) * 2, g[1] + Math.cos(ga) * 2], [g[0] + Math.sin(ga) * 7.6, g[1] - Math.cos(ga) * 7.6], BOX_TOP + 9.1, P), { tone: "hi", free: "start" }));
  out.push(D([at([g[0], g[1], BOX_TOP + 9])], { size: 0.6, tone: "hi" }));
  return out.join("");
}

// egress proxy: the one exit, a filter canister with an allowlist valve
function egress() {
  const [x, y] = STACK;
  const out = [];
  out.push(S(k.cylinder(x, y, 13, BOX_TOP, 3, P, 56, 0.5), { tone: "mid" }));
  out.push(D(Array.from({ length: 10 }, (_, i) => (i / 10) * Math.PI * 2).map((a) => onDrum(x, y, 11, a, BOX_TOP + 3) && at([x + 11 * Math.cos(a), y + 11 * Math.sin(a), BOX_TOP + 3])).filter(Boolean), { size: 0.4 }));
  out.push(S(k.cylinder(x, y, 9, BOX_TOP + 3, 30, P, 56, 0.6), {
    tone: "mid",
    inner: L([6, 10, 14, 18, 22, 26].map((h) => k.sideArc(x, y, 9, BOX_TOP + 3 + h, P, 32)).join(""), { tone: "faint" }),
  }));
  out.push(S(k.cylinder(x, y, 10.4, BOX_TOP + 33, 3, P, 56, 0.5), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, 4, BOX_TOP + 36, 8, P, 28), { tone: "mid", crease: "none" }));
  // valve handwheel
  out.push(S(k.cylinder(x, y, 9, BOX_TOP + 44, 2.2, P, 48, 0.3), { tone: "mid" }));
  out.push(L([0, 1, 2].map((i) => {
    const a = (i / 3) * Math.PI * 2 + 0.4;
    return k.lineOnTop([x + Math.cos(a) * 3.2, y + Math.sin(a) * 3.2], [x + Math.cos(a) * 7.6, y + Math.sin(a) * 7.6], BOX_TOP + 46.2, P);
  }).join(""), { tone: "lo" }));
  out.push(L(k.ring(x, y, 7.6, BOX_TOP + 46.2, P, 40), { tone: "lo" }));
  out.push(S(k.cylinder(x, y, 3, BOX_TOP + 46.2, 6, P, 24), { tone: "mid" }));
  // vent cap
  out.push(S(k.cylinder(x, y, 6.4, BOX_TOP + 52.2, 2.4, P, 32, 0.4), { tone: "mid" }));
  out.push(D([at([x, y, BOX_TOP + 54.6])], { size: 0.5 }));
  return out.join("");
}

// airlock pass box on the glovebox front: the rail enters here, double doors interlocked
function airlock() {
  const out = [`<path class="iso-halo" d="${k.haloOf(LOCK, 0, 4, P)}"/>`];
  out.push(S(k.slabOf(LOCK, 0, LOCK_TOP, P, 4, 0.8), { tone: "mid", inner: L(k.sideSeam(LOCK, 6, P, 4), { tone: "faint" }) }));
  const front = LOCK.y + LOCK.d;
  const side = LOCK.x + LOCK.w;
  // inspection port on the front
  const c = [LOCK.x + 24, front, 24];
  out.push(L(k.sideRing(c, 8, "y", P, 36), { tone: "mid" }));
  out.push(`<path class="iso-shade" data-shade="3" d="${k.sideRing(c, 6.4, "y", P, 36)}" style="opacity:.6"/>`);
  out.push(L(k.sideRing(c, 6.4, "y", P, 36), { tone: "lo" }));
  out.push(D(Array.from({ length: 6 }, (_, i) => (i / 6) * Math.PI * 2 + 0.5).map((a) => at([c[0] + 7.2 * Math.cos(a), front, c[2] + 7.2 * Math.sin(a)])), { size: 0.4 }));
  // door seam on the side face where it meets the box
  out.push(L(xRect(side, LOCK.y + 6, LOCK.y + LOCK.d - 6, 6, LOCK_TOP - 6), { tone: "lo" }));
  out.push(D([[LOCK.y + 9, 10], [LOCK.y + 9, LOCK_TOP - 10]].map(([u, z]) => at([side, u, z])), { size: 0.5 }));
  // interlock: two levers on top, one per door; only one may be open
  for (const [lx, up] of [[LOCK.x + 10, true], [LOCK.x + 34, false]]) {
    out.push(S(k.slabOf({ x: lx - 4, y: LOCK.y + 14, w: 8, d: 12, r: 1.2 }, LOCK_TOP, 4, P, 3, 0.4), { tone: "mid" }));
    out.push(S(k.slabOf({ x: lx - 1, y: LOCK.y + 19, w: 2, d: up ? 2 : 10, r: 0.6 }, LOCK_TOP + 4, up ? 12 : 2, P, 2), { tone: "mid" }));
    out.push(S(k.cylinder(lx, up ? LOCK.y + 20 : LOCK.y + 29, 2, up ? LOCK_TOP + 16 : LOCK_TOP + 4, 3, P, 16), { tone: "hi" }));
  }
  out.push(S(k.slabOf({ x: LOCK.x + 14, y: LOCK.y + 19, w: 16, d: 2, r: 0.6 }, LOCK_TOP, 2, P, 2), { tone: "lo" }));
  return out.join("");
}

// run ledger: a two-reel tape deck that records every step and tool call (Postgres ledger + events)
function ledger() {
  const deck = { x: 116, y: 40, w: 66, d: 36, r: 3 };
  const out = [`<path class="iso-halo" d="${k.haloOf(deck, 0, 4, P)}"/>`];
  out.push(S(k.slabOf(deck, 0, 12, P, 4, 0.8), { tone: "mid", inner: L(k.sideSeam(deck, 4, P, 4), { tone: "faint" }) }));
  out.push(L(k.planOutline(k.insetPlan(deck, 2.5), 12, P, 4), { tone: "faint" }));
  out.push(D(k.corners(deck, 4).map(([x, y]) => at([x, y, 12])), { size: 0.45 }));
  const front = deck.y + deck.d;
  out.push(L(Array.from({ length: 6 }, (_, i) => k.pathOf(k.roundedPlan({ x: deck.x + 8 + i * 4, y: 3, w: 1.6, d: 6, r: 0.8 }, 3).map(([u, z]) => at([u, front, z])), true)).join(""), { tone: "faint" }));
  const reels = [[deck.x + 16, deck.y + 17], [deck.x + 48, deck.y + 17]];
  const R = 10;
  // tape between the reels, running past the record head
  const wounds = [4.2, 7.4]; // 37 of 80 steps written: supply reel thin, take-up reel full
  // record head just behind the tape path
  out.push(S(k.slabOf({ x: deck.x + 28, y: deck.y + 17.6, w: 8, d: 4.4, r: 1 }, 12, 6, P, 3, 0.4), { tone: "mid" }));
  out.push(D([at([deck.x + 32, deck.y + 19.8, 18])], { size: 0.45 }));
  for (const [i, [x, y]] of reels.entries()) {
    const wound = wounds[i];
    out.push(S(k.cylinder(x, y, R, 12, 1.4, P, 56, 0.4), { tone: "mid" }));
    out.push(L(k.ring(x, y, R - 1.2, 13.4, P, 48), { tone: "lo" }));
    out.push(L([0, 1, 2].map((s) => { const a = (s / 3) * Math.PI * 2 + 0.3; return k.lineOnTop([x + Math.cos(a) * wound, y + Math.sin(a) * wound], [x + Math.cos(a) * (R - 1.2), y + Math.sin(a) * (R - 1.2)], 13.4, P); }).join(""), { tone: "faint" }));
    out.push(S(k.cylinder(x, y, wound, 13.4, 1.6, P, 40), { tone: "hi", crease: "lo" }));
    out.push(S(k.cylinder(x, y, 2.2, 15, 3, P, 20), { tone: "mid" }));
  }
  // the tape: one span, rim to rim, brushing the head's front face
  out.push(L(k.lineOnTop([reels[0][0], reels[0][1] + wounds[0]], [reels[1][0], reels[1][1] + wounds[1]], 15, P), { tone: "lo" }));
  return out.join("");
}

// budget meters: $, tokens, steps, wall time
function budgetPanel() {
  const panel = { x: -150, y: 58, w: 92, d: 24, r: 3 };
  const out = [`<path class="iso-halo" d="${k.haloOf(panel, 0, 4, P)}"/>`];
  out.push(S(k.slabOf(panel, 0, 10, P, 4, 0.8), { tone: "mid", inner: L(k.sideSeam(panel, 3, P, 4), { tone: "faint" }) }));
  out.push(L(k.planOutline(k.insetPlan(panel, 2.5), 10, P, 4), { tone: "faint" }));
  fractions.forEach((f, i) => {
    const [x, y] = [panel.x + 13 + i * 22, panel.y + 12];
    out.push(S(k.cylinder(x, y, 8.6, 10, 3.6, P, 48, 0.5), { tone: "hi", crease: "lo" }));
    out.push(L(k.ring(x, y, 7.4, 13.6, P, 40), { tone: "lo" }));
    const t = k.radialTicks(x, y, 7, 20, 5, 13.6, [1, 2], P, -Math.PI * 0.75, Math.PI * 1.5);
    out.push(L(t.minor, { tone: "lo" }), L(t.major, { tone: "mid" }));
    // red-line at 100%
    const end = Math.PI * 0.75;
    out.push(L(k.lineOnTop([x + Math.sin(end) * 4.2, y - Math.cos(end) * 4.2], [x + Math.sin(end) * 7, y - Math.cos(end) * 7], 13.6, P), { tone: "hi" }));
    const a = -Math.PI * 0.75 + Math.PI * 1.5 * f;
    out.push(L(k.lineOnTop([x - Math.sin(a) * 1.6, y + Math.cos(a) * 1.6], [x + Math.sin(a) * 6, y - Math.cos(a) * 6], 13.7, P), { tone: "hi", free: true }));
    out.push(D([at([x, y, 13.7])], { size: 0.55, tone: "hi" }));
  });
  // cut-off rocker at the right end
  out.push(S(k.slabOf({ x: panel.x + panel.w - 4, y: panel.y + 6, w: 3, d: 12, r: 0.8 }, 10, 3, P, 2), { tone: "lo" }));
  return out.join("");
}

// stuck detector: a 12-slot window of recent actions; 3 identical pegs trip the lever
function stuckDetector() {
  const box = { x: -40, y: 60, w: 32, d: 22, r: 2 };
  const out = [`<path class="iso-halo" d="${k.haloOf(box, 0, 4, P)}"/>`];
  out.push(S(k.slabOf(box, 0, 12, P, 3, 0.8), { tone: "mid", inner: L(k.sideSeam(box, 4, P, 3), { tone: "faint" }) }));
  const pegs = [];
  for (let i = 0; i < STUCK.window; i++) {
    const col = i % 6;
    const row = Math.floor(i / 6);
    const sx = box.x + 3.5 + col * 4.4;
    const sy = box.y + 4 + row * 7;
    out.push(L(k.planOutline({ x: sx, y: sy, w: 3, d: 5, r: 0.8 }, 12, P, 3), { tone: "lo" }));
    pegs.push([sx + 1.5, sy + 2.5, i >= STUCK.window - STUCK.repeat ? "same" : i % 3 ? "a" : "b"]);
  }
  for (const [px, py, kind] of pegs) {
    if (kind === "same") out.push(S(k.cylinder(px, py, 1.2, 12, 3, P, 12), { tone: "hi" }));
    else out.push(D([at([px, py, 12])], { size: kind === "a" ? 0.6 : 0.4, tone: "lo" }));
  }
  // trip lever on the side, thrown
  out.push(S(k.cylinder(box.x + box.w + 2, box.y + 11, 2.4, 4, 3, P, 16), { tone: "mid" }));
  out.push(S(k.slabOf({ x: box.x + box.w + 1, y: box.y + 10, w: 2, d: 2, r: 0.5 }, 7, 14, P, 2), { tone: "mid" }));
  out.push(S(k.cylinder(box.x + box.w + 2, box.y + 11, 2.2, 21, 3, P, 16), { tone: "hi" }));
  return out.join("");
}

const art = [
  plate(),
  rack(),
  intake(),
  pawl(),
  drum(),
  brakeShoe(),
  governor(),
  follower(),
  glovebox(),
  egress(),
  gateBack(),
  rails(),
  shuttle(),
  gateFront(),
  airlock(),
  ledger(),
  budgetPanel(),
  stuckDetector(),
];

// card frame drawn into the SVG so it stands alone in an <img>
const mono = "font-family:var(--anatomy-mono, ui-monospace, monospace);font-size:13px;letter-spacing:.04em";
const corner = (x, y, text, anchor = "start", fill = "var(--anatomy-muted)") => `<text x="${x}" y="${y}" text-anchor="${anchor}" style="${mono};fill:${fill}">${text}</text>`;
const card = [
  `<rect x="0.5" y="0.5" width="${WIDTH - 1}" height="${HEIGHT - 1}" rx="14" style="fill:var(--anatomy-card);stroke:var(--anatomy-faint);stroke-width:1"/>`,
  corner(22, 34, "Fig 1"),
  corner(WIDTH - 22, 34, "Relay &#183; one runtime, five agents", "end", "var(--anatomy-text)"),
  corner(22, HEIGHT - 22, "triggers &#8594; sequencer &#8594; policy gate &#8594; sealed sandbox"),
  corner(WIDTH - 22, HEIGHT - 22, `step ${SPENT.steps}/${BUDGET.steps} &#183; $${SPENT.usd.toFixed(2)}/$${BUDGET.usd.toFixed(2)} &#183; cartridge: ${AGENTS[LOADED]}`, "end"),
];

const label =
  "Relay drawn as a machine on a bench. Three trigger chutes drop tokens into an intake manifold. A pinned cam drum, one of five interchangeable cartridges kept in a rack behind it, steps the run forward on a ratchet that never turns back. A follower pushes each tool call, the lit shuttle, along a rail through a policy gate with an allowlist card, a taint flag and a human-approval key lock. A flyball governor with four budget meters can brake the drum, and a twelve-slot stuck detector trips when three identical actions repeat. The rail ends in an airlock on a sealed glovebox whose only exit is a filtered egress stack on top.";

const svg = k.figureSvg({ width: WIDTH, height: HEIGHT, label, body: [...card, ...art], standalone: theme });
const name = theme === "light" ? "relay-architecture-light" : "relay-architecture";
writeFileSync(join(HERE, `${name}.svg`), svg);
if (process.argv.includes("--html")) {
  writeFileSync(join(HERE, `${name}.html`), `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:#111}</style></head><body><img src="${name}.svg" width="${WIDTH}" height="${HEIGHT}"></body></html>`);
}
console.log("wrote", `${name}.svg`);
