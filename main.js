import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const EARTH_RADIUS = 1;
const MARKER_LIFT = 0.004;

const TEX = {
  earth:  'https://unpkg.com/three-globe@2.31.1/example/img/earth-blue-marble.jpg',
  bump:   'https://unpkg.com/three-globe@2.31.1/example/img/earth-topology.png',
  clouds: 'https://unpkg.com/three-globe@2.31.1/example/img/earth-clouds.png',
};

// ---------------------------------------------------------------------------
// Scene
// ---------------------------------------------------------------------------
const app = document.getElementById('app');
const tooltipEl = document.getElementById('tooltip');
const loaderEl = document.getElementById('loader');
const hudEl = document.getElementById('hud');
const zoomFillEl = document.getElementById('zoom-fill');
const zoomLabelEl = document.getElementById('zoom-level');

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x000308);

const camera = new THREE.PerspectiveCamera(
  45,
  window.innerWidth / window.innerHeight,
  0.01,
  500
);
camera.position.set(0, 0.6, 3.2);

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
app.appendChild(renderer.domElement);

// ---------------------------------------------------------------------------
// Stars
// ---------------------------------------------------------------------------
function buildStars() {
  const n = 6000;
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const r = 80 + Math.random() * 40;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    pos[i * 3 + 0] = r * Math.sin(phi) * Math.cos(theta);
    pos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    pos[i * 3 + 2] = r * Math.cos(phi);
  }
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.PointsMaterial({
    color: 0xffffff,
    size: 0.06,
    sizeAttenuation: true,
    transparent: true,
    opacity: 0.85,
    depthWrite: false,
  });
  scene.add(new THREE.Points(geo, mat));
}
buildStars();

// ---------------------------------------------------------------------------
// Lighting
// ---------------------------------------------------------------------------
scene.add(new THREE.AmbientLight(0xffffff, 0.4));
const sun = new THREE.DirectionalLight(0xffffff, 1.2);
sun.position.set(5, 2, 4);
scene.add(sun);

// ---------------------------------------------------------------------------
// Earth
// ---------------------------------------------------------------------------
const loadingMgr = new THREE.LoadingManager();
const loader = new THREE.TextureLoader(loadingMgr);

const earthTex = loader.load(TEX.earth);
earthTex.colorSpace = THREE.SRGBColorSpace;
earthTex.anisotropy = renderer.capabilities.getMaxAnisotropy();
const bumpTex = loader.load(TEX.bump);
const cloudsTex = loader.load(TEX.clouds);

const earth = new THREE.Mesh(
  new THREE.SphereGeometry(EARTH_RADIUS, 128, 128),
  new THREE.MeshPhongMaterial({
    map: earthTex,
    bumpMap: bumpTex,
    bumpScale: 0.02,
    specular: new THREE.Color(0x1a2c44),
    shininess: 9,
  })
);
scene.add(earth);

const clouds = new THREE.Mesh(
  new THREE.SphereGeometry(EARTH_RADIUS * 1.006, 96, 96),
  new THREE.MeshLambertMaterial({
    map: cloudsTex,
    transparent: true,
    opacity: 0.38,
    depthWrite: false,
  })
);
scene.add(clouds);

// Atmosphere glow (back-side fresnel)
const atmosphere = new THREE.Mesh(
  new THREE.SphereGeometry(EARTH_RADIUS * 1.06, 64, 64),
  new THREE.ShaderMaterial({
    vertexShader: `
      varying vec3 vNormal;
      void main() {
        vNormal = normalize(normalMatrix * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      varying vec3 vNormal;
      void main() {
        float intensity = pow(0.65 - dot(vNormal, vec3(0.0, 0.0, 1.0)), 2.0);
        gl_FragColor = vec4(0.32, 0.62, 1.0, 1.0) * intensity;
      }
    `,
    blending: THREE.AdditiveBlending,
    side: THREE.BackSide,
    transparent: true,
    depthWrite: false,
  })
);
scene.add(atmosphere);

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.rotateSpeed = 0.5;
controls.zoomSpeed = 0.8;
controls.panSpeed = 0.5;
controls.enablePan = false;
controls.minDistance = 1.12;
controls.maxDistance = 8;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.25;

let resumeRotateTimer = null;
controls.addEventListener('start', () => {
  controls.autoRotate = false;
  if (resumeRotateTimer) clearTimeout(resumeRotateTimer);
});
controls.addEventListener('end', () => {
  if (resumeRotateTimer) clearTimeout(resumeRotateTimer);
  resumeRotateTimer = setTimeout(() => { controls.autoRotate = true; }, 5000);
});

// ---------------------------------------------------------------------------
// Lat/Lon helpers
// ---------------------------------------------------------------------------
function latLonToVec3(lat, lon, r = EARTH_RADIUS) {
  const phi = (90 - lat) * Math.PI / 180;
  const theta = (lon + 180) * Math.PI / 180;
  return new THREE.Vector3(
    -r * Math.sin(phi) * Math.cos(theta),
    r * Math.cos(phi),
    r * Math.sin(phi) * Math.sin(theta)
  );
}

function vec3ToLatLon(v) {
  const n = v.clone().normalize();
  const lat = 90 - (Math.acos(n.y) * 180) / Math.PI;
  const lon = ((Math.atan2(n.z, -n.x) * 180) / Math.PI) - 180;
  return {
    lat,
    lon: ((lon + 540) % 360) - 180,
  };
}

// ---------------------------------------------------------------------------
// LOD: pick a step (degrees) based on camera distance
// ---------------------------------------------------------------------------
function getLOD() {
  const d = camera.position.length();
  // step (deg), label, fillPercent
  if (d > 4.5)  return { step: 30, label: 'جهانی',   fill: 0.10, max: 80 };
  if (d > 3.0)  return { step: 20, label: 'جهانی',   fill: 0.22, max: 110 };
  if (d > 2.2)  return { step: 12, label: 'قاره‌ای',  fill: 0.40, max: 140 };
  if (d > 1.7)  return { step: 7,  label: 'منطقه‌ای', fill: 0.58, max: 170 };
  if (d > 1.4)  return { step: 4,  label: 'محلی',     fill: 0.75, max: 200 };
  if (d > 1.22) return { step: 2,  label: 'دقیق',     fill: 0.90, max: 220 };
  return         { step: 1,  label: 'بسیار دقیق', fill: 1.0,  max: 240 };
}

// Visible-cap angle from the camera direction
function visibleAngle() {
  const d = camera.position.length();
  // angular radius of the visible cap (where horizon is)
  const cap = Math.acos(Math.min(1, EARTH_RADIUS / d));
  // a little margin so points near horizon are included
  return Math.min(Math.PI, cap + 0.18);
}

function generateGrid() {
  const { step, max } = getLOD();
  const camDir = camera.position.clone().normalize();
  const cap = visibleAngle();

  const points = [];
  const latStart = -80;
  const latEnd = 80;
  for (let lat = latStart; lat <= latEnd; lat += step) {
    // skip-step at high latitudes to avoid bunching at poles
    const lonStep = step / Math.max(0.2, Math.cos(lat * Math.PI / 180));
    for (let lon = -180; lon < 180; lon += lonStep) {
      const p = latLonToVec3(lat, lon, 1);
      const ang = p.angleTo(camDir);
      if (ang < cap) {
        // priority = closer to camera direction first
        points.push({ lat, lon, score: ang });
      }
    }
  }
  points.sort((a, b) => a.score - b.score);
  return points.slice(0, max);
}

// ---------------------------------------------------------------------------
// Open-Meteo: WMO weather codes → emoji + label
// ---------------------------------------------------------------------------
const WMO = {
  0:  ['☀️', 'صاف'],
  1:  ['🌤️', 'بیشتر صاف'],
  2:  ['⛅', 'کمی ابری'],
  3:  ['☁️', 'ابری'],
  45: ['🌫️', 'مه'],
  48: ['🌫️', 'مه یخ‌زده'],
  51: ['🌦️', 'نم‌نم باران'],
  53: ['🌦️', 'نم‌نم باران'],
  55: ['🌦️', 'نم‌نم شدید'],
  56: ['🌧️', 'باران یخ‌زده'],
  57: ['🌧️', 'باران یخ‌زده شدید'],
  61: ['🌧️', 'باران سبک'],
  63: ['🌧️', 'باران'],
  65: ['🌧️', 'باران شدید'],
  66: ['🌧️', 'باران یخ‌زده'],
  67: ['🌧️', 'باران یخ‌زده شدید'],
  71: ['🌨️', 'برف سبک'],
  73: ['🌨️', 'برف'],
  75: ['🌨️', 'برف شدید'],
  77: ['🌨️', 'دانه‌های برف'],
  80: ['🌧️', 'رگبار سبک'],
  81: ['🌧️', 'رگبار'],
  82: ['⛈️', 'رگبار شدید'],
  85: ['🌨️', 'بارش برف'],
  86: ['🌨️', 'بارش برف شدید'],
  95: ['⛈️', 'رعد و برق'],
  96: ['⛈️', 'رعد و تگرگ'],
  99: ['⛈️', 'رعد و تگرگ شدید'],
};

function describeWeather(code) {
  return WMO[code] || ['❓', '—'];
}

function tempToColor(t) {
  if (typeof t !== 'number' || isNaN(t)) return new THREE.Color(0x888888);
  // -30 °C (deep blue) → 0 (cyan) → 20 (green/yellow) → 40+ (red)
  const v = Math.max(0, Math.min(1, (t + 30) / 70));
  const c = new THREE.Color();
  // hue: 0.66 (blue) → 0.0 (red)
  c.setHSL((1 - v) * 0.66, 0.85, 0.55);
  return c;
}

// ---------------------------------------------------------------------------
// Weather data: cache + batched fetch
// ---------------------------------------------------------------------------
const weatherCache = new Map(); // key "lat|lon|step" → { temp, code, ts }
const CACHE_TTL = 15 * 60 * 1000;

function cacheKey(lat, lon, step) {
  // snap to step grid so cache reuses across passes
  const s = step;
  const slat = (Math.round(lat / s) * s).toFixed(2);
  const slon = (Math.round(lon / s) * s).toFixed(2);
  return `${slat}|${slon}|${s}`;
}

async function fetchBatch(points) {
  if (!points.length) return [];
  // Open-Meteo accepts comma-joined coordinates and returns an array.
  // Keep batches reasonably small to stay within URL length safe range.
  const CHUNK = 90;
  const all = [];
  for (let i = 0; i < points.length; i += CHUNK) {
    const slice = points.slice(i, i + CHUNK);
    const lats = slice.map(p => p.lat.toFixed(4)).join(',');
    const lons = slice.map(p => p.lon.toFixed(4)).join(',');
    const url =
      'https://api.open-meteo.com/v1/forecast' +
      `?latitude=${lats}&longitude=${lons}` +
      '&current=temperature_2m,weather_code,wind_speed_10m' +
      '&timezone=UTC';
    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('http ' + resp.status);
      const data = await resp.json();
      const arr = Array.isArray(data) ? data : [data];
      for (let k = 0; k < slice.length; k++) {
        const d = arr[k] || {};
        const cur = d.current || {};
        all.push({
          lat: slice[k].lat,
          lon: slice[k].lon,
          temp: cur.temperature_2m,
          code: cur.weather_code,
          wind: cur.wind_speed_10m,
        });
      }
    } catch (e) {
      // swallow; just leave those points without data this round
      console.warn('weather fetch failed:', e);
    }
  }
  return all;
}

// ---------------------------------------------------------------------------
// Markers
// ---------------------------------------------------------------------------
const markersGroup = new THREE.Group();
markersGroup.renderOrder = 2;
scene.add(markersGroup);

const markerGeo = new THREE.SphereGeometry(1, 8, 8);

function clearMarkers() {
  for (const m of markersGroup.children) {
    m.material.dispose();
  }
  markersGroup.clear();
}

function buildMarkers(records, step) {
  clearMarkers();
  const baseR = Math.max(0.006, Math.min(0.018, step * 0.0015));
  for (const r of records) {
    if (r.temp == null) continue;
    const color = tempToColor(r.temp);
    const mat = new THREE.MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.92,
      depthWrite: false,
    });
    const m = new THREE.Mesh(markerGeo, mat);
    const pos = latLonToVec3(r.lat, r.lon, EARTH_RADIUS + MARKER_LIFT);
    m.position.copy(pos);
    m.scale.setScalar(baseR);
    m.userData = r;
    markersGroup.add(m);
  }
}

// ---------------------------------------------------------------------------
// Update loop: when camera settles, refresh weather
// ---------------------------------------------------------------------------
let lastCamKey = '';
let refreshTimer = null;

function camKey() {
  const p = camera.position;
  const d = p.length();
  // round so micro-movement doesn't trigger refresh
  return [
    p.x.toFixed(2),
    p.y.toFixed(2),
    p.z.toFixed(2),
    d.toFixed(2),
  ].join(',');
}

async function refreshWeather() {
  const lod = getLOD();
  zoomFillEl.style.width = `${Math.round(lod.fill * 100)}%`;
  zoomLabelEl.textContent = lod.label;

  const points = generateGrid();
  // pull cached + queue uncached
  const need = [];
  const cached = [];
  for (const p of points) {
    const key = cacheKey(p.lat, p.lon, lod.step);
    const hit = weatherCache.get(key);
    if (hit && Date.now() - hit.ts < CACHE_TTL) {
      cached.push({ lat: p.lat, lon: p.lon, ...hit });
    } else {
      need.push(p);
    }
  }

  // render cached immediately
  buildMarkers(cached, lod.step);

  if (need.length) {
    const fresh = await fetchBatch(need);
    for (const r of fresh) {
      if (r.temp == null) continue;
      const key = cacheKey(r.lat, r.lon, lod.step);
      weatherCache.set(key, { temp: r.temp, code: r.code, wind: r.wind, ts: Date.now() });
    }
    // rebuild with cached + fresh combined
    const combined = [...cached, ...fresh.filter(r => r.temp != null)];
    buildMarkers(combined, lod.step);
  }
}

function scheduleRefresh() {
  const key = camKey();
  if (key === lastCamKey) return;
  lastCamKey = key;
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { refreshWeather(); }, 350);
}

// ---------------------------------------------------------------------------
// Hover tooltip via raycaster
// ---------------------------------------------------------------------------
const raycaster = new THREE.Raycaster();
raycaster.params.Mesh.threshold = 0;
const mouseNDC = new THREE.Vector2(-2, -2);
let lastMouseX = 0, lastMouseY = 0;

renderer.domElement.addEventListener('pointermove', (e) => {
  const rect = renderer.domElement.getBoundingClientRect();
  lastMouseX = e.clientX;
  lastMouseY = e.clientY;
  mouseNDC.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouseNDC.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
});

renderer.domElement.addEventListener('pointerleave', () => {
  mouseNDC.set(-2, -2);
  tooltipEl.classList.add('hidden');
});

function updateTooltip() {
  if (mouseNDC.x < -1.5) return;
  raycaster.setFromCamera(mouseNDC, camera);
  const hits = raycaster.intersectObjects(markersGroup.children, false);
  if (hits.length) {
    const r = hits[0].object.userData;
    const [emoji, cond] = describeWeather(r.code);
    const t = typeof r.temp === 'number' ? `${Math.round(r.temp)}°C` : '—';
    const wind = typeof r.wind === 'number' ? `${Math.round(r.wind)} km/h` : '';
    const ns = r.lat >= 0 ? 'N' : 'S';
    const ew = r.lon >= 0 ? 'E' : 'W';
    tooltipEl.innerHTML = `
      <div class="tt-row">
        <span class="tt-emoji">${emoji}</span>
        <span class="tt-temp">${t}</span>
        <span class="tt-cond">${cond}</span>
      </div>
      ${wind ? `<div class="tt-row"><span style="color:#8aa2c2">باد</span><span>${wind}</span></div>` : ''}
      <div class="tt-coord">${Math.abs(r.lat).toFixed(1)}°${ns} · ${Math.abs(r.lon).toFixed(1)}°${ew}</div>
    `;
    tooltipEl.style.left = `${lastMouseX}px`;
    tooltipEl.style.top = `${lastMouseY - 8}px`;
    tooltipEl.classList.remove('hidden');
  } else {
    tooltipEl.classList.add('hidden');
  }
}

// ---------------------------------------------------------------------------
// Resize
// ---------------------------------------------------------------------------
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ---------------------------------------------------------------------------
// Main render loop
// ---------------------------------------------------------------------------
const clock = new THREE.Clock();

function animate() {
  const dt = clock.getDelta();
  controls.update();

  // gentle independent cloud drift
  clouds.rotation.y += dt * 0.008;

  scheduleRefresh();
  updateTooltip();

  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}

// Wait for textures, then reveal
loadingMgr.onLoad = () => {
  loaderEl.classList.add('hidden');
  hudEl.classList.remove('hidden');
  refreshWeather();
};
loadingMgr.onError = (url) => {
  console.error('texture failed:', url);
  loaderEl.classList.add('hidden');
  hudEl.classList.remove('hidden');
  refreshWeather();
};

animate();
