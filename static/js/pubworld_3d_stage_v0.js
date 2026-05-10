// static/js/pubworld_3d_stage_v0.js
// Pubcast AI 3D Studio Stage v0 preview builder.
// Creates the contained movie-studio soundstage/backlot environment from JSON primitives.
// Reference photos were used only as conceptual guidance; no reference image asset is imported here.

export async function loadPubcast3DStage({ THREE, scene, metadataUrl, sceneUrl }) {
  const [metadata, stageDef] = await Promise.all([
    fetch(metadataUrl).then(r => {
      if (!r.ok) throw new Error(`Could not load metadata: ${metadataUrl}`);
      return r.json();
    }),
    fetch(sceneUrl).then(r => {
      if (!r.ok) throw new Error(`Could not load scene definition: ${sceneUrl}`);
      return r.json();
    }),
  ]);

  if (metadata.environment_id !== 'pubworld_3d_stage_v0') {
    throw new Error(`Wrong environment_id: ${metadata.environment_id}`);
  }

  const root = new THREE.Group();
  root.name = 'pubworld_3d_stage_v0_root';
  root.userData = { metadata, stageDef };
  scene.add(root);

  const materials = buildMaterials(THREE, stageDef.materials || {});

  for (const primitive of stageDef.primitives || []) {
    const mesh = buildPrimitive(THREE, primitive, materials);
    if (mesh) root.add(mesh);
  }

  const labelRoot = buildLabels(THREE, metadata);
  root.add(labelRoot);

  addStageLighting(THREE, scene, metadata);

  return { root, metadata, stageDef };
}

function buildMaterials(THREE, materialDefs) {
  const result = {};
  for (const [id, def] of Object.entries(materialDefs)) {
    const opacity = def.opacity ?? 1;
    result[id] = new THREE.MeshStandardMaterial({
      color: new THREE.Color(def.color || '#777777'),
      roughness: def.roughness ?? 0.8,
      metalness: def.metalness ?? 0.0,
      transparent: opacity < 1,
      opacity,
      side: THREE.DoubleSide,
    });
  }
  return result;
}

function buildPrimitive(THREE, primitive, materials) {
  const type = primitive.type || 'box';
  const scale = primitive.scale || [1, 1, 1];
  let geometry;

  if (type === 'box') {
    geometry = new THREE.BoxGeometry(scale[0], scale[1], scale[2]);
  } else if (type === 'cylinder') {
    geometry = new THREE.CylinderGeometry(scale[0], scale[2] || scale[0], scale[1], 48);
  } else if (type === 'sphere') {
    geometry = new THREE.SphereGeometry(scale[0], 32, 16);
  } else {
    console.warn('Unknown Pubcast stage primitive type:', primitive);
    return null;
  }

  const material = materials[primitive.material] || new THREE.MeshStandardMaterial({ color: 0x777777 });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = primitive.id || 'pubcast_stage_primitive';
  mesh.position.set(...(primitive.position || [0, 0, 0]));

  if (primitive.rotation) {
    mesh.rotation.set(
      THREE.MathUtils.degToRad(primitive.rotation[0] || 0),
      THREE.MathUtils.degToRad(primitive.rotation[1] || 0),
      THREE.MathUtils.degToRad(primitive.rotation[2] || 0),
    );
  }

  mesh.castShadow = type !== 'cylinder';
  mesh.receiveShadow = true;
  mesh.userData = { pubcastPrimitive: primitive };
  return mesh;
}

function addStageLighting(THREE, scene, metadata) {
  scene.add(new THREE.HemisphereLight(0xd7e7ff, 0x0a0805, 0.45));
  scene.add(new THREE.AmbientLight(0x303030, 0.25));

  const key = new THREE.DirectionalLight(0xffffff, 1.2);
  key.position.set(-8, 16, -4);
  key.castShadow = true;
  scene.add(key);

  const backlot = new THREE.DirectionalLight(0xfff2cc, 0.7);
  backlot.position.set(-20, 18, 55);
  scene.add(backlot);

  const practicalPoints = metadata.lighting_points || {};
  for (const [id, lightDef] of Object.entries(practicalPoints)) {
    const p = metadataPointToThree(lightDef.position);
    const light = new THREE.PointLight(id.includes('backlot') ? 0xffe5aa : 0xe5c17e, id.includes('practical') ? 0.9 : 0.55, 28);
    light.name = id;
    light.position.set(p.x, p.y, p.z);
    scene.add(light);
  }
}

function buildLabels(THREE, metadata) {
  const group = new THREE.Group();
  group.name = 'pubworld_3d_stage_v0_labels';

  const markers = metadata.transition_markers || {};
  for (const [id, marker] of Object.entries(markers)) {
    const p = metadataPointToThree(marker.position);
    const label = makeSpriteLabel(THREE, readable(id), id.includes('bellamy') ? '#4dc8ff' : '#22dd66');
    label.position.set(p.x, p.y + 1.5, p.z);
    label.userData = { transitionMarker: id, marker };
    group.add(label);
  }

  const buildZones = metadata.build_zones || {};
  for (const [id, zone] of Object.entries(buildZones)) {
    const center = boundsCenter(zone.bounds);
    const p = metadataPointToThree(center);
    const label = makeSpriteLabel(THREE, readable(id), '#d7b21f');
    label.position.set(p.x, 0.9, p.z);
    label.userData = { buildZone: id, zone };
    group.add(label);
  }

  return group;
}

function metadataPointToThree(position) {
  // Metadata points are [x, z, y] style for legacy Pubcast notes.
  // Preview scene uses Three.js [x, y, z].
  return { x: position[0] || 0, y: position[2] || 0, z: position[1] || 0 };
}

function boundsCenter(bounds) {
  if (!bounds || !bounds.min || !bounds.max) return [0, 0, 0];
  return [
    (bounds.min[0] + bounds.max[0]) / 2,
    (bounds.min[1] + bounds.max[1]) / 2,
    (bounds.min[2] + bounds.max[2]) / 2,
  ];
}

function readable(id) {
  return id.replace(/^transition_/, '').replace(/_/g, ' ').toUpperCase();
}

function makeSpriteLabel(THREE, text, color) {
  const canvas = document.createElement('canvas');
  canvas.width = 1024;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgba(0,0,0,0.72)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = color;
  ctx.lineWidth = 6;
  ctx.strokeRect(4, 4, canvas.width - 8, canvas.height - 8);
  ctx.fillStyle = color;
  ctx.font = 'bold 42px Courier New, monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);

  const texture = new THREE.CanvasTexture(canvas);
  texture.needsUpdate = true;
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(8, 1, 1);
  return sprite;
}

export function createPubcastStageCameras(THREE, metadata, aspect) {
  const cameras = {};
  for (const [id, def] of Object.entries(metadata.camera_points || {})) {
    const camera = new THREE.PerspectiveCamera(def.fov || 55, aspect, 0.1, 250);
    const p = metadataPointToThree(def.position);
    const t = metadataPointToThree(def.look_at || [0, 0, 0]);
    camera.name = id;
    camera.position.set(p.x, p.y, p.z);
    camera.lookAt(t.x, t.y, t.z);
    cameras[id] = camera;
  }
  return cameras;
}

export function installPubcastStageWalkControls({ THREE, camera, domElement }) {
  const keys = new Set();
  const state = { enabled: true, speed: 18, turnSpeed: 1.7 };

  window.addEventListener('keydown', event => keys.add(event.key.toLowerCase()));
  window.addEventListener('keyup', event => keys.delete(event.key.toLowerCase()));

  let last = performance.now();
  function tick(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;

    if (state.enabled) {
      const forward = new THREE.Vector3();
      camera.getWorldDirection(forward);
      forward.y = 0;
      forward.normalize();
      const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize().multiplyScalar(-1);

      if (keys.has('w')) camera.position.addScaledVector(forward, state.speed * dt);
      if (keys.has('s')) camera.position.addScaledVector(forward, -state.speed * dt);
      if (keys.has('a')) camera.position.addScaledVector(right, -state.speed * dt);
      if (keys.has('d')) camera.position.addScaledVector(right, state.speed * dt);
      if (keys.has('q')) camera.position.y -= state.speed * 0.6 * dt;
      if (keys.has('e')) camera.position.y += state.speed * 0.6 * dt;
      if (keys.has('arrowleft')) camera.rotation.y += state.turnSpeed * dt;
      if (keys.has('arrowright')) camera.rotation.y -= state.turnSpeed * dt;
      if (keys.has('arrowup')) camera.rotation.x += state.turnSpeed * 0.6 * dt;
      if (keys.has('arrowdown')) camera.rotation.x -= state.turnSpeed * 0.6 * dt;
    }

    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  return state;
}
