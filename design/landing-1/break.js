/* "Your turn", the landing pad run's scene where a visitor breaks any robot at any hour.

   The answer comes from one of two places, said on screen:
   - live: a Moonwright backend on this machine (`uv run moonwright serve`), POST /simulate,
     the replan computed just now by CP-SAT; tried only when the page itself is served from
     localhost, with a 600 ms health check;
   - precomputed: data/landing_pad.failures.json, all 504 breakdowns made ahead with the same code.
   The 3D view replays the robots' hourly positions when the answer carries them (live, or a
   recorded case); otherwise it shows where the verified schedule puts every robot at that hour.

   Names the demo video drives (keep them): the select "Robot to break", the input "Hour of the
   failure", the button "Break it", and #break-result, whose text says "Finished at 72 h",
   "No replan" or "nothing to replan". */

const API = 'http://127.0.0.1:8074';
const MAKESPAN = 72;
const TASKS = { T1: 'survey the pad', T2: 'survey the berm', T3: 'dig the pad, part 1', T4: 'dig the pad, part 2', T5: 'haul to the berm, part 1', T6: 'haul to the berm, part 2', T7: 'compact the pad', T8: 'compact the berm', T9: 'sinter the pad', T10: 'sinter the berm crest', T11: 'inspect the pad', T12: 'inspect the berm' };
// type, the spare of the same type (if any), the hour its last task ends (null: it has none)
const ROBOTS = { EX1: ['excavator', 'EX2', 21], EX2: ['excavator', null, null], HL1: ['hauler', null, null], HL2: ['hauler', 'HL1', 31], CP1: ['compactor', null, 44], PR1: ['printer', null, 71], SV1: ['surveyor', null, 72] };
const ORDER = ['EX1', 'EX2', 'HL1', 'HL2', 'CP1', 'PR1', 'SV1'];
const RECORDED = { 'EX1@10': 'data/landing_pad.ex1-at-10.sim.json', 'HL2@24': 'data/landing_pad.hl2-at-24.sim.json' };
const STATUS = ['idle', 'moving', 'working', 'failed', 'done'];

const $ = (id) => document.getElementById(id);
const root = document.querySelector('[data-run="landing_pad"] [data-scene="break"]');
const calm = matchMedia('(prefers-reduced-motion: reduce)');
let table = null, plan = null, live = false, framesFile = null, framesReady = null;
let dataReady = null; // the table, the plan and the live check
let view = null, viewReady = null, awake = false;
let shown = null; // the frames and row the 3D view should show: the last breakdown, or the plan
let seq = 0; // the latest press of "Break it"; an older one never overwrites it

if (root) {
  // listen before anything loads, so a quick "Next" or an early "Break it" is never missed
  addEventListener('mw:scene', (e) => onScene(e.detail));
  $('bk-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const h = Math.max(0, Math.min(71, Math.round(+$('bk-hour').value || 0)));
    $('bk-hour').value = h;
    breakIt($('bk-robot').value, h);
  });
  $('bk-map').addEventListener('click', (e) => {
    const c = e.target.closest('.cell');
    if (!c) return;
    $('bk-robot').value = c.dataset.r;
    $('bk-hour').value = c.dataset.h;
    breakIt(c.dataset.r, +c.dataset.h);
  });
  $('bk-play').addEventListener('click', () => view && view.toggle());
  dataReady = loadData();
  // the page may already be open on this scene, or on the one before it
  const open = root.closest('.runview').hidden ? null : root.parentElement.querySelector('.scene:not([hidden])');
  if (open) onScene({ run: 'landing_pad', scene: open.dataset.scene });
}

async function loadData() {
  const [t, p] = await Promise.all([
    fetch('data/landing_pad.failures.json').then((r) => r.json()),
    fetch('data/landing_pad.plan.json').then((r) => r.json()),
  ]).catch(() => [null, null]);
  if (!t) { $('bk-line').textContent = 'The breakdown table could not load. Open the site over http, not as a file.'; return false; }
  table = new Map(t.rows.map((r) => [r.robot + '@' + r.t_h, r]));
  plan = p;
  buildMap();
  live = await probe();
  return true;
}

function onScene({ run, scene }) {
  const here = run === 'landing_pad' && scene === 'break';
  // the 3D view is built one scene early, while the visitor watches the Isaac Sim clip,
  // so the first "Break it" plays at once; it renders only while this scene is shown
  if (run === 'landing_pad' && (scene === 'failure' || here)) { ensureView(); loadFrames(); }
  awake = here;
  if (here) ensureView().then((v) => { if (v && awake) v.wakeUp(); });
  else if (view) view.sleep();
}

function ensureView() {
  if (!viewReady) {
    viewReady = dataReady
      .then((ok) => (ok ? createView($('bk-3d')) : null))
      .then((v) => {
        view = v;
        if (v) v.show(...(shown || [snapshot(10, null), null]));
        return v;
      })
      .catch((e) => {
        const note = $('bk-note');
        if (note) note.textContent = 'The 3D view needs WebGL; the map below has every breakdown.';
        console.warn(e);
        return null;
      });
  }
  return viewReady;
}

/* ---------- where the answer comes from ---------- */
async function probe() {
  if (!['127.0.0.1', 'localhost'].includes(location.hostname)) return false;
  try {
    const c = new AbortController();
    setTimeout(() => c.abort(), 600);
    return (await fetch(API + '/health', { signal: c.signal })).ok;
  } catch { return false; }
}
async function simulateLive(robot, h) {
  const c = new AbortController();
  setTimeout(() => c.abort(), 4000);
  const r = await fetch(API + '/simulate', {
    method: 'POST', signal: c.signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan, failures: [{ robot, t_h: h }], brief_id: 'landing_pad', frame_stride: 4 }),
  });
  if (!r.ok) throw new Error('simulate ' + r.status);
  return r.json();
}
// a SimResult, read into the same row shape as the precomputed table
function rowFromSim(res, robot, h) {
  const ev = (res.events || []).find((e) => e.kind === 'robot_failure' && e.robot === robot);
  const m = /\((\d+)% done\)/.exec(ev?.detail || '');
  const row = { robot, t_h: h, task: ev?.task || null, progress: m ? +m[1] / 100 : 0 };
  if (!res.replans || !res.replans.length) return { ...row, outcome: 'no_effect', finished_at_h: res.finished_at_h, delay_h: (res.finished_at_h ?? MAKESPAN) - MAKESPAN };
  const rec = res.replans[0], steps = rec.after?.steps || [], msg = steps.length ? steps[steps.length - 1].message || '' : '';
  const solve = /in ([\d.]+) s/.exec(msg);
  const base = { remaining: rec.remaining_tasks.length, solve_s: solve ? +solve[1] : null };
  if (rec.after?.success && res.completed) return { ...row, ...base, outcome: 'replanned', finished_at_h: res.finished_at_h, delay_h: res.finished_at_h - MAKESPAN };
  return { ...row, ...base, outcome: 'infeasible', finished_at_h: null, delay_h: null, core: msg.split('->')[0].match(/[a-z_]+:[A-Za-z0-9_>-]+/g) || [] };
}
// frames, in one shape: [{ t, robots: { id: { pos: [x, y], status, task } } }]
const norm = (frames) => frames.map((f) => ({ t: f.t_h, robots: Object.fromEntries(f.robots.map((r) => [r.id, { pos: r.pos, status: r.status, task: r.current_task }])) }));
// every breakdown's motion, 2.6 MB: fetched with the 3D view, one scene early
// { robots: [...], cases: { "HL2@24": [[t, [[x, y, status, task], ...]], ...] } }
function loadFrames() {
  if (!framesReady) framesReady = fetch('data/landing_pad.failures.frames.json').then((r) => (r.ok ? r.json() : null)).catch(() => null).then((j) => (framesFile = j));
  return framesReady;
}
async function precomputedFrames(robot, h) {
  await loadFrames();
  const c = framesFile?.cases?.[robot + '@' + h];
  if (c) return c.map(([t, rs]) => ({ t, robots: Object.fromEntries(rs.map(([x, y, s, task], i) => [framesFile.robots[i], { pos: [x, y], status: STATUS[s], task }])) }));
  const url = RECORDED[robot + '@' + h];
  return url ? norm((await (await fetch(url)).json()).frames) : null;
}
// no frames for this case: where the verified schedule puts every robot at hour h, frozen
function snapshot(h, broken) {
  const zones = Object.fromEntries(plan.requirements.site.zones.map((z) => [z.id, z.center]));
  const zoneOf = Object.fromEntries(plan.task_graph.tasks.map((t) => [t.id, t.zone]));
  const robots = Object.fromEntries(ORDER.map((id) => {
    const e = plan.schedule.entries.find((x) => x.robot === id && x.start_h <= h && h < x.end_h);
    const pos = e ? zones[zoneOf[e.task]] : plan.requirements.site.depot;
    return [id, { pos, status: id === broken ? 'failed' : e ? 'working' : 'idle', task: e ? e.task : null }];
  }));
  return [{ t: h, robots }, { t: h + 0.001, robots }];
}

/* ---------- breaking one ---------- */
async function breakIt(robot, h) {
  if (!(await dataReady)) return;
  const mine = ++seq;
  const go = $('bk-go');
  go.setAttribute('aria-busy', 'true');
  let row = null, frames = null, src = 'precomputed by the same replanner';
  if (live) {
    try {
      const res = await simulateLive(robot, h);
      row = rowFromSim(res, robot, h);
      frames = norm(res.frames || []);
      src = 'computed just now by CP-SAT';
    } catch { live = false; }
  }
  if (!row) row = table.get(robot + '@' + h);
  if (mine !== seq) return;
  go.removeAttribute('aria-busy');
  // the answer first, from the table or the live replan; the motion follows
  render(row, src);
  pick(robot, h);
  timeline(row);
  table3(row, src);
  if (!frames) frames = await precomputedFrames(robot, h).catch(() => null);
  if (mine !== seq) return;
  shown = [frames && frames.length > 1 ? frames : snapshot(h, robot), row];
  ensureView().then((v) => v && v.show(...shown)); // at once if built; else as soon as it is
}

function render(r, src) {
  const [type, spare, last] = ROBOTS[r.robot];
  const fig = $('bk-fig');
  const where = r.task ? `${r.robot} failed at ${r.t_h} h, ${Math.round(r.progress * 100)} % through ${r.task}, ${TASKS[r.task]}.` : `${r.robot} failed at ${r.t_h} h.`;
  const solve = r.solve_s == null ? 'in under 0.01 s' : `in ${r.solve_s} s`;
  let top, line;
  if (r.outcome === 'replanned') {
    const late = Math.round(r.delay_h);
    fig.classList.remove('is-word'); $('bk-n').textContent = Math.round(r.finished_at_h); $('bk-u').textContent = 'H';
    top = late > 0 ? `<span class="pill pill--neg">+${late} h</span>` : '<span class="pill pill--pos">No delay</span>';
    line = `${where} <b>Finished at ${Math.round(r.finished_at_h)} h</b>${late > 0 ? `, ${late} h late` : ', no delay'}. CP-SAT rescheduled ${r.remaining} tasks ${solve}; Nemotron not needed.${spare ? ` ${spare}, the other ${type}, takes over.` : ''}`;
  } else if (r.outcome === 'infeasible') {
    fig.classList.add('is-word'); $('bk-n').textContent = 'No replan'; $('bk-u').textContent = '';
    const lost = (r.core || []).map((c) => c.split(':')[1]).filter((t) => TASKS[t]);
    top = '<span class="pill pill--neg">No replan</span>';
    line = `${where} No robot left can ${lost.map((t) => `${TASKS[t]} (${t})`).join(', ') || 'do the rest'}: the plan cannot finish without a spare ${type}. <a href="#landing_pad/odds" data-go="odds">See what spares change</a>`;
  } else {
    fig.classList.remove('is-word'); $('bk-n').textContent = MAKESPAN; $('bk-u').textContent = 'H';
    top = '<span class="pill pill--none">Nothing to replan</span>';
    line = last == null
      ? `${r.robot} has no task in this plan: it is the spare ${type}, so nothing to replan. <b>Finished at ${MAKESPAN} h</b>.`
      : `${r.robot} finished its last task at ${last} h, so nothing to replan. <b>Finished at ${MAKESPAN} h</b>.`;
  }
  $('bk-top').innerHTML = top;
  $('bk-line').innerHTML = line;
  const s = $('bk-src');
  s.hidden = false;
  s.classList.toggle('is-live', src.startsWith('computed'));
  s.querySelector('span').textContent = src;
}

/* ---------- the map of all 504, the timeline, the numbers ---------- */
const kind = (r) => (r.outcome === 'no_effect' ? 'none' : r.outcome === 'infeasible' ? 'norep' : r.delay_h > 0 ? 'late' : 'ontime');
function buildMap() {
  $('bk-map').innerHTML = ORDER.map((id) => `<span class="map__robot">${id}</span><div class="map__row">${Array.from({ length: 72 }, (_, h) => {
    const r = table.get(id + '@' + h);
    return `<button type="button" class="cell" tabindex="-1" data-o="${kind(r)}" data-r="${id}" data-h="${h}" aria-label="${id} at ${h} h"></button>`;
  }).join('')}</div>`).join('') + '<div class="map__axis"><span style="left:0">0 h</span><span style="left:16.67%">12</span><span style="left:33.33%">24</span><span style="left:50%">36</span><span style="left:66.67%">48</span><span style="left:83.33%">60</span><span style="left:98.6%">71</span></div>';
}
function pick(robot, h) {
  document.querySelectorAll('#bk-map .is-picked').forEach((c) => c.classList.remove('is-picked'));
  document.querySelector(`#bk-map .cell[data-r="${robot}"][data-h="${h}"]`)?.classList.add('is-picked');
}
const pct = (h) => (h / 90 * 100).toFixed(2) + '%';
function timeline(r) {
  const h = r.t_h;
  let html;
  if (r.outcome === 'infeasible') {
    html = `<span class="tl__bar is-ink" style="width:${pct(h)}"></span><span class="tl__val is-neg-text" style="left:calc(${pct(h)} + 10px)">stops at ${h} h: no plan</span>`;
  } else {
    const f = r.finished_at_h ?? MAKESPAN;
    html = `<span class="tl__bar is-ink" style="width:${pct(Math.min(f, MAKESPAN))}"></span>`
      + (f > MAKESPAN ? `<span class="tl__bar is-neg" style="left:${pct(MAKESPAN)};width:${pct(f - MAKESPAN)}"></span>` : '')
      + `<span class="tl__val" style="left:calc(${pct(f)} + 8px)">${Math.round(f)} h</span>`;
  }
  $('bk-tl').innerHTML = html + `<span class="tl__x" style="left:${pct(h)}"></span><span class="tl__now" id="bk-now" style="left:${pct(h)}"></span>`;
}
function table3(r, src) {
  const rows = [
    ['Robot', `${r.robot}, ${ROBOTS[r.robot][0]}`],
    ['Fails at', `${r.t_h} h`],
    ['Working on', r.task ? `${r.task}, ${Math.round(r.progress * 100)} % done` : 'nothing'],
    ['Outcome', { replanned: 'replanned', infeasible: 'no replan', no_effect: 'nothing to replan' }[r.outcome]],
    ['Finished', r.finished_at_h == null ? 'no plan' : `${Math.round(r.finished_at_h)} h`],
    ['Delay', r.delay_h == null ? 'none possible' : `${Math.round(r.delay_h)} h`],
  ];
  if (r.remaining != null) rows.push(['Tasks rescheduled', String(r.remaining)], ['CP-SAT time', r.solve_s == null ? 'under 0.01 s' : `${r.solve_s} s`]);
  if (r.core?.length) rows.push(['Core', r.core.map((c) => `<code>${c}</code>`).join(' ')]);
  rows.push(['Source', src]);
  $('bk-table').innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td class="r">${v}</td></tr>`).join('');
}

/* ---------- the 3D view: built one scene early, asleep when this scene is not shown ---------- */
async function createView(box) {
  const [THREE, { GLTFLoader }, { DRACOLoader }, { OrbitControls }, { CSS2DRenderer, CSS2DObject }, { RoomEnvironment }] = await Promise.all([
    import('three'), import('three/addons/loaders/GLTFLoader.js'), import('three/addons/loaders/DRACOLoader.js'),
    import('three/addons/controls/OrbitControls.js'), import('three/addons/renderers/CSS2DRenderer.js'), import('three/addons/environments/RoomEnvironment.js'),
  ]);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.75));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  box.prepend(renderer.domElement);
  const labels = new CSS2DRenderer();
  Object.assign(labels.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
  box.appendChild(labels.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111111);
  scene.fog = new THREE.Fog(0x111111, 210, 360);
  const envMap = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
  const camera = new THREE.PerspectiveCamera(30, 1.5, 1, 1000);
  camera.position.set(28, 70, 110);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0, 15);
  controls.enableDamping = true;
  controls.enableZoom = false; // the page scrolls; turning the site is enough
  controls.maxPolarAngle = Math.PI * 0.46;

  scene.add(new THREE.HemisphereLight(0x9aa0aa, 0x0b0b0b, 0.35));
  const sun = new THREE.DirectionalLight(0xfff4e6, 3.4); // low in the east, as the sun sits at the pole
  sun.position.set(100, 24, 22);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -75, right: 75, top: 75, bottom: -75, near: 1, far: 300 });
  sun.shadow.bias = -0.0004;
  scene.add(sun);

  // LS-1, 80 x 80 m; a grid cell is 2 m; north is -z
  const W = (gx, gy) => new THREE.Vector3((gx - 20) * 2, 0, -(gy - 20) * 2);
  const zones = Object.fromEntries(plan.requirements.site.zones.map((z) => [z.id, W(...z.center)]));
  const Z1 = zones.Z1, Z2 = zones.Z2;
  const hash = (x, z) => { const s = Math.sin(x * 127.1 + z * 311.7) * 43758.5453; return s - Math.floor(s); };
  const vnoise = (x, z) => {
    const xi = Math.floor(x), zi = Math.floor(z), xf = x - xi, zf = z - zi, u = xf * xf * (3 - 2 * xf), v = zf * zf * (3 - 2 * zf);
    const a = hash(xi, zi), b = hash(xi + 1, zi), c = hash(xi, zi + 1), d = hash(xi + 1, zi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  };
  const fbm = (x, z) => { let f = 0, amp = 0.5, fr = 1; for (let o = 0; o < 5; o++) { f += amp * vnoise(x * fr, z * fr); amp *= 0.5; fr *= 2.03; } return f; };
  const ground = new THREE.PlaneGeometry(320, 320, 220, 220).rotateX(-Math.PI / 2);
  const gp = ground.attributes.position;
  const craters = [[-30, -8, 6], [34, 10, 4], [-18, 30, 3.5], [26, -40, 7], [-48, 44, 5], [10, 6, 2.5], [44, 46, 3]];
  for (let i = 0; i < gp.count; i++) {
    const x = gp.getX(i), z = gp.getZ(i);
    let y = 1.6 * (fbm(x * 0.018, z * 0.018) - 0.5) + 0.35 * (fbm(x * 0.09 + 7, z * 0.09 + 3) - 0.5);
    for (const [cx, cz, r] of craters) {
      const d = Math.hypot(x - cx, z - cz) / r;
      if (d < 1.4) y += d < 1 ? -0.09 * r * (1 - d * d) : 0.045 * r * ((1.4 - d) / 0.4) * ((d - 1) / 0.4);
    }
    const dp = Math.hypot(x - Z1.x, z - Z1.z), db = Math.hypot(x - Z2.x, z - Z2.z);
    if (dp < 14) y *= Math.min(1, Math.max(0, (dp - 10) / 4));
    if (db < 18) y *= Math.min(1, Math.max(0, (db - 14) / 4));
    gp.setY(i, y);
  }
  ground.computeVertexNormals();
  const regolith = new THREE.MeshStandardMaterial({ color: 0x7c7c7c, roughness: 1 });
  const groundMesh = new THREE.Mesh(ground, regolith);
  groundMesh.receiveShadow = true;
  scene.add(groundMesh);
  const bowl = new THREE.Mesh(new THREE.CircleGeometry(10, 96).rotateX(-Math.PI / 2), new THREE.MeshStandardMaterial({ color: 0x5a5a5a, roughness: 1 }));
  bowl.position.copy(Z1).setY(-0.38);
  bowl.receiveShadow = true;
  scene.add(bowl);
  const rim = new THREE.Mesh(new THREE.TorusGeometry(10.3, 0.45, 8, 96).rotateX(Math.PI / 2), regolith);
  rim.position.copy(Z1).setY(-0.1);
  rim.scale.y = 0.5;
  rim.castShadow = rim.receiveShadow = true;
  scene.add(rim);
  // the blast berm: a mound swept along a 40 m arc, 4 m at the base, 1.5 m high
  const arc = new THREE.CatmullRomCurve3(Array.from({ length: 13 }, (_, k) => {
    const a = Math.PI * (1.2 + 0.6 * k / 12);
    return new THREE.Vector3(Z2.x + 16 * Math.cos(a), 0, Z2.z + 16 * Math.sin(a) + 10);
  }));
  const NU = 80, NV = 18, bv = [], bi = [];
  for (let iu = 0; iu <= NU; iu++) {
    const t = iu / NU, p = arc.getPoint(t), tan = arc.getTangent(t), n = new THREE.Vector3(-tan.z, 0, tan.x).normalize();
    const taper = Math.min(1, Math.min(t, 1 - t) / 0.06);
    for (let iv = 0; iv <= NV; iv++) {
      const v = iv / NV * 2 - 1;
      bv.push(p.x + n.x * v * 2.6, 1.5 * taper * Math.pow(Math.cos(v * Math.PI / 2), 1.6) - 0.05, p.z + n.z * v * 2.6);
    }
  }
  for (let iu = 0; iu < NU; iu++) for (let iv = 0; iv < NV; iv++) { const a = iu * (NV + 1) + iv, b = a + NV + 1; bi.push(a, b, a + 1, b, b + 1, a + 1); }
  const bermGeo = new THREE.BufferGeometry();
  bermGeo.setAttribute('position', new THREE.Float32BufferAttribute(bv, 3));
  bermGeo.setIndex(bi);
  bermGeo.computeVertexNormals();
  const berm = new THREE.Mesh(bermGeo, new THREE.MeshStandardMaterial({ color: 0x8a8a8a, roughness: 1, side: THREE.DoubleSide }));
  berm.castShadow = berm.receiveShadow = true;
  scene.add(berm);
  const depot = new THREE.Mesh(new THREE.BoxGeometry(5, 2.2, 3.4), new THREE.MeshStandardMaterial({ color: 0xa8a8a8, roughness: 0.6, metalness: 0.3 }));
  depot.position.set(16, 1.1, 40);
  depot.castShadow = depot.receiveShadow = true;
  scene.add(depot);

  // NASA's RASSOR for every robot, with the role light the Isaac Sim clip uses
  const draco = new DRACOLoader().setDecoderPath('https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/libs/draco/gltf/');
  const gltf = await new GLTFLoader().setDRACOLoader(draco).loadAsync('assets/rassor.glb');
  const holder = new THREE.Group();
  holder.add(gltf.scene);
  const size = new THREE.Box3().setFromObject(holder).getSize(new THREE.Vector3());
  const S = 4.6 / Math.max(size.x, size.z);
  holder.scale.setScalar(S);
  gltf.scene.position.y -= new THREE.Box3().setFromObject(holder).min.y / S;
  const body = new THREE.MeshStandardMaterial({ color: 0xe6e3dd, roughness: 0.6, metalness: 0.2, envMap, envMapIntensity: 0.35 });
  holder.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; o.material = body; } });
  const height = new THREE.Box3().setFromObject(holder).max.y;
  const ROLE = { EX: 0xf2a33a, HL: 0x4aa3ff, CP: 0x4cd07d, SV: 0xf2f2f2, PR: 0xf2f2f2 };
  const metal = new THREE.MeshStandardMaterial({ color: 0x9a9a9a, roughness: 0.5, metalness: 0.5 });
  const bots = ORDER.map((id, i) => {
    const g = new THREE.Group();
    g.add(holder.clone());
    const kind = id.slice(0, 2);
    if (kind === 'PR') { // the printer: a gantry over the same chassis
      for (const sx of [-1.4, 1.4]) { const post = new THREE.Mesh(new THREE.BoxGeometry(0.18, 2.6, 0.18), metal); post.position.set(sx, 1.3, 0); post.castShadow = true; g.add(post); }
      const beam = new THREE.Mesh(new THREE.BoxGeometry(3, 0.2, 0.2), metal); beam.position.set(0, 2.6, 0); beam.castShadow = true; g.add(beam);
    }
    const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 0.9), metal);
    mast.position.set(0, height + 0.45, 0);
    g.add(mast);
    const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.4, 16, 12), new THREE.MeshBasicMaterial({ color: ROLE[kind] }));
    lamp.position.set(0, height + 0.95, 0);
    g.add(lamp);
    const glow = new THREE.PointLight(ROLE[kind], 14, 9, 1.6);
    glow.position.set(0, height + 0.6, 0);
    g.add(glow);
    const el = document.createElement('div');
    el.className = 'bk-label';
    const tag = new CSS2DObject(el);
    tag.position.set(0, height + 1.9, 0);
    g.add(tag);
    scene.add(g);
    return { id, i, g, lamp, glow, el, heading: Math.PI, base: ROLE[kind] };
  });
  $('bk-note').remove();

  /* the replay: hourly frames; drives between stops follow an S, work passes back and forth */
  let frames = [], trips = {}, t = 0, end = 0, from = 0, playing = false, awake = false, failure = null, outcome = null;
  const same = (p, q) => Math.abs(p[0] - q[0]) < 1e-6 && Math.abs(p[1] - q[1]) < 1e-6;
  const smoother = (u) => u * u * u * (u * (u * 6 - 15) + 10);
  function sCurve(A, B, u) {
    const d = B.clone().sub(A), n = new THREE.Vector3(-d.z, 0, d.x).normalize().multiplyScalar(d.length() * 0.16);
    const c = new THREE.CubicBezierCurve3(A, A.clone().addScaledVector(d, 1 / 3).add(n), A.clone().addScaledVector(d, 2 / 3).sub(n), B);
    return [c.getPoint(u), c.getTangent(u)];
  }
  function load(fr) {
    frames = fr;
    trips = Object.fromEntries(ORDER.map((id) => {
      const P = frames.map((f) => f.robots[id].pos), list = [];
      for (let k = 0; k < P.length - 1; k++) {
        if (same(P[k], P[k + 1])) continue;
        let j = k + 1;
        while (j < P.length - 1 && !same(P[j], P[j + 1])) j++;
        list.push({ ts: frames[k].t, te: frames[j].t, A: W(...P[k]), B: W(...P[j]) });
        k = j - 1;
      }
      return [id, list];
    }));
  }
  function frameAt(time) {
    let k = 0;
    while (k < frames.length - 2 && frames[k + 1].t <= time) k++;
    const a = frames[k], b = frames[Math.min(k + 1, frames.length - 1)];
    return [a, b, b.t > a.t ? Math.min(1, Math.max(0, (time - a.t) / (b.t - a.t))) : 0];
  }
  function place(time, snap) {
    if (!frames.length) return;
    const [a, b, u] = frameAt(time), wall = performance.now() / 1000;
    for (const bot of bots) {
      const ra = a.robots[bot.id], rb = b.robots[bot.id], cur = u < 0.5 ? ra : rb;
      const failed = cur.status === 'failed';
      const ang = bot.i / ORDER.length * Math.PI * 2 + 0.5;
      const slot = new THREE.Vector3(Math.cos(ang) * 10, 0, Math.sin(ang) * 10);
      const trip = trips[bot.id].find((q) => time >= q.ts && time <= q.te);
      let p;
      if (trip) {
        const [q, tan] = sCurve(trip.A, trip.B, smoother((time - trip.ts) / (trip.te - trip.ts)));
        p = q.add(slot);
        bot.heading = Math.atan2(tan.x, tan.z);
      } else {
        p = W(...ra.pos).add(slot);
        if (cur.status === 'working') {
          const dir = ang + Math.PI / 2, phase = time * 2.4 + bot.i;
          p.x += Math.cos(dir) * 2.2 * Math.sin(phase);
          p.z += Math.sin(dir) * 2.2 * Math.sin(phase);
          bot.heading = Math.atan2(Math.cos(dir), Math.sin(dir)) + (Math.cos(phase) < 0 ? Math.PI : 0);
        }
      }
      bot.g.position.copy(p);
      const r0 = bot.g.rotation.y, diff = Math.atan2(Math.sin(bot.heading - r0), Math.cos(bot.heading - r0));
      bot.g.rotation.set(failed ? 0.12 : 0, r0 + diff * (snap ? 1 : 0.18), failed ? 0.1 : 0);
      bot.lamp.material.color.setHex(failed ? 0xe5566f : bot.base);
      bot.glow.color.setHex(failed ? 0xe5566f : bot.base);
      const lit = failed || cur.status === 'working' || cur.status === 'moving' || !!trip;
      bot.lamp.visible = lit || cur.status === 'idle';
      bot.glow.intensity = lit ? (failed ? 22 : 14 + 6 * Math.sin(wall * 5 + bot.i)) : 0;
      bot.el.textContent = failed ? `${bot.id} broken` : `${bot.id}${cur.task && cur.status !== 'done' ? ' · ' + cur.task : ''}`;
      bot.el.classList.toggle('is-failed', failed);
    }
    $('bk-clock').textContent = 'Hour ' + Math.floor(time);
    const sub = !failure ? 'the plan as scheduled'
      : time < failure.t_h ? `${failure.robot} breaks down at hour ${failure.t_h}`
      : time >= end - 0.01 ? (outcome === 'infeasible' ? 'the plan cannot finish' : `done at hour ${Math.round(end)}`)
      : `${failure.robot} is down; the crew works on`;
    $('bk-clock-sub').textContent = sub;
    const now = $('bk-now');
    if (now) now.style.left = pct(time);
  }
  const playBtn = $('bk-play');
  const sync = () => { playBtn.textContent = playing ? '❚❚' : '▶'; playBtn.setAttribute('aria-label', playing ? 'Pause the replan' : 'Play the replan'); };

  function resize() {
    const w = box.clientWidth, h = box.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h);
    labels.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(box);
  let last = performance.now();
  const loop = (now) => {
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    if (playing) { t += dt * 4; if (t >= end) { t = end; playing = false; sync(); } }
    place(t, false);
    controls.update();
    renderer.render(scene, camera);
    labels.render(scene, camera);
  };

  return {
    show(fr, row) {
      load(fr);
      failure = row ? { robot: row.robot, t_h: row.t_h } : null;
      outcome = row?.outcome || null;
      end = frames[frames.length - 1].t;
      from = row ? Math.max(frames[0].t, row.t_h - 3) : frames[0].t;
      t = calm.matches && row ? row.t_h : from;
      playing = !!row && !calm.matches && end > from + 0.01;
      sync();
      place(t, true);
    },
    toggle() { if (t >= end - 0.01) t = from; playing = !playing; sync(); },
    wakeUp() { if (awake) return; awake = true; resize(); last = performance.now(); renderer.setAnimationLoop(loop); },
    sleep() { awake = false; playing = false; sync(); renderer.setAnimationLoop(null); },
  };
}
