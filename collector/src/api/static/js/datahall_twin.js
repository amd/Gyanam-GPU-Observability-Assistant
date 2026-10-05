// Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: MIT
//
// Data Hall View — 3D digital twin. Renders racks on a data-hall floor with
// systems seated at their rack-units, coloured by poll status, and lets the
// operator assign/move systems directly in the scene. three.js is vendored
// locally (no CDN) and resolved via the page's import map.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

// ---- constants ----
// Rack geometry follows the OCP Open Rack v3 (ORv3) form factor:
//   - External width  : 600 mm  (fits a standard 600 mm floor tile)
//   - External depth   : ~1200 mm
//   - Vertical pitch   : 48 mm per "OpenU" (OU)  — vs 44.45 mm for a 19" EIA RU
//   - Equipment bay    : 21" (538 mm) inside the 600 mm frame
// See https://www.opencompute.org/ (Open Rack v3 base spec). rack_height_u from
// the server config is the number of OU in the frame (ORv3 is commonly 48 OU).
const U_M = 0.048;                // 1 OpenU (OCP Open Rack) in metres
const RACK_W = 0.6;               // cabinet external width (m) — OCP 600 mm
const RACK_D = 1.2;               // cabinet external depth (m) — OCP ~1200 mm
// Two visible states: "connected" is green, everything else (failed OR not yet
// polled) renders as the same "unavailable" gray — never-polled is not shown as
// a distinct state in the main view.
const STATUS_COLOR = { success: 0x16a34a, error: 0x6b7280, pending: 0x6b7280 };
const REFRESH_MS = 30000;         // live status poll cadence

const $ = (id) => document.getElementById(id);

// Escape BMC/user-provided strings before inserting into tooltip innerHTML.
function esc(v) {
    const d = document.createElement('div');
    d.textContent = v == null ? '' : String(v);
    return d.innerHTML;
}

// Non-blocking toast (replaces native alert() for action feedback).
let _toastTimer = null;
function toast(message, kind = 'info') {
    const el = $('twin-toast');
    if (!el) return;
    el.textContent = message;
    el.style.background = kind === 'error' ? '#7f1d1d' : (kind === 'success' ? '#14532d' : '#1e293b');
    el.style.display = 'block';
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.style.display = 'none'; }, kind === 'error' ? 6000 : 3500);
}

// ---- page-provided data ----
const host = $('twin-canvas');
const CSRF = $('twin-csrf')?.value || '';
let layout = JSON.parse($('twin-layout-data')?.textContent || '{}');

// ---- three.js objects ----
let scene, camera, renderer, labelRenderer, controls, raycaster;
const pointer = new THREE.Vector2();
let rackGroup = null;             // current hall's racks
let currentHall = null;
let selectedUnplacedId = null;
let hovered = null;
let pollTimer = null;
let heatmapMetric = '';   // '' = colour by status; else a metric key (gpu_temp…)
let heatmapInfo = null;   // { values:{id:val}, domain:[lo,hi], unit, critical }

// Shared geometries/materials (reused across meshes).
const SYS_GEO = new THREE.BoxGeometry(RACK_W * 0.86, 1, RACK_D * 0.86);
const SYS_EDGES = new THREE.EdgesGeometry(SYS_GEO);          // crisp border per system
const SYS_EDGE_MAT = new THREE.LineBasicMaterial({ color: 0xe8eef7 });
const RACK_EDGE_MAT = new THREE.LineBasicMaterial({ color: 0x475569 });
const RACK_BODY_MAT = new THREE.MeshStandardMaterial({
    color: 0x0f172a, transparent: true, opacity: 0.10, roughness: 0.8, metalness: 0.2,
});
// Painted-steel cabinet frame (posts, rails, plinth, roof) — dark metallic.
const RACK_FRAME_MAT = new THREE.MeshStandardMaterial({
    color: 0x2b3444, roughness: 0.55, metalness: 0.75,
});
// Perforated front/rear door — translucent so seated systems stay visible.
const RACK_DOOR_MAT = new THREE.MeshStandardMaterial({
    color: 0x1b2433, transparent: true, opacity: 0.22, roughness: 0.6, metalness: 0.5,
    side: THREE.DoubleSide,
});
const RACK_POST = 0.03;  // square frame-post thickness (m)
// Blanking panel filling empty rack-units (matte dark filler plate).
const BLANKING_MAT = new THREE.MeshStandardMaterial({
    color: 0x334155, roughness: 0.85, metalness: 0.3,
});
const sysMaterial = (status) =>
    new THREE.MeshStandardMaterial({
        color: STATUS_COLOR[status] ?? STATUS_COLOR.pending,
        emissive: STATUS_COLOR[status] ?? STATUS_COLOR.pending,
        emissiveIntensity: 0.35,
        roughness: 0.5,
        metalness: 0.1,
    });

function rackHeightM() {
    return (layout.rack_height_u || 48) * U_M;  // OCP Open Rack v3 ~ 48 OU
}

// ---- scene bootstrap ----
function initThree() {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1220);
    scene.fog = new THREE.Fog(0x0b1220, 18, 60);

    const w = host.clientWidth, h = host.clientHeight;
    camera = new THREE.PerspectiveCamera(55, w / h, 0.1, 500);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setSize(w, h);
    host.appendChild(renderer.domElement);

    labelRenderer = new CSS2DRenderer();
    labelRenderer.setSize(w, h);
    labelRenderer.domElement.style.position = 'absolute';
    labelRenderer.domElement.style.top = '0';
    labelRenderer.domElement.style.pointerEvents = 'none';
    host.appendChild(labelRenderer.domElement);

    // Bind to the WebGL canvas, NOT the label renderer (that overlay has
    // pointer-events: none, so controls attached to it receive no input).
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.maxPolarAngle = Math.PI * 0.49;   // don't dip under the floor
    controls.minDistance = 1.5;
    controls.maxDistance = 80;

    scene.add(new THREE.HemisphereLight(0xdfe9ff, 0x0a0f1a, 0.9));
    scene.add(new THREE.AmbientLight(0xffffff, 0.25));
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(6, 12, 8);
    scene.add(dir);

    raycaster = new THREE.Raycaster();

    renderer.domElement.addEventListener('pointermove', onPointerMove);
    renderer.domElement.addEventListener('click', onClick);
    window.addEventListener('resize', onResize);

    animate();
}

function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
}

function onResize() {
    const w = host.clientWidth, h = host.clientHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    labelRenderer.setSize(w, h);
}

// ---- floor ----
const TILE_M = 0.6;  // 600 mm raised-floor tile (industry standard)

function makeFloor(spanX, spanZ, cx, cz) {
    const g = new THREE.Group();
    const w = Math.max(spanX + 6, 10), d = Math.max(spanZ + 6, 10);
    const floor = new THREE.Mesh(
        new THREE.PlaneGeometry(w, d),
        new THREE.MeshStandardMaterial({ color: 0x0e1626, roughness: 0.95, metalness: 0.1 })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(cx, 0, cz);
    g.add(floor);

    // 600 mm raised-floor tile grid (one grid line per tile seam).
    const span = Math.max(w, d);
    const divs = Math.round(span / TILE_M);
    const grid = new THREE.GridHelper(span, divs, 0x24324d, 0x1a2338);
    grid.position.set(cx, 0.002, cz);
    g.add(grid);
    return g;
}

// Neutral aisle tile laid between rack rows. Deliberately a muted slate (no
// hot/cold red/blue): the floor stays neutral so it can't be mistaken for a
// thermal reading — real temperature is shown by the Heatmap overlay instead.
const AISLE_MAT = new THREE.MeshStandardMaterial({
    color: 0x27303f, roughness: 0.85, metalness: 0.1,
});

function makeAisleStrip(xCenter, zCenter, length) {
    const strip = new THREE.Mesh(
        new THREE.PlaneGeometry(length, TILE_M * 0.9),
        AISLE_MAT
    );
    strip.rotation.x = -Math.PI / 2;
    strip.position.set(xCenter, 0.004, zCenter);
    strip.userData = { type: 'aisle' };
    return strip;
}

// ---- rack + systems ----
function makeRack(rackData) {
    const group = new THREE.Group();
    group.position.set(rackData.x, 0, rackData.z);

    const hM = rackHeightM();
    const halfW = RACK_W / 2;
    const halfD = RACK_D / 2;

    // Translucent cabinet body (also the pick target for empty-slot placement).
    const body = new THREE.Mesh(new THREE.BoxGeometry(RACK_W, hM, RACK_D), RACK_BODY_MAT);
    body.position.y = hM / 2;
    body.userData = { type: 'rack', rack: rackData };
    group.add(body);

    // ---- cabinet frame: makes the column read as a real datacenter rack ----
    // A short helper to add a frame box at (x,y,z) with given size.
    const addBox = (w, h, d, x, y, z, mat = RACK_FRAME_MAT) => {
        const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
        m.position.set(x, y, z);
        // Frame is scenery: ignore it when picking so slot placement still hits
        // the translucent body behind it.
        m.userData = { type: 'rack-frame' };
        group.add(m);
        return m;
    };

    const P = RACK_POST;
    // Four vertical corner posts.
    for (const sx of [-1, 1]) {
        for (const sz of [-1, 1]) {
            addBox(P, hM, P, sx * (halfW - P / 2), hM / 2, sz * (halfD - P / 2));
        }
    }
    // Base plinth (slightly proud of the footprint) + roof cap.
    addBox(RACK_W + 0.02, 0.05, RACK_D + 0.02, 0, 0.025, 0);
    addBox(RACK_W, 0.04, RACK_D, 0, hM - 0.02, 0);
    // Two front mounting rails (the 19"/21" EIA/OpenU uprights systems bolt to).
    const railInset = 0.06;
    for (const sx of [-1, 1]) {
        addBox(0.02, hM - 0.1, 0.02, sx * (halfW - railInset), hM / 2, halfD - 0.03);
    }
    // Perforated front & rear doors (translucent panels within the frame).
    for (const sz of [-1, 1]) {
        const door = new THREE.Mesh(
            new THREE.PlaneGeometry(RACK_W - 2 * P, hM - 0.1), RACK_DOOR_MAT);
        door.position.set(0, hM / 2, sz * (halfD - 0.004));
        door.userData = { type: 'rack-frame' };
        group.add(door);
    }

    // Crisp outline around the whole cabinet.
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(body.geometry), RACK_EDGE_MAT);
    edges.position.y = hM / 2;
    group.add(edges);

    // Seated systems — each with a bright outline so adjacent units stay distinct.
    const H = layout.rack_height_u || 48;
    const defaultHeight = layout.default_height_u || 4;
    const occupied = new Array(H + 2).fill(false);  // 1-indexed U occupancy
    for (const s of rackData.systems) {
        const height = Math.max(1, s.height_u || defaultHeight);
        const mesh = new THREE.Mesh(SYS_GEO, sysMaterial(s.status));
        mesh.scale.y = height * U_M * 0.94;
        mesh.position.set(0, (s.start_u - 1 + height / 2) * U_M, 0.04);
        mesh.userData = { type: 'system', system: s, rack: rackData };
        mesh.add(new THREE.LineSegments(SYS_EDGES, SYS_EDGE_MAT));  // border (inherits scale)
        group.add(mesh);
        for (let u = s.start_u; u < s.start_u + height && u <= H; u++) occupied[u] = true;
    }

    // Fill empty rack-units with blanking panels (merged into contiguous runs) so
    // a sparsely-populated rack still reads as a real, paneled cabinet rather
    // than a hollow box. Panels are scenery — picking passes through to the body.
    let run = 0;
    const addBlank = (endExclusive) => {
        if (run <= 0) return;
        const startU = endExclusive - run;              // first empty U in the run
        const panel = new THREE.Mesh(
            new THREE.BoxGeometry(RACK_W - 0.12, run * U_M * 0.9, 0.015), BLANKING_MAT);
        panel.position.set(0, (startU - 1 + run / 2) * U_M, halfD - 0.05);
        panel.userData = { type: 'rack-frame' };
        group.add(panel);
        run = 0;
    };
    for (let u = 1; u <= H; u++) {
        if (occupied[u]) addBlank(u); else run++;
    }
    addBlank(H + 1);

    // Rack label floating above.
    const label = document.createElement('div');
    label.className = 'twin-rack-label';
    label.textContent = 'Rack ' + rackData.rack;
    const labelObj = new CSS2DObject(label);
    labelObj.position.set(0, hM + 0.18, 0);
    group.add(labelObj);

    if (rackData.overflow > 0) {
        const warn = document.createElement('div');
        warn.className = 'twin-rack-warn';
        warn.textContent = '⚠ ' + rackData.overflow + ' overflow';
        const warnObj = new CSS2DObject(warn);
        warnObj.position.set(0, hM + 0.42, 0);
        group.add(warnObj);
    }

    return group;
}

// Overhead ladder-style cable tray running along a row (x axis) above the racks.
const TRAY_MAT = new THREE.MeshStandardMaterial({
    color: 0x3a4659, roughness: 0.6, metalness: 0.7,
});

function makeOverheadTray(xCenter, z, length, yTop) {
    const g = new THREE.Group();
    const y = yTop + 0.35;  // suspended above the cabinets
    const trayW = 0.28;
    // Two side rails.
    for (const sz of [-1, 1]) {
        const rail = new THREE.Mesh(new THREE.BoxGeometry(length, 0.04, 0.03), TRAY_MAT);
        rail.position.set(xCenter, y, z + sz * trayW / 2);
        g.add(rail);
    }
    // Rungs every ~0.3 m.
    const rungs = Math.max(2, Math.round(length / 0.3));
    for (let i = 0; i <= rungs; i++) {
        const rung = new THREE.Mesh(new THREE.BoxGeometry(0.02, 0.02, trayW), TRAY_MAT);
        rung.position.set(xCenter - length / 2 + (i / rungs) * length, y, z);
        g.add(rung);
    }
    return g;
}

// Build the room infrastructure (hot/cold aisles + overhead trays) for the racks
// of one hall, keyed off their row positions.
function makeInfrastructure(racks, yTop) {
    const g = new THREE.Group();
    // Group rack x-positions by row (z), keeping the row label.
    const rows = new Map();
    for (const r of racks) {
        if (!rows.has(r.z)) rows.set(r.z, { xs: [], row: r.row });
        rows.get(r.z).xs.push(r.x);
    }
    const zs = [...rows.keys()].sort((a, b) => a - b);
    for (let i = 0; i < zs.length; i++) {
        const z = zs[i];
        const { xs, row } = rows.get(z);
        const xMin = Math.min(...xs), xMax = Math.max(...xs);
        const len = (xMax - xMin) + RACK_W + 0.6;
        const xc = (xMin + xMax) / 2;
        // Overhead cable tray over every row.
        g.add(makeOverheadTray(xc, z, len, yTop));
        // Floor signage at the head of the row for wayfinding.
        if (row) {
            const tag = document.createElement('div');
            tag.className = 'twin-rack-label';
            tag.textContent = 'Row ' + row;
            const tagObj = new CSS2DObject(tag);
            tagObj.position.set(xMin - RACK_W, 0.2, z);
            g.add(tagObj);
        }
        // Neutral aisle strip between this row and the next (walkway marker).
        if (i < zs.length - 1) {
            const zMid = (z + zs[i + 1]) / 2;
            g.add(makeAisleStrip(xc, zMid, len));
        }
    }
    return g;
}

function buildHall(hallName, { keepCamera = false } = {}) {
    currentHall = hallName;
    if (rackGroup) {
        scene.remove(rackGroup);
        disposeGroup(rackGroup);
    }
    rackGroup = new THREE.Group();

    const racks = (layout.racks || []).filter((r) => r.hall === hallName);
    if (!racks.length) {
        scene.add(rackGroup);
        toggleEmpty(true);
        return;
    }
    toggleEmpty(false);

    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const r of racks) {
        rackGroup.add(makeRack(r));
        minX = Math.min(minX, r.x); maxX = Math.max(maxX, r.x);
        minZ = Math.min(minZ, r.z); maxZ = Math.max(maxZ, r.z);
    }
    const cx = (minX + maxX) / 2, cz = (minZ + maxZ) / 2;
    rackGroup.add(makeFloor(maxX - minX, maxZ - minZ, cx, cz));
    rackGroup.add(makeInfrastructure(racks, rackHeightM()));
    scene.add(rackGroup);

    // Only reframe the camera on an explicit (re)build — never on a background
    // status refresh — so zoom/pan survive the 30 s poll cycle.
    if (!keepCamera) frameCamera(cx, cz, Math.max(maxX - minX, maxZ - minZ));
}

function frameCamera(cx, cz, spread) {
    const dist = Math.max(spread * 1.1, 4) + 4;
    camera.position.set(cx + dist * 0.7, rackHeightM() + dist * 0.6, cz + dist);
    controls.target.set(cx, rackHeightM() * 0.5, cz);
    controls.update();
}

// Zoom the camera onto a single rack (used by the find box).
function frameOnRack(rackData) {
    frameCamera(rackData.x, rackData.z, RACK_W + 2.5);
}

// Find a rack (by rack label) or a system (by name/BMC host) anywhere in the
// fleet, switch to its hall if needed, and frame the camera on it — the
// wayfinding tool for large halls.
function findAndFrame(query) {
    const q = (query || '').trim().toLowerCase();
    if (!q) return;
    const racks = layout.racks || [];
    let hit = racks.find((r) => (r.rack || '').toLowerCase() === q)
        || racks.find((r) => (r.rack || '').toLowerCase().includes(q));
    let sysHit = null;
    if (!hit) {
        for (const r of racks) {
            const s = (r.systems || []).find(
                (sy) => (sy.name || '').toLowerCase().includes(q)
                     || (sy.host || '').toLowerCase().includes(q));
            if (s) { hit = r; sysHit = s; break; }
        }
    }
    if (!hit) { toast(`No rack or system matching "${query}"`, 'error'); return; }
    if (hit.hall !== currentHall) {
        $('twin-hall-select').value = hit.hall;
        buildHall(hit.hall, { keepCamera: true });  // switch hall without snapping camera
    }
    frameOnRack(hit);
    toast(sysHit ? `Found ${sysHit.name} — rack ${hit.rack}` : `Rack ${hit.rack}`, 'success');
}

function disposeGroup(group) {
    group.traverse((o) => {
        // Dispose per-rack geometries; keep the shared system geo/edges.
        if (o.geometry && o.geometry !== SYS_GEO && o.geometry !== SYS_EDGES) {
            o.geometry.dispose?.();
        }
        // System fill materials are created per system; shared mats are kept.
        if (o.userData?.type === 'system' && o.material?.dispose) o.material.dispose();
        if (o.isCSS2DObject && o.element?.remove) o.element.remove();
    });
}

// ---- interaction ----
function pick(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    return rackGroup ? raycaster.intersectObjects(rackGroup.children, true) : [];
}

function onPointerMove(event) {
    const hits = pick(event);
    const sysHit = hits.find((h) => h.object.userData?.type === 'system');
    if (hovered && hovered !== sysHit?.object) {
        hovered.material.emissiveIntensity = 0.35;
        hovered = null;
    }
    const tip = $('twin-tooltip');
    if (sysHit) {
        hovered = sysHit.object;
        hovered.material.emissiveIntensity = 0.85;
        const s = sysHit.object.userData.system;
        const uRange = s.height_u > 1 ? '–' + (s.start_u + s.height_u - 1) : '';
        let html = `<strong>${esc(s.name)}</strong><br>BMC: ${esc(s.host)}<br>` +
            `U${s.start_u}${uRange} · ${esc(s.status)}`;
        if (s.location_check === 'mismatch') {
            html += '<br><span style="color:#fbbf24">⚠ name / BMC location differ</span>';
        }
        const inv = s.inventory || {};
        const rows = [];
        if (inv.model) rows.push(esc(inv.model));
        if (inv.serial) rows.push('S/N ' + esc(inv.serial));
        if (inv.asset_tag) rows.push('Asset ' + esc(inv.asset_tag));
        if (inv.health) rows.push('Health: ' + esc(inv.health));
        if (inv.power_state) rows.push('Power: ' + esc(inv.power_state));
        if (inv.cpu) rows.push(esc(inv.cpu));
        if (inv.gpu) rows.push(esc(inv.gpu));
        if (inv.memory_gib) rows.push(esc(inv.memory_gib) + ' GiB');
        if (inv.bios_version) rows.push('BIOS ' + esc(inv.bios_version));
        if (inv.bmc_firmware) rows.push('BMC fw ' + esc(inv.bmc_firmware));
        if (inv.firmware && typeof inv.firmware === 'object') {
            for (const [name, ver] of Object.entries(inv.firmware)) {
                rows.push(esc(name) + ' ' + esc(ver));
            }
        }
        if (rows.length) {
            html += '<br><span style="opacity:.75;font-size:.95em">' + rows.join('<br>') + '</span>';
        }
        tip.innerHTML = html;
        tip.style.display = 'block';
        tip.style.left = (event.clientX + 14) + 'px';
        tip.style.top = (event.clientY + 14) + 'px';
        host.style.cursor = 'pointer';
    } else {
        tip.style.display = 'none';
        host.style.cursor = hits.length ? 'crosshair' : 'default';
    }
}

function onClick(event) {
    const hits = pick(event);
    if (!hits.length) return;
    const sysHit = hits.find((h) => h.object.userData?.type === 'system');
    if (sysHit) {
        openEditModal(sysHit.object.userData.system, sysHit.object.userData.rack);
        return;
    }
    const rackHit = hits.find((h) => h.object.userData?.type === 'rack');
    if (rackHit) {
        const u = Math.min(
            Math.max(1, Math.floor(rackHit.point.y / U_M) + 1),
            layout.rack_height_u || 42
        );
        if (selectedUnplacedId) {
            placeSelectedAt(rackHit.object.userData.rack, u);
        }
    }
}

// ---- REST calls (reuse existing placement endpoints) ----
async function postForm(url, fields) {
    const fd = new FormData();
    fd.append('csrf_token', CSRF);
    for (const [k, v] of Object.entries(fields)) {
        if (v !== null && v !== undefined && v !== '') fd.append(k, v);
    }
    const resp = await fetch(url, { method: 'POST', body: fd });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* non-JSON */ }
    if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
    return data;
}

async function placeSelectedAt(rack, u) {
    try {
        await postForm(`/datahall/api/${selectedUnplacedId}/placement`, {
            hall: rack.hall === layout.unknown_hall ? '' : rack.hall,
            row: rack.row === layout.unknown_row ? '' : rack.row,
            rack: rack.rack, rack_u: u, height: layout.default_height_u || 6,
        });
        selectedUnplacedId = null;
        await refresh({ keepCamera: true });
    } catch (e) { toast(e.message, 'error'); }
}

// ---- modal ----
function openModal() { $('twin-modal').classList.add('open'); }
function closeModal() { $('twin-modal').classList.remove('open'); }

function openEditModal(sys, rack) {
    $('twin-m-id').value = sys.id;
    $('twin-m-sub').textContent = sys.name + ' — BMC ' + sys.host;
    $('twin-m-hall').value = rack.hall === layout.unknown_hall ? '' : rack.hall;
    $('twin-m-row').value = rack.row === layout.unknown_row ? '' : rack.row;
    $('twin-m-rack').value = rack.rack;
    $('twin-m-u').value = sys.start_u || '';
    $('twin-m-height').value = sys.height_u || layout.default_height_u || 6;
    $('twin-m-unit').value = sys.unit_type || layout.default_unit_type || 'EIA_310';
    openModal();
}

async function saveModal() {
    const id = $('twin-m-id').value;
    if (!id) return;
    try {
        await postForm(`/datahall/api/${id}/placement`, {
            hall: $('twin-m-hall').value.trim(),
            row: $('twin-m-row').value.trim(),
            rack: $('twin-m-rack').value.trim(),
            rack_u: $('twin-m-u').value.trim(),
            height: $('twin-m-height').value.trim(),
            unit_type: $('twin-m-unit').value,
        });
        closeModal();
        await refresh({ keepCamera: true });
    } catch (e) { toast(e.message, 'error'); }
}

async function unplaceModal() {
    const id = $('twin-m-id').value;
    if (!id) return;
    try {
        await postForm(`/datahall/api/${id}/placement/clear`, {});
        closeModal();
        await refresh({ keepCamera: true });
    } catch (e) { toast(e.message, 'error'); }
}

async function autoResolve() {
    try {
        const d = await postForm('/datahall/api/resolve', {});
        toast(`Resolved ${d.resolved} of ${d.total} systems from hostnames`, 'success');
        await refresh({ keepCamera: true });
    } catch (e) { toast(e.message, 'error'); }
}

// ---- HTML panels ----
function populateHallSelector() {
    const sel = $('twin-hall-select');
    sel.innerHTML = '';
    for (const name of layout.halls || []) {
        const opt = document.createElement('option');
        opt.value = name; opt.textContent = name;
        sel.appendChild(opt);
    }
    if (currentHall && (layout.halls || []).includes(currentHall)) {
        sel.value = currentHall;
    } else {
        currentHall = (layout.halls || [])[0] || null;
        if (currentHall) sel.value = currentHall;
    }
}

function populateUnplaced() {
    const list = $('twin-unplaced-list');
    const count = $('twin-unplaced-count');
    list.innerHTML = '';
    const items = layout.unplaced || [];
    count.textContent = items.length;
    for (const s of items) {
        const li = document.createElement('div');
        li.className = 'twin-chip' + (s.id === selectedUnplacedId ? ' selected' : '');
        li.innerHTML = `<span class="dot ${esc(s.status)}"></span>${esc(s.name)}`;
        li.title = 'BMC ' + s.host;
        li.addEventListener('click', () => {
            selectedUnplacedId = s.id === selectedUnplacedId ? null : s.id;
            populateUnplaced();
        });
        list.appendChild(li);
    }
    $('twin-hint').style.display = selectedUnplacedId ? 'block' : 'none';

    // Placed-but-overflowing systems: can't be drawn in their rack, so surface
    // them here (identified, with their intended location) rather than hiding
    // them behind a count — otherwise a monitored node is invisible.
    const overflow = layout.overflow || [];
    const wrap = $('twin-overflow-wrap');
    if (wrap) {
        wrap.style.display = overflow.length ? 'block' : 'none';
        $('twin-overflow-count').textContent = overflow.length;
        const olist = $('twin-overflow-list');
        olist.innerHTML = '';
        for (const s of overflow) {
            const li = document.createElement('div');
            li.className = 'twin-chip';
            li.innerHTML = `<span class="dot ${esc(s.status)}"></span>${esc(s.name)}`;
            li.title = `BMC ${s.host} · intended ${s.location || '?'} (doesn't fit rack)`;
            olist.appendChild(li);
        }
    }
}

function updateStats() {
    const c = layout.counts || {};
    $('twin-stats').textContent =
        `${c.placed || 0} placed · ${c.unplaced || 0} unplaced · ` +
        `${(layout.halls || []).length} hall(s) · ${(layout.racks || []).length} rack(s)`;
    updateLegendCounts();
}

// Legend doubles as a per-hall fleet rollup: count connected vs unavailable
// among the systems placed in the hall currently on screen.
function updateLegendCounts() {
    let ok = 0, bad = 0;
    for (const r of layout.racks || []) {
        if (r.hall !== currentHall) continue;
        for (const s of r.systems) (s.status === 'success' ? ok++ : bad++);
    }
    const okEl = $('twin-legend-ok'), badEl = $('twin-legend-bad');
    if (okEl) okEl.textContent = ok;
    if (badEl) badEl.textContent = bad;
}

function toggleEmpty(show) {
    $('twin-empty').style.display = show ? 'flex' : 'none';
}

// Update seated-system colours in place (no scene rebuild, no camera change) so
// the live status poll never disturbs the operator's current view.
function applyStatusColors() {
    if (!rackGroup) return;
    const byId = new Map();
    for (const r of layout.racks || []) {
        if (r.hall !== currentHall) continue;
        for (const s of r.systems) byId.set(s.id, s.status);
    }
    rackGroup.traverse((o) => {
        if (o.userData?.type !== 'system') return;
        const sys = o.userData.system;
        const next = byId.get(sys.id);
        if (next && next !== sys.status) {
            sys.status = next;
            if (o.material?.dispose) o.material.dispose();
            o.material = sysMaterial(next);
        }
    });
}

// ---- heatmap (colour systems by live telemetry) ----
const HEAT_NODATA = 0x475569;  // neutral gray for systems with no fresh reading
// Gradient stops: blue → green → yellow → red.
const HEAT_STOPS = [[0x1d, 0x4e, 0xd8], [0x16, 0xa3, 0x4a], [0xea, 0xb3, 0x08], [0xdc, 0x26, 0x26]];

function heatColor(v, lo, hi) {
    const t = hi > lo ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5;
    const seg = Math.min(HEAT_STOPS.length - 2, Math.floor(t * 3));
    const f = t * 3 - seg;
    const a = HEAT_STOPS[seg], b = HEAT_STOPS[seg + 1];
    const r = Math.round(a[0] + (b[0] - a[0]) * f);
    const g = Math.round(a[1] + (b[1] - a[1]) * f);
    const bl = Math.round(a[2] + (b[2] - a[2]) * f);
    return (r << 16) | (g << 8) | bl;
}

function heatMaterial(color) {
    return new THREE.MeshStandardMaterial({
        color, emissive: color, emissiveIntensity: 0.32, roughness: 0.5, metalness: 0.1,
    });
}

// Recolour seated systems by heatmap value in place (no rebuild, no camera move).
function applyHeatmap() {
    if (!rackGroup || !heatmapInfo) return;
    const vals = heatmapInfo.values || {};
    const dom = heatmapInfo.domain || [0, 1];
    rackGroup.traverse((o) => {
        if (o.userData?.type !== 'system') return;
        const v = vals[o.userData.system.id];
        const color = v === undefined ? HEAT_NODATA : heatColor(v, dom[0], dom[1]);
        if (o.material?.dispose) o.material.dispose();
        o.material = heatMaterial(color);
    });
}

async function fetchHeatmap() {
    if (!heatmapMetric) { heatmapInfo = null; return; }
    try {
        const r = await fetch(`/datahall/api/heatmap?metric=${encodeURIComponent(heatmapMetric)}`,
            { headers: { Accept: 'application/json' } });
        if (r.ok) heatmapInfo = await r.json();
    } catch (e) { /* keep last */ }
}

// Apply whichever colouring is active.
function recolor() {
    if (heatmapMetric) applyHeatmap();
    else applyStatusColors();
}

function updateHeatLegend() {
    const on = heatmapMetric !== '';
    $('twin-status-legend').style.display = on ? 'none' : 'flex';
    $('twin-heat-legend').style.display = on ? 'flex' : 'none';
    if (!on || !heatmapInfo) return;
    const dom = heatmapInfo.domain || [0, 0];
    const unit = heatmapInfo.unit || '';
    $('twin-heat-lo').textContent = Math.round(dom[0]) + unit;
    $('twin-heat-hi').textContent = Math.round(dom[1]) + unit;
    $('twin-heat-unit').textContent = { gpu_temp: 'GPU temp', board_temp: 'Board temp', power: 'Power' }[heatmapMetric] || '';
    // How many placed systems in this hall have no reading.
    let placed = 0, withData = 0;
    const vals = heatmapInfo.values || {};
    for (const r of layout.racks || []) {
        if (r.hall !== currentHall) continue;
        for (const s of r.systems) { placed++; if (vals[s.id] !== undefined) withData++; }
    }
    const missing = placed - withData;
    $('twin-heat-nodata').textContent = missing > 0 ? `· ${missing} no data` : '';
}

// ---- refresh cycle ----
async function refresh({ rebuild = true, keepCamera = false } = {}) {
    try {
        const resp = await fetch('/datahall/api/layout', { headers: { Accept: 'application/json' } });
        if (resp.ok) layout = await resp.json();
    } catch (e) { /* keep last layout */ }
    populateHallSelector();
    populateUnplaced();
    updateStats();
    if (heatmapMetric) await fetchHeatmap();
    if (rebuild) buildHall(currentHall, { keepCamera });
    recolor();
    updateHeatLegend();
}

// ---- wire up ----
function wire() {
    $('twin-hall-select').addEventListener('change', (e) => buildHall(e.target.value));
    $('twin-reset-cam').addEventListener('click', () => buildHall(currentHall));
    $('twin-resolve').addEventListener('click', autoResolve);
    $('twin-find').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') findAndFrame(e.target.value);
    });
    // Status / heatmap selector: an inline segmented single-select button group.
    const heatGroup = $('twin-heatmap-group');
    heatGroup.querySelectorAll('.twin-seg-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            if (btn.classList.contains('selected')) return;  // already active
            heatmapMetric = btn.dataset.value;
            heatGroup.querySelectorAll('.twin-seg-btn').forEach((b) => {
                const on = b === btn;
                b.classList.toggle('selected', on);
                b.setAttribute('aria-checked', String(on));
            });
            // Rebuild (restores status colours first so toggling back works) then
            // recolour; keep the operator's current viewpoint.
            refresh({ rebuild: true, keepCamera: true });
        });
    });
    $('twin-m-save').addEventListener('click', saveModal);
    $('twin-m-cancel').addEventListener('click', closeModal);
    $('twin-m-unplace').addEventListener('click', unplaceModal);
    $('twin-modal').addEventListener('click', (e) => {
        if (e.target === $('twin-modal')) closeModal();
    });
}

function boot() {
    try {
        initThree();
    } catch (e) {
        // No WebGL — reveal the server-rendered fallback list.
        console.error('WebGL init failed', e);
        $('twin-stage').style.display = 'none';
        $('twin-fallback').style.display = 'block';
        return;
    }
    wire();
    populateHallSelector();
    populateUnplaced();
    updateStats();
    buildHall(currentHall);
    pollTimer = setInterval(() => refresh({ rebuild: false }), REFRESH_MS);
}

boot();
