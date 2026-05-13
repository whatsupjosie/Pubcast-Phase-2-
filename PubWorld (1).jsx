import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

// ═══════════════════════════════════════════════════════════════════════════════
// VOXEL VOLUME — sparse store with hollow culling + face exposure
// ═══════════════════════════════════════════════════════════════════════════════

const FACES = [
  { name:"+x", n:[1,0,0], dir: new THREE.Vector3( 1, 0, 0) },
  { name:"-x", n:[-1,0,0],dir: new THREE.Vector3(-1, 0, 0) },
  { name:"+y", n:[0,1,0], dir: new THREE.Vector3( 0, 1, 0) },
  { name:"-y", n:[0,-1,0],dir: new THREE.Vector3( 0,-1, 0) },
  { name:"+z", n:[0,0,1], dir: new THREE.Vector3( 0, 0, 1) },
  { name:"-z", n:[0,0,-1],dir: new THREE.Vector3( 0, 0,-1) },
];

// Functional tags — these voxels SURVIVE hollow culling unconditionally
const FUNCTIONAL = new Set([
  "light","trigger","joint","support","interior_visible",
  "wire","audio_source","camera_target","spawn","interactive",
]);

function vk(x,y,z){ return `${Math.round(x)},${Math.round(y)},${Math.round(z)}`; }
function fromVk(k){ const [x,y,z]=k.split(",").map(Number); return {x,y,z}; }

class VoxelVolume {
  constructor(){ this._v=new Map(); this._fn=new Set(); this._shellCache=null; }

  has(x,y,z){ return this._v.has(vk(x,y,z)); }
  get(x,y,z){ return this._v.get(vk(x,y,z)); }
  get size(){ return this._v.size; }

  place(x,y,z,data={}){
    const k=vk(x,y,z);
    this._v.set(k,{color:data.color??0xffffff,tag:data.tag??null,mat:data.mat??"default"});
    if(data.tag && FUNCTIONAL.has(data.tag)) this._fn.add(k);
    this._shellCache=null;
  }

  remove(x,y,z){
    const k=vk(x,y,z);
    this._v.delete(k); this._fn.delete(k); this._shellCache=null;
  }

  clear(){ this._v.clear(); this._fn.clear(); this._shellCache=null; }

  // ── Shell: only voxels with at least one exposed face (or functional) ────────
  get shell(){
    if(this._shellCache) return this._shellCache;
    const s=new Set();
    this._v.forEach((_,k)=>{
      if(this._fn.has(k)){ s.add(k); return; }
      const {x,y,z}=fromVk(k);
      if(FACES.some(f=>!this._v.has(vk(x+f.n[0],y+f.n[1],z+f.n[2])))) s.add(k);
    });
    this._shellCache=s;
    return s;
  }

  // ── For each shell voxel, which faces are exposed? ───────────────────────────
  exposedFaces(){
    const result=[];
    this.shell.forEach(k=>{
      const {x,y,z}=fromVk(k);
      const exposed=FACES.filter(f=>!this._v.has(vk(x+f.n[0],y+f.n[1],z+f.n[2])));
      if(exposed.length) result.push({x,y,z,color:this._v.get(k).color,mat:this._v.get(k).mat,tag:this._v.get(k).tag,faces:exposed});
    });
    return result;
  }

  stats(){
    const total=this._v.size, shell=this.shell.size, culled=total-shell;
    return {total,shell,culled,pct:total?Math.round(culled/total*100):0};
  }

  // For backend POST
  toJSON(label="unnamed"){
    return {
      label, hollow:true, stats:this.stats(),
      blocks:this.exposedFaces().map(v=>({
        x:v.x,y:v.y,z:v.z,
        color_hex:`#${v.color.toString(16).padStart(6,"0")}`,
        material:v.mat, tag:v.tag,
        exposed_faces:v.faces.map(f=>f.name),
        type:v.tag&&FUNCTIONAL.has(v.tag)?v.tag:"surface",
      })),
    };
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// GREEDY MESHER
// Merges coplanar, same-colour, exposed faces into the fewest possible quads.
// Input:  exposedFaces() output
// Output: THREE.BufferGeometry (indexed quads → triangles)
// ═══════════════════════════════════════════════════════════════════════════════

function greedyMesh(voxelFaceList, targetColor){
  // Collect all quads for this colour across all faces
  const positions=[], normals=[], indices=[];

  // Axis definitions for each face direction
  const AXIS_DEF = {
    "+x":{ axis:0, sign:1,  u:1, v:2, uDir:[0,1,0], vDir:[0,0,1] },
    "-x":{ axis:0, sign:-1, u:1, v:2, uDir:[0,1,0], vDir:[0,0,1] },
    "+y":{ axis:1, sign:1,  u:0, v:2, uDir:[1,0,0], vDir:[0,0,1] },
    "-y":{ axis:1, sign:-1, u:0, v:2, uDir:[1,0,0], vDir:[0,0,1] },
    "+z":{ axis:2, sign:1,  u:0, v:1, uDir:[1,0,0], vDir:[0,1,0] },
    "-z":{ axis:2, sign:-1, u:0, v:1, uDir:[1,0,0], vDir:[0,1,0] },
  };

  // Group voxel-faces by (face direction, layer)
  const layers = new Map(); // "faceName:layer" → Set of [u,v]
  const colorFilter = voxelFaceList.filter(v => v.color === targetColor);

  colorFilter.forEach(v => {
    v.faces.forEach(f => {
      const def = AXIS_DEF[f.name];
      const coords = [v.x, v.y, v.z];
      const layer = coords[def.axis] + (def.sign > 0 ? 0.5 : -0.5);
      const layerKey = `${f.name}:${layer}`;
      if (!layers.has(layerKey)) layers.set(layerKey, { def, layer, cells: new Map() });
      const u = coords[def.u], ve = coords[def.v];
      layers.get(layerKey).cells.set(`${u},${ve}`, true);
    });
  });

  // For each layer, greedy-merge cells into rectangles
  layers.forEach(({ def, layer, cells }) => {
    const visited = new Set();
    cells.forEach((_, cellKey) => {
      if (visited.has(cellKey)) return;
      const [u0, v0] = cellKey.split(",").map(Number);

      // Extend in u direction
      let uLen = 1;
      while (cells.has(`${u0 + uLen},${v0}`) && !visited.has(`${u0 + uLen},${v0}`)) uLen++;

      // Extend in v direction (check full width)
      let vLen = 1;
      outer: while (true) {
        for (let du = 0; du < uLen; du++) {
          const k = `${u0 + du},${v0 + vLen}`;
          if (!cells.has(k) || visited.has(k)) break outer;
        }
        vLen++;
      }

      // Mark all cells in this rectangle visited
      for (let du = 0; du < uLen; du++)
        for (let dv = 0; dv < vLen; dv++)
          visited.add(`${u0 + du},${v0 + dv}`);

      // Build quad for this rectangle
      const base = [0, 0, 0];
      base[def.axis] = layer;
      base[def.u] = u0 - 0.5;
      base[def.v] = v0 - 0.5;

      const uD = def.uDir, vD = def.vDir;
      const vIdx = positions.length / 3;

      // 4 corners of the quad
      const corners = [
        [base[0],              base[1],              base[2]             ],
        [base[0]+uD[0]*uLen,   base[1]+uD[1]*uLen,   base[2]+uD[2]*uLen  ],
        [base[0]+uD[0]*uLen+vD[0]*vLen, base[1]+uD[1]*uLen+vD[1]*vLen, base[2]+uD[2]*uLen+vD[2]*vLen],
        [base[0]+vD[0]*vLen,   base[1]+vD[1]*vLen,   base[2]+vD[2]*vLen  ],
      ];
      corners.forEach(c => { positions.push(...c); });

      // Normal for this face direction
      const nx = def.uDir[1]*def.vDir[2]-def.uDir[2]*def.vDir[1];
      const ny = def.uDir[2]*def.vDir[0]-def.uDir[0]*def.vDir[2];
      const nz = def.uDir[0]*def.vDir[1]-def.uDir[1]*def.vDir[0];
      for (let i=0;i<4;i++) normals.push(nx*def.sign, ny*def.sign, nz*def.sign);

      // Two triangles (winding depends on face direction)
      if (def.sign > 0) {
        indices.push(vIdx,vIdx+1,vIdx+2, vIdx,vIdx+2,vIdx+3);
      } else {
        indices.push(vIdx,vIdx+2,vIdx+1, vIdx,vIdx+3,vIdx+2);
      }
    });
  });

  if (positions.length === 0) return null;

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(positions), 3));
  geo.setAttribute("normal",   new THREE.BufferAttribute(new Float32Array(normals),   3));
  geo.setIndex(indices);
  geo.computeVertexNormals(); // recompute for smooth normals
  return geo;
}

// ═══════════════════════════════════════════════════════════════════════════════
// SCENE HELPERS (unchanged from original)
// ═══════════════════════════════════════════════════════════════════════════════

function applyOrbit(cam, o) {
  cam.position.x = o.tx + o.r * Math.sin(o.phi) * Math.sin(o.theta);
  cam.position.y = o.ty + o.r * Math.cos(o.phi);
  cam.position.z = o.tz + o.r * Math.sin(o.phi) * Math.cos(o.theta);
  cam.lookAt(o.tx, o.ty, o.tz);
}

function snap(v) { return Math.floor(v) + 0.5; }

function mkVirtualCamera() {
  const g = new THREE.Group();
  const dark = new THREE.MeshStandardMaterial({ color: 0x1c1c1c, metalness: 0.85, roughness: 0.15 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(1.0, 0.7, 1.5), dark);
  body.castShadow = true; g.add(body);
  const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.35, 0.8, 16), dark);
  barrel.rotation.x = Math.PI / 2; barrel.position.z = -1.15; barrel.castShadow = true; g.add(barrel);
  const glass = new THREE.Mesh(
    new THREE.CircleGeometry(0.24, 16),
    new THREE.MeshStandardMaterial({ color: 0x2244ff, metalness: 0.2, roughness: 0, transparent: true, opacity: 0.85 })
  );
  glass.position.z = -1.56; g.add(glass);
  const vf = new THREE.Mesh(new THREE.BoxGeometry(0.32, 0.2, 0.3), dark);
  vf.position.set(0, 0.46, 0.18); g.add(vf);
  const handle = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.6, 0.22), dark);
  handle.position.set(0.46, -0.35, 0.1); g.add(handle);
  const rec = new THREE.Mesh(
    new THREE.SphereGeometry(0.06, 8, 8),
    new THREE.MeshStandardMaterial({ color: 0xff1100, emissive: 0xff1100, emissiveIntensity: 2.5 })
  );
  rec.position.set(0.36, 0.4, -0.4); g.add(rec);
  g.userData.rec = rec;
  const frustumMat = new THREE.LineBasicMaterial({ color: 0x4400cc, transparent: true, opacity: 0.5 });
  const pts = [
    new THREE.Vector3(0,0,-0.8), new THREE.Vector3(1.5,1.0,-4),
    new THREE.Vector3(0,0,-0.8), new THREE.Vector3(-1.5,1.0,-4),
    new THREE.Vector3(0,0,-0.8), new THREE.Vector3(1.5,-1.0,-4),
    new THREE.Vector3(0,0,-0.8), new THREE.Vector3(-1.5,-1.0,-4),
  ];
  g.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), frustumMat));
  return g;
}

function mkActor() {
  const g = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0x2277dd, roughness: 0.55, metalness: 0.1 });
  const mkr = new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 3 });
  [[0.5,0.5,0.5,0,1.75,0],[0.65,0.82,0.3,0,1.09,0],
   [0.23,0.7,0.23,-0.46,1.06,0],[0.23,0.7,0.23,0.46,1.06,0],
   [0.25,0.8,0.25,-0.18,0.4,0],[0.25,0.8,0.25,0.18,0.4,0]].forEach(([w,h,d,x,y,z]) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w,h,d), mat);
    m.position.set(x,y,z); m.castShadow = true; g.add(m);
  });
  [[0,1.75,0.27],[-0.32,1.47,0.16],[0.32,1.47,0.16],
   [-0.46,1.04,0.13],[0.46,1.04,0.13],[0,1.09,0.16],
   [-0.18,0.76,0.14],[0.18,0.76,0.14],[0,1.75,0],
   [-0.18,0.01,0.14],[0.18,0.01,0.14]].forEach(([x,y,z]) => {
    const m = new THREE.Mesh(new THREE.SphereGeometry(0.048,6,6), mkr);
    m.position.set(x,y,z); g.add(m);
  });
  return g;
}

const SWATCHES = [
  "#ff6a00","#ffcc00","#00ff88","#00ccff","#aa55ff",
  "#ff2266","#ffffff","#888888","#4444ff","#ff44aa",
];

// ═══════════════════════════════════════════════════════════════════════════════
// PUBWORLD COMPONENT
// ═══════════════════════════════════════════════════════════════════════════════

export default function PubWorld() {
  const mountRef = useRef(null);
  const engRef   = useRef(null);
  const modeRef  = useRef("build");
  const palRef   = useRef("#ff6a00");
  const yRef     = useRef(0);

  const [mode,     setMode]     = useState("build");
  const [pal,      setPal]      = useState("#ff6a00");
  const [buildY,   setBuildY]   = useState(0);
  const [stats,    setStats]    = useState({ total:0, shell:0, culled:0, pct:0 });
  const [sets,     setSets]     = useState([]);
  const [status,   setStatus]   = useState("PUB WORLD INITIALIZED · VOID READY · START BUILDING YOUR SET");
  const [recState, setRecState] = useState("idle");

  useEffect(() => { modeRef.current = mode; }, [mode]);
  useEffect(() => { palRef.current  = pal;  }, [pal]);
  useEffect(() => { yRef.current    = buildY; }, [buildY]);

  // ─── Three.js bootstrap ──────────────────────────────────────────────────────
  useEffect(() => {
    const el = mountRef.current;
    let W = el.clientWidth, H = el.clientHeight;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x03030a);
    scene.fog = new THREE.Fog(0x03030a, 60, 120);

    const cam = new THREE.PerspectiveCamera(55, W/H, 0.1, 400);
    const orbit = { theta:0.78, phi:1.08, r:28, tx:0, ty:1.5, tz:0 };
    applyOrbit(cam, orbit);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(W, H);
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    el.appendChild(renderer.domElement);

    // Lighting
    scene.add(new THREE.AmbientLight(0x0d0d28, 3));
    const key = new THREE.SpotLight(0xffe8c0, 5, 130, Math.PI/5.5, 0.45, 1.2);
    key.position.set(12, 38, 10); key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048); scene.add(key);
    const fill = new THREE.DirectionalLight(0x1122ff, 0.9);
    fill.position.set(-14, 8, -6); scene.add(fill);
    const rim = new THREE.DirectionalLight(0xff2200, 0.4);
    rim.position.set(3, 4, -22); scene.add(rim);

    // Floor + grid
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(80, 80),
      new THREE.MeshStandardMaterial({ color: 0x060614, roughness: 0.97 })
    );
    floor.rotation.x = -Math.PI/2; floor.receiveShadow = true; scene.add(floor);
    const grid = new THREE.GridHelper(40, 40, 0x120d28, 0x0a0820);
    grid.position.y = 0.002; scene.add(grid);

    // Stage perimeter
    scene.add(new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(20, 0.02, 20)),
      new THREE.LineBasicMaterial({ color: 0x5500ff })
    ));
    [[-10,0,-10],[10,0,-10],[-10,0,10],[10,0,10]].forEach(([x,,z]) => {
      const m = new THREE.Mesh(
        new THREE.BoxGeometry(0.15, 0.3, 0.15),
        new THREE.MeshStandardMaterial({ color: 0x8800ff, emissive: 0x4400ff, emissiveIntensity: 1 })
      );
      m.position.set(x, 0.15, z); scene.add(m);
    });

    // Ceiling rig
    scene.add(Object.assign(new THREE.GridHelper(26, 5, 0x1a0040, 0x0d0020), { position: new THREE.Vector3(0,16,0) }));
    scene.add(Object.assign(new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(24, 0.3, 24)),
      new THREE.LineBasicMaterial({ color: 0x220044 })
    ), { position: new THREE.Vector3(0,16,0) }));
    [[-7,15.6,-7],[7,15.6,-7],[-7,15.6,7],[7,15.6,7],[0,15.6,0]].forEach(([x,y,z],i) => {
      const fix = new THREE.Mesh(
        new THREE.CylinderGeometry(0.18, 0.28, 0.55, 8),
        new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.8 })
      );
      fix.position.set(x,y,z); scene.add(fix);
      const sl = new THREE.SpotLight(i===4?0xffffff:0xfff0cc, 1.2, 22, Math.PI/7, 0.6);
      sl.position.set(x, y-0.3, z); scene.add(sl);
    });

    // Raycast planes
    const castPlanes = [];
    for (let ly=0; ly<=25; ly++) {
      const p = new THREE.Mesh(
        new THREE.PlaneGeometry(40, 40),
        new THREE.MeshBasicMaterial({ visible: false, side: THREE.DoubleSide })
      );
      p.rotation.x = -Math.PI/2; p.position.y = ly+0.5; p.userData.ly = ly;
      scene.add(p); castPlanes.push(p);
    }

    // Scene objects
    const vcam = mkVirtualCamera();
    vcam.position.set(7, 3.5, 7); vcam.rotation.y = -Math.PI*0.73; scene.add(vcam);
    const actor = mkActor(); scene.add(actor);
    const ghost = new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshStandardMaterial({ color: 0xffffff, transparent: true, opacity: 0.22, depthWrite: false })
    );
    ghost.visible = false; scene.add(ghost);

    // ── VOLUME: replaces flat voxMap ──────────────────────────────────────────
    const volume = new VoxelVolume();

    // meshCache: key → THREE.Mesh  (only shell voxels have entries here)
    const meshCache = new Map();
    const bakedList = [];
    const rc = new THREE.Raycaster();

    engRef.current = { scene, cam, renderer, orbit, rc, volume, meshCache, bakedList, vcam, actor, ghost, castPlanes };

    // ── Shell sync: keeps meshCache matching volume.shell ────────────────────
    function syncShell() {
      const shell = volume.shell;

      // Remove meshes no longer in shell
      meshCache.forEach((mesh, k) => {
        if (!shell.has(k)) {
          scene.remove(mesh); mesh.geometry.dispose(); mesh.material.dispose();
          meshCache.delete(k);
        }
      });

      // Add new shell voxels
      shell.forEach(k => {
        if (meshCache.has(k)) return;
        const vd = volume._v.get(k);
        const { x, y, z } = fromVk(k);
        const mesh = new THREE.Mesh(
          new THREE.BoxGeometry(0.96, 0.96, 0.96),
          new THREE.MeshStandardMaterial({ color: vd.color, roughness: 0.65, metalness: 0.06 })
        );
        mesh.position.set(x, y, z);
        mesh.castShadow = mesh.receiveShadow = true;
        scene.add(mesh); meshCache.set(k, mesh);
      });

      const s = volume.stats();
      setStats(s);
    }

    function getNDC(e) {
      const r = el.getBoundingClientRect();
      return new THREE.Vector2(
        ((e.clientX - r.left) / W) * 2 - 1,
        -((e.clientY - r.top)  / H) * 2 + 1
      );
    }

    function castForPlacement(e) {
      rc.setFromCamera(getNDC(e), cam);
      const vHits = rc.intersectObjects([...meshCache.values()]);
      if (vHits.length) {
        const h = vHits[0];
        const n = h.face.normal.clone();
        const p = h.object.position.clone().add(n);
        return { pos: [p.x, p.y, p.z] };
      }
      const bHits = rc.intersectObjects(bakedList);
      if (bHits.length) {
        const h = bHits[0];
        const pt = h.point.clone().add(h.face.normal.clone().multiplyScalar(0.5));
        return { pos: [snap(pt.x), snap(pt.y), snap(pt.z)] };
      }
      const plane = castPlanes[yRef.current] || castPlanes[0];
      const pHits = rc.intersectObject(plane);
      if (pHits.length) {
        const pt = pHits[0].point;
        return { pos: [snap(pt.x), yRef.current+0.5, snap(pt.z)] };
      }
      return null;
    }

    function placeVoxel(x, y, z) {
      if (volume.has(x, y, z)) return;
      const hex = parseInt(palRef.current.slice(1), 16);
      volume.place(x, y, z, { color: hex });
      syncShell();
      const s = volume.stats();
      setStatus(
        `PLACED (${Math.round(x)},${Math.round(y-0.5)},${Math.round(z)}) · ` +
        `${s.shell} rendered · ${s.culled} interior culled (${s.pct}% hollow)`
      );
    }

    function removeVoxelAt(e) {
      rc.setFromCamera(getNDC(e), cam);
      const hits = rc.intersectObjects([...meshCache.values()]);
      if (!hits.length) return;
      const p = hits[0].object.position;
      volume.remove(p.x, p.y, p.z);
      syncShell();
      const s = volume.stats();
      setStatus(`REMOVED · ${s.shell} rendered · ${s.culled} culled`);
    }

    // Pointer
    let dn=false, btn=0, sx=0, sy=0, moved=false;
    const onDown = e => { dn=true; btn=e.button; sx=e.clientX; sy=e.clientY; moved=false; el.setPointerCapture(e.pointerId); };
    const onMove = e => {
      if (!dn && modeRef.current==="build") {
        const r = castForPlacement(e);
        if (r) { ghost.visible=true; ghost.material.color.setHex(parseInt(palRef.current.slice(1),16)); ghost.position.set(...r.pos); }
        else ghost.visible=false;
      }
      if (!dn) return;
      const dx=e.clientX-sx, dy=e.clientY-sy;
      if (Math.abs(dx)>2||Math.abs(dy)>2) moved=true;
      if (moved) {
        if (btn===0) { orbit.theta-=dx*0.0038; orbit.phi=Math.max(0.06,Math.min(1.56,orbit.phi+dy*0.0038)); }
        else if (btn===2) {
          const speed=orbit.r*0.0014;
          const right=new THREE.Vector3();
          right.crossVectors(cam.getWorldDirection(new THREE.Vector3()), new THREE.Vector3(0,1,0)).normalize();
          orbit.tx-=right.x*dx*speed; orbit.tz-=right.z*dx*speed; orbit.ty+=dy*speed*0.6;
        }
        applyOrbit(cam,orbit); sx=e.clientX; sy=e.clientY;
      }
    };
    const onUp = e => {
      if (!moved && modeRef.current==="build") {
        if (e.button===0) { const r=castForPlacement(e); if(r) placeVoxel(...r.pos); }
        else if (e.button===2) removeVoxelAt(e);
      }
      dn=false;
    };
    const onWheel = e => { orbit.r=Math.max(3,Math.min(90,orbit.r+e.deltaY*0.055)); applyOrbit(cam,orbit); };
    const onLeave = () => { ghost.visible=false; };

    el.addEventListener("pointerdown",  onDown);
    el.addEventListener("pointermove",  onMove);
    el.addEventListener("pointerup",    onUp);
    el.addEventListener("wheel",        onWheel, { passive:true });
    el.addEventListener("pointerleave", onLeave);
    el.addEventListener("contextmenu",  e=>e.preventDefault());

    // Animate
    let raf;
    const clock = new THREE.Clock();
    const animate = () => {
      raf = requestAnimationFrame(animate);
      const t = clock.getElapsedTime();
      actor.position.y = Math.sin(t*1.15)*0.018;
      if (vcam.userData.rec) vcam.userData.rec.material.emissiveIntensity = 1.5+Math.sin(t*4.5)*1.2;
      if (ghost.visible) ghost.material.opacity = 0.15+Math.sin(t*6)*0.08;
      renderer.render(scene, cam);
    };
    animate();

    const onResize = () => {
      W=el.clientWidth; H=el.clientHeight;
      cam.aspect=W/H; cam.updateProjectionMatrix(); renderer.setSize(W,H);
    };
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      el.removeEventListener("pointerdown",  onDown);
      el.removeEventListener("pointermove",  onMove);
      el.removeEventListener("pointerup",    onUp);
      el.removeEventListener("wheel",        onWheel);
      el.removeEventListener("pointerleave", onLeave);
      if (el.contains(renderer.domElement)) el.removeChild(renderer.domElement);
      renderer.dispose();
    };
  }, []);

  // ─── BAKE: hollow culling → face culling → greedy mesh ───────────────────────
  const handleBake = () => {
    const eng = engRef.current;
    if (!eng || eng.volume.size === 0) { setStatus("NOTHING TO BAKE"); return; }

    const volume = eng.volume;
    const totalRaw = volume.size;
    const s = volume.stats();

    // Get per-voxel exposed faces (hollow + face culled in one pass)
    const facedVoxels = volume.exposedFaces();
    const totalFaces = facedVoxels.reduce((acc,v)=>acc+v.faces.length,0);

    // Group by colour
    const colorGroups = new Map();
    facedVoxels.forEach(v => {
      if (!colorGroups.has(v.color)) colorGroups.set(v.color,[]);
      colorGroups.get(v.color).push(v);
    });

    // Remove preview meshes
    eng.meshCache.forEach(m => { eng.scene.remove(m); m.geometry.dispose(); m.material.dispose(); });
    eng.meshCache.clear();
    volume.clear();
    setStats({ total:0, shell:0, culled:0, pct:0 });

    const setName = `SET ${eng.bakedList.length+1}`;
    let meshCount = 0, triCount = 0;

    colorGroups.forEach((voxels, hex) => {
      // Greedy mesh: merges coplanar same-colour faces into minimal quads
      const geo = greedyMesh(voxels, hex);
      if (!geo) return;

      const triC = (geo.index?.count??0)/3;
      triCount += triC;

      const mat = new THREE.MeshStandardMaterial({ color:hex, roughness:0.62, metalness:0.08 });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.castShadow = mesh.receiveShadow = true;
      mesh.userData.setName = setName;
      eng.scene.add(mesh);
      eng.bakedList.push(mesh);
      meshCount++;
    });

    const entry = {
      name: setName,
      voxels: totalRaw,
      shellVoxels: s.shell,
      culledVoxels: s.culled,
      faces: totalFaces,
      tris: triCount,
      meshes: meshCount,
      colors: [...colorGroups.keys()],
    };
    setSets(prev => [...prev, entry]);

    // POST to backend — only shell voxels, with face data
    const payload = volume.toJSON ? null : null; // volume was cleared; use entry data
    fetch("/api/pubworld/props", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: setName,
        hollow: true,
        stats: { total:totalRaw, shell:s.shell, culled:s.culled, pct:s.pct },
        blocks: facedVoxels.map(v=>({
          x:v.x, y:v.y, z:v.z,
          color_hex:`#${v.color.toString(16).padStart(6,"0")}`,
          material: v.mat,
          exposed_faces: v.faces.map(f=>f.name),
          type: v.tag&&FUNCTIONAL.has(v.tag)?v.tag:"surface",
        })),
      }),
    }).catch(()=>{});

    setStatus(
      `✓ BAKED "${setName}" · ` +
      `${totalRaw} raw → ${s.shell} shell (${s.culled} interior culled) → ` +
      `${totalFaces} faces → ${triCount} tris · ${meshCount} draw call${meshCount!==1?"s":""}`
    );
  };

  // ─── Clear ───────────────────────────────────────────────────────────────────
  const handleClear = () => {
    const eng = engRef.current; if (!eng) return;
    eng.meshCache.forEach(m => { eng.scene.remove(m); m.geometry.dispose(); m.material.dispose(); });
    eng.meshCache.clear();
    eng.volume.clear();
    eng.bakedList.forEach(m => { eng.scene.remove(m); m.geometry.dispose(); m.material.dispose(); });
    eng.bakedList.length = 0;
    setSets([]); setStats({total:0,shell:0,culled:0,pct:0});
    setStatus("VOID CLEARED · FRESH START");
  };

  // ─── UI ──────────────────────────────────────────────────────────────────────
  const MODES = [
    { id:"build",   icon:"⬛", label:"BUILD"   },
    { id:"camera",  icon:"◎",  label:"CAMERA"  },
    { id:"capture", icon:"⚡", label:"CAPTURE" },
    { id:"review",  icon:"▶",  label:"REVIEW"  },
  ];

  const S = {
    root:    { width:"100%", height:"100vh", background:"#02020a", display:"flex", flexDirection:"column", fontFamily:'"Share Tech Mono","Courier New",monospace', color:"#fff", overflow:"hidden", userSelect:"none" },
    topbar:  { display:"flex", alignItems:"center", height:44, background:"linear-gradient(90deg,#08001a,#04000e)", borderBottom:"1px solid #2200aa", padding:"0 14px", gap:14, flexShrink:0 },
    modeBtn: (a)=>({ background:a?"#18006a":"transparent", border:a?"1px solid #6600ff":"1px solid #1a0040", color:a?"#cc88ff":"#44226a", padding:"4px 16px", cursor:"pointer", fontSize:10, letterSpacing:2, fontFamily:"inherit", transition:"all .15s" }),
    sidebar: { width:168, background:"#030212", borderRight:"1px solid #120030", padding:"12px 10px", display:"flex", flexDirection:"column", gap:10, flexShrink:0, overflowY:"auto" },
    rPanel:  { width:162, background:"#030212", borderLeft:"1px solid #120030", padding:"12px 10px", display:"flex", flexDirection:"column", gap:8, flexShrink:0, overflowY:"auto" },
    label:   { fontSize:9, color:"#440077", letterSpacing:2 },
    divider: { borderBottom:"1px solid #0f0025", paddingBottom:6, marginBottom:2 },
    btn:     (c="#aa55ff")=>({ background:"#0a0022", border:"1px solid #250050", color:c, padding:"6px 8px", cursor:"pointer", fontSize:9, letterSpacing:1, fontFamily:"inherit", width:"100%", textAlign:"left" }),
    status:  { height:26, background:"#020109", borderTop:"1px solid #0d001e", display:"flex", alignItems:"center", padding:"0 14px", flexShrink:0 },
    swatch:  (c,sel)=>({ width:22, height:22, background:c, border:sel?"2px solid #fff":"2px solid transparent", cursor:"pointer", borderRadius:2, flexShrink:0 }),
    sceneRow:{ display:"flex", alignItems:"center", gap:6, padding:"5px 7px", background:"#07001a", border:"1px solid #140030", marginBottom:2 },
    stat:    { fontSize:8, color:"#553377", letterSpacing:1, lineHeight:1.8 },
  };

  return (
    <div style={S.root}>
      <div style={S.topbar}>
        <div style={{ display:"flex", alignItems:"center", gap:8 }}>
          <div style={{ width:9, height:9, borderRadius:"50%", background:"#ff2200", boxShadow:"0 0 8px #ff2200", flexShrink:0 }} />
          <span style={{ color:"#cc44ff", fontWeight:"bold", fontSize:14, letterSpacing:4 }}>PUB WORLD</span>
          <span style={{ color:"#2a0055", fontSize:11 }}>|</span>
          <span style={{ color:"#550099", fontSize:9, letterSpacing:3 }}>PUBCAST AI</span>
        </div>
        <div style={{ flex:1, display:"flex", gap:3, justifyContent:"center" }}>
          {MODES.map(m=>(
            <button key={m.id} onClick={()=>setMode(m.id)} style={S.modeBtn(mode===m.id)}>
              {m.icon} {m.label}
            </button>
          ))}
        </div>
        <div style={{ display:"flex", gap:6, alignItems:"center" }}>
          <div style={{ width:7, height:7, borderRadius:"50%", background:recState==="recording"?"#ff0000":"#220033", boxShadow:recState==="recording"?"0 0 6px #ff0000":"none" }} />
          <span style={{ fontSize:9, color:"#330055", letterSpacing:2 }}>3D STUDIO</span>
        </div>
      </div>

      <div style={{ flex:1, display:"flex", overflow:"hidden" }}>

        {/* Left panel */}
        <div style={S.sidebar}>
          <div style={{ ...S.label, ...S.divider }}>TOOLS</div>

          {mode==="build" && <>
            <div style={S.label}>VOXEL COLOR</div>
            <div style={{ display:"flex", flexWrap:"wrap", gap:3, marginBottom:2 }}>
              {SWATCHES.map(c=>(
                <div key={c} style={S.swatch(c,pal===c)} onClick={()=>setPal(c)} />
              ))}
            </div>
            <input type="color" value={pal} onChange={e=>setPal(e.target.value)}
              style={{ width:"100%", height:28, border:"1px solid #2200aa", background:"#000", cursor:"pointer", marginBottom:2 }} />

            <div style={S.label}>BUILD HEIGHT  Y={buildY}</div>
            <div style={{ display:"flex", gap:4 }}>
              <button onClick={()=>setBuildY(v=>Math.max(0,v-1))} style={{ ...S.btn(), flex:1, textAlign:"center" }}>▼</button>
              <div style={{ flex:1, display:"flex", alignItems:"center", justifyContent:"center", fontSize:11, color:"#8844cc" }}>{buildY}</div>
              <button onClick={()=>setBuildY(v=>Math.min(25,v+1))} style={{ ...S.btn(), flex:1, textAlign:"center" }}>▲</button>
            </div>

            {/* Live hollow stats */}
            <div style={{ ...S.stat, marginTop:6 }}>
              <div>TOTAL: {stats.total}</div>
              <div style={{ color:"#4488ff" }}>SHELL: {stats.shell}</div>
              <div style={{ color:"#ff6600" }}>CULLED: {stats.culled} ({stats.pct}%)</div>
            </div>

            <button onClick={handleBake} style={{ ...S.btn("#bb77ff"), padding:"9px 8px", fontSize:10, letterSpacing:2, marginTop:4 }}>
              ⬡ BAKE TO MESH
            </button>
            <button onClick={handleClear} style={{ ...S.btn("#ff3333"), fontSize:9 }}>
              ✕ CLEAR VOID
            </button>

            <div style={{ ...S.label, marginTop:6, lineHeight:1.7 }}>
              LMB: Place voxel<br/>
              RMB: Remove voxel<br/>
              Drag: Orbit camera<br/>
              R-drag: Pan<br/>
              Scroll: Zoom
            </div>
          </>}

          {mode==="camera" && (
            <div style={{ ...S.label, lineHeight:2.0 }}>
              Virtual camera is live in scene.<br/><br/>
              Orbit: Left drag<br/>Pan: Right drag<br/>Zoom: Scroll<br/><br/>
              POV mode coming<br/>in next iteration.
            </div>
          )}

          {mode==="capture" && <>
            <div style={S.label}>CAPTURE</div>
            <div style={{ ...S.label, color:"#553388", lineHeight:1.8, marginBottom:6 }}>
              Motion Capture<br/>Performance Capture<br/>Object Tracking
            </div>
            {["▶  RECORD","⏸  PAUSE","⏹  STOP","⊕  ADD MARKER"].map((l,i)=>(
              <button key={l} onClick={()=>{
                if(i===0) setRecState("recording");
                if(i===1) setRecState("paused");
                if(i===2){ setRecState("idle"); setStatus("CAPTURE SESSION ENDED"); }
              }} style={{ ...S.btn(i===0?"#ff4444":i===3?"#44cc88":"#aa55ff"), marginBottom:2 }}>{l}</button>
            ))}
            {recState!=="idle" && (
              <div style={{ display:"flex", gap:5, alignItems:"center", marginTop:6 }}>
                <div style={{ width:7, height:7, borderRadius:"50%", background:recState==="recording"?"#ff0000":"#ffaa00", boxShadow:`0 0 6px ${recState==="recording"?"#ff0000":"#ffaa00"}` }} />
                <span style={{ fontSize:9, color:recState==="recording"?"#ff5555":"#ffaa44", letterSpacing:1 }}>
                  {recState==="recording"?"RECORDING":"PAUSED"}
                </span>
              </div>
            )}
          </>}

          {mode==="review" && (
            <div style={{ ...S.label, lineHeight:2.0, color:"#553388" }}>
              Review & Playback<br/><br/>
              Reskin pipeline:<br/>
              Capture → Bake → <br/>
              Replace geometry<br/>
              with hi-fidelity<br/>
              model via data.<br/><br/>
              No limit to final<br/>output quality.
            </div>
          )}
        </div>

        {/* 3D Viewport */}
        <div ref={mountRef} style={{ flex:1, position:"relative", overflow:"hidden" }}>
          <div style={{ position:"absolute", top:10, left:12, pointerEvents:"none" }}>
            <div style={{ fontSize:9, color:"#330066", letterSpacing:2 }}>VOID SPACE · 3D PRODUCTION STUDIO</div>
            {mode==="build" && <div style={{ fontSize:8, color:"#220044", marginTop:3, letterSpacing:1 }}>
              CLICK TO PLACE · RIGHT CLICK TO ERASE · DRAG TO ORBIT
            </div>}
          </div>
          <div style={{ position:"absolute", top:10, right:12, pointerEvents:"none", textAlign:"right" }}>
            <div style={{ fontSize:9, color:"#220044", letterSpacing:1 }}>MODE: {mode.toUpperCase()}</div>
            <div style={{ fontSize:8, color:"#18002a", letterSpacing:1, marginTop:2 }}>Y LAYER: {buildY}</div>
            {stats.culled>0 && <div style={{ fontSize:8, color:"#ff6600", letterSpacing:1, marginTop:2 }}>
              {stats.culled} INTERIOR VOXELS HOLLOW
            </div>}
          </div>
          {[{t:0,l:0},{t:0,r:0},{b:0,l:0},{b:0,r:0}].map((p,i)=>(
            <div key={i} style={{ position:"absolute",...p,width:20,height:20,
              borderTop:(p.t===0)?"1px solid #2200aa":"none",
              borderBottom:(p.b===0)?"1px solid #2200aa":"none",
              borderLeft:(p.l===0)?"1px solid #2200aa":"none",
              borderRight:(p.r===0)?"1px solid #2200aa":"none",
              pointerEvents:"none" }} />
          ))}
        </div>

        {/* Right panel - Scene outliner */}
        <div style={S.rPanel}>
          <div style={{ ...S.label, ...S.divider }}>SCENE</div>
          <div style={{ ...S.label, fontSize:8, color:"#330055", marginBottom:4 }}>PERMANENT OBJECTS</div>
          <div style={S.sceneRow}>
            <span style={{ fontSize:11 }}>◎</span>
            <div>
              <div style={{ fontSize:8, color:"#8855ff", letterSpacing:1 }}>VIRTUAL CAMERA</div>
              <div style={{ fontSize:7, color:"#330044" }}>REC ACTIVE</div>
            </div>
          </div>
          <div style={S.sceneRow}>
            <span style={{ fontSize:11 }}>▣</span>
            <div>
              <div style={{ fontSize:8, color:"#3388ff", letterSpacing:1 }}>PERFORMER ACTOR</div>
              <div style={{ fontSize:7, color:"#330044" }}>MOCAP MARKERS ×11</div>
            </div>
          </div>

          {sets.length>0 && <>
            <div style={{ ...S.label, fontSize:8, color:"#330055", marginTop:8, marginBottom:4 }}>BAKED SETS</div>
            {sets.map((s,i)=>(
              <div key={i} style={{ ...S.sceneRow, flexDirection:"column", alignItems:"flex-start", gap:3 }}>
                <div style={{ display:"flex", gap:5, alignItems:"center" }}>
                  <div style={{ display:"flex", gap:2 }}>
                    {s.colors.map(c=>(
                      <div key={c} style={{ width:8, height:8, background:`#${c.toString(16).padStart(6,"0")}`, borderRadius:1 }} />
                    ))}
                  </div>
                  <span style={{ fontSize:8, color:"#cc8800", letterSpacing:1 }}>{s.name}</span>
                </div>
                <div style={{ fontSize:7, color:"#442200" }}>
                  {s.voxels}v raw · {s.shellVoxels} shell · {s.culledVoxels} hollow
                </div>
                <div style={{ fontSize:7, color:"#335500" }}>
                  {s.faces} faces → {s.tris} tris · {s.meshes} draw call{s.meshes!==1?"s":""}
                </div>
              </div>
            ))}
          </>}

          {sets.length===0 && stats.total===0 && (
            <div style={{ fontSize:8, color:"#1a0033", lineHeight:1.8, marginTop:8 }}>
              Place voxels to<br/>build set pieces.<br/>Bake them into<br/>
              hollow solid mesh.<br/>Interior faces<br/>auto-culled.<br/>
              Piece by piece,<br/>build your world.
            </div>
          )}

          {stats.total>0 && (
            <div style={{ ...S.sceneRow, flexDirection:"column", alignItems:"flex-start", gap:2, marginTop:4, borderColor:"#3a1a00" }}>
              <div style={{ fontSize:8, color:"#ffaa44", letterSpacing:1 }}>VOXEL BUFFER</div>
              <div style={{ fontSize:7, color:"#4488ff" }}>{stats.shell} rendered</div>
              <div style={{ fontSize:7, color:"#ff6600" }}>{stats.culled} hollow ({stats.pct}%)</div>
              <div style={{ width:"100%", height:2, background:"#110500", borderRadius:1, marginTop:2 }}>
                <div style={{ height:"100%", width:`${Math.min(100,(stats.total/200)*100)}%`, background:"#ff6600", borderRadius:1, transition:"width .2s" }} />
              </div>
            </div>
          )}
        </div>
      </div>

      <div style={S.status}>
        <div style={{ width:6, height:6, borderRadius:"50%", background:"#4400ff", marginRight:8, flexShrink:0 }} />
        <span style={{ fontSize:9, color:"#553377", letterSpacing:1 }}>{status}</span>
      </div>
    </div>
  );
}
