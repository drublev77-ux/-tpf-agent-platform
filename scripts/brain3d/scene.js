/* TPF hero: the graphite brain from the Figma image as a live 3D scene (cortex mesh from the MNI ICBM152 MRI template).
   Loaded by index.html only after the page has loaded, on screens that can run WebGL; until then, and wherever it cannot run,
   the same picture stays as /brand-hero.jpg. Settings chosen by the owner: rotation 2.4x, glow 1.6x, circuits 1.6x, network 80%. */
(function () {
  var stage = document.getElementById('hero3d');
  if (!stage || !window.THREE) return;
  var canvas = document.createElement('canvas'); canvas.setAttribute('aria-hidden', 'true'); stage.appendChild(canvas);
  function fail() { if (canvas.parentNode) canvas.parentNode.removeChild(canvas); }
  var renderer;
  try { renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, powerPreference: 'low-power' }); } catch (e) { fail(); return; }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.25));
  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(30, 16 / 9, 0.1, 100);
  camera.position.set(0, 0.15, 5.7); camera.lookAt(0, -0.12, 0);
  var composer = null, bloom = null;
  if (THREE.EffectComposer && THREE.UnrealBloomPass) {
    composer = new THREE.EffectComposer(renderer);
    composer.addPass(new THREE.RenderPass(scene, camera));
    bloom = new THREE.UnrealBloomPass(new THREE.Vector2(512, 512), 0.85, 0.55, 0.42);
    composer.addPass(bloom);
  }

  var bgMat = new THREE.ShaderMaterial({ depthWrite: false, depthTest: false,
    vertexShader: 'varying vec2 vUv; void main(){ vUv=uv; gl_Position=vec4(position.xy,0.9999,1.0); }',
    fragmentShader: 'varying vec2 vUv; void main(){ vec2 d=(vUv-vec2(0.5,0.56))*vec2(1.5,1.0); float r=length(d);' +
      ' vec3 c=mix(vec3(0.05,0.17,0.22),vec3(0.012,0.03,0.045),smoothstep(0.0,0.75,r)); c=mix(c,vec3(0.006,0.012,0.018),smoothstep(0.6,1.1,r));' +
      ' gl_FragColor=vec4(c,1.0); }' });
  var bg = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), bgMat); bg.frustumCulled = false; bg.renderOrder = -1;
  scene.add(bg);
  renderer.setClearColor(0x05090D, 1);

  var seed = 7; function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }

  // ---------- brain material: graphite metal, cracked plates, amber sulci, cyan circuits ----------
  var brainMat = new THREE.ShaderMaterial({
    uniforms: { uTime: { value: 0 }, uGlow: { value: 1 }, uTrace: { value: 1 } },
    extensions: { derivatives: true },
    vertexShader: [
      'attribute float aFold; attribute float aCav; varying float vFold; varying float vCav; varying vec3 vN; varying vec3 vV; varying vec3 vP;',
      'void main(){ vFold=aFold; vCav=aCav; vP=position; vec4 mv=modelViewMatrix*vec4(position,1.0);',
      ' vN=normalize(normalMatrix*normal); vV=normalize(-mv.xyz); gl_Position=projectionMatrix*mv; }'
    ].join('\n'),
    fragmentShader: [
      'uniform float uTime; uniform float uGlow; uniform float uTrace;',
      'varying float vFold; varying float vCav; varying vec3 vN; varying vec3 vV; varying vec3 vP;',
      'vec3 h3(vec3 p){ p=vec3(dot(p,vec3(127.1,311.7,74.7)),dot(p,vec3(269.5,183.3,246.1)),dot(p,vec3(113.5,271.9,124.6))); return fract(sin(p)*43758.5453); }',
      'float h1(vec3 p){ return fract(sin(dot(p,vec3(12.9898,78.233,37.719)))*43758.5453); }',
      // Voronoi: distance to nearest feature point, edge distance (F2-F1), id of the cell
      'void voro(vec3 x, out float f1, out float edge, out vec3 cid){',
      ' vec3 p=floor(x), f=fract(x); float d1=9.0, d2=9.0; cid=p;',
      ' for(int k=-1;k<=1;k++) for(int j=-1;j<=1;j++) for(int i=-1;i<=1;i++){',
      '  vec3 b=vec3(float(i),float(j),float(k)); vec3 r=b+h3(p+b)*0.8+0.1-f; float d=dot(r,r);',
      '  if(d<d1){ d2=d1; d1=d; cid=p+b; } else if(d<d2){ d2=d; } }',
      ' f1=sqrt(d1); edge=sqrt(d2)-f1; }',
      'void main(){',
      ' vec3 N=normalize(vN), V=normalize(vV), R=reflect(-V,N);',
      ' float crest=smoothstep(0.62,0.18,vFold);',          // 1 on top of a gyrus
      ' float occ=1.0-0.88*smoothstep(0.3,0.92,vFold);',     // deep sulci are dark
      // studio environment for the metal: bright softbox above, dark floor, cyan side light, warm bounce
      ' float up=R.y;',
      ' vec3 env=mix(vec3(0.01,0.015,0.02),vec3(0.62,0.64,0.66),smoothstep(-0.05,0.9,up));',
      ' env+=vec3(0.15,0.75,0.95)*0.55*smoothstep(0.35,1.0,abs(R.x))*(1.0-smoothstep(0.4,0.95,up));',
      ' env+=vec3(1.0,0.5,0.15)*0.22*smoothstep(-0.1,-0.9,up);',
      ' float ndv=max(dot(N,V),0.0); float fres=0.28+0.72*pow(1.0-ndv,3.0);',
      ' vec3 L=normalize(vec3(-0.35,0.75,0.6)); float diff=max(dot(N,L),0.0);',
      ' vec3 H=normalize(L+V); float sp=pow(max(dot(N,H),0.0),90.0)*0.9+pow(max(dot(N,H),0.0),16.0)*0.12;',
      ' vec3 base=vec3(0.11,0.115,0.12);',
      ' vec3 col=base*(0.2+0.85*diff)+env*fres*0.62+vec3(1.0,0.97,0.92)*sp;',
      // armor plates: fine cracks across the gyri
      ' float cf1, ce; vec3 cid; voro(vP*7.5+0.15*sin(vP.yzx*9.0), cf1, ce, cid);',
      ' float crack=smoothstep(0.055,0.0,ce)*crest;',
      ' col*=1.0-0.75*crack;',
      ' col*=occ;',
      // amber light glowing out of the sulci, pulsing slowly
      ' float wave=0.75+0.25*sin(uTime*1.3-vP.z*2.2+vP.y*1.4);',
      ' float fw=max(fwidth(vFold),0.004);',
      ' float lineC=1.0-smoothstep(0.0,fw*1.1,abs(vFold-0.42));',
      ' float lineW=1.0-smoothstep(0.0,fw*3.5+0.03,abs(vFold-0.45));',
      ' float g=smoothstep(0.7,1.0,vFold);',
      ' vec3 amber=vec3(1.0,0.42,0.06);',
      ' col+=amber*(lineW*0.5+g*0.32+smoothstep(0.5,1.0,vCav)*0.25)*wave*uGlow+vec3(1.0,0.66,0.28)*lineC*0.75*wave*uGlow;',
      ' col+=vec3(1.0,0.45,0.08)*crack*0.25*smoothstep(0.2,0.6,vFold+0.3)*uGlow;',     // some cracks catch the fire
      // tiny amber sparks inside the grooves
      ' float spk=step(0.985,h1(floor(vP*22.0)))*g*smoothstep(0.35,0.1,length(fract(vP*22.0)-0.5));',
      ' col+=vec3(1.0,0.75,0.35)*spk*(0.6+0.6*sin(uTime*4.0+h1(floor(vP*22.0))*30.0))*2.0*uGlow;',
      // cyan circuit traces along cell borders, round nodes with dark centres
      ' float tf1, te; vec3 tid; voro(vP*3.6, tf1, te, tid);',
      ' float on=step(0.62,h1(tid));',
      ' float trace=smoothstep(0.04,0.01,te)*on;',
      ' float ring=smoothstep(0.03,0.012,abs(tf1-0.075))*step(0.45,h1(tid+3.1));',
      ' float dotc=smoothstep(0.035,0.0,tf1)*step(0.45,h1(tid+3.1));',
      ' float tmask=crest*uTrace;',
      ' float flow=0.65+0.35*sin(uTime*2.0+h1(tid)*20.0);',
      ' col=mix(col,col*0.35,ring*tmask*0.6);',
      ' col+=vec3(0.3,0.88,1.0)*(trace*1.5*flow+ring*2.0+dotc*1.4)*tmask;',
      // cyan rim from the backdrop
      ' col+=vec3(0.25,0.8,1.0)*pow(1.0-ndv,3.5)*0.32;',
      ' gl_FragColor=vec4(col,1.0); }'
    ].join('\n')
  });

  var brain = new THREE.Group();
  brain.rotation.set(0.06, -Math.PI / 2, 0);
  brain.scale.setScalar(1.1);
  scene.add(brain);
  var loaded = false;
  fetch('/brain-mesh.bin').then(function (r) { if (!r.ok) throw new Error(r.status); return r.arrayBuffer(); }).then(function (buf) {
    var h = new Uint32Array(buf, 0, 2), nv = h[0], nf = h[1], bb = new Float32Array(buf, 8, 6);
    var q = new Uint16Array(buf, 32, nv * 3), sul = new Uint8Array(buf, 32 + nv * 6, nv);
    var off = 32 + nv * 7; off += (4 - off % 4) % 4;
    var idx = new Uint32Array(buf, off, nf * 3), pos = new Float32Array(nv * 3), fold = new Float32Array(nv);
    for (var k = 0; k < nv; k++) {
      for (var a = 0; a < 3; a++) pos[k * 3 + a] = bb[a] + (bb[a + 3] - bb[a]) * q[k * 3 + a] / 65535;
      fold[k] = sul[k] / 255;
    }
    var g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('aFold', new THREE.BufferAttribute(fold, 1));
    g.setIndex(new THREE.BufferAttribute(idx, 1));
    g.computeVertexNormals();
    var nrm = g.attributes.normal.array;
    for (k = 0; k < nv; k++) {                     // deepen the sulci so gyri read as fat, separate plates
      var px = pos[k * 3], py = pos[k * 3 + 1], pz = pos[k * 3 + 2], pl = Math.sqrt(px * px + py * py + pz * pz) || 1;
      var out = Math.max(0, (nrm[k * 3] * px + nrm[k * 3 + 1] * py + nrm[k * 3 + 2] * pz) / pl);
      var dd = -0.06 * Math.pow(fold[k], 1.5) * Math.min(1, out * 2.5);
      pos[k * 3] += nrm[k * 3] * dd; pos[k * 3 + 1] += nrm[k * 3 + 1] * dd; pos[k * 3 + 2] += nrm[k * 3 + 2] * dd;
    }
    g.attributes.position.needsUpdate = true; g.computeVertexNormals();
    // concavity: how far a vertex sits below the average of its neighbours, along the normal; traces every sulcus as a continuous line
    nrm = g.attributes.normal.array;
    var sum = new Float32Array(nv * 3), cnt = new Uint16Array(nv), cav = new Float32Array(nv), tmp = new Float32Array(nv);
    for (var f = 0; f < nf * 3; f += 3) for (var e2 = 0; e2 < 3; e2++) {
      var va = idx[f + e2], vb = idx[f + (e2 + 1) % 3];
      sum[va * 3] += pos[vb * 3]; sum[va * 3 + 1] += pos[vb * 3 + 1]; sum[va * 3 + 2] += pos[vb * 3 + 2]; cnt[va]++;
      sum[vb * 3] += pos[va * 3]; sum[vb * 3 + 1] += pos[va * 3 + 1]; sum[vb * 3 + 2] += pos[va * 3 + 2]; cnt[vb]++;
    }
    for (k = 0; k < nv; k++) { var c = cnt[k] || 1;
      cav[k] = (sum[k * 3] / c - pos[k * 3]) * nrm[k * 3] + (sum[k * 3 + 1] / c - pos[k * 3 + 1]) * nrm[k * 3 + 1] + (sum[k * 3 + 2] / c - pos[k * 3 + 2]) * nrm[k * 3 + 2]; }
    for (var pass = 0; pass < 2; pass++) {             // light smoothing so the lines are clean
      tmp.fill(0); var tc = new Uint16Array(nv);
      for (f = 0; f < nf * 3; f += 3) for (e2 = 0; e2 < 3; e2++) { va = idx[f + e2]; vb = idx[f + (e2 + 1) % 3]; tmp[va] += cav[vb]; tc[va]++; tmp[vb] += cav[va]; tc[vb]++; }
      for (k = 0; k < nv; k++) cav[k] = 0.5 * cav[k] + 0.5 * tmp[k] / (tc[k] || 1);
    }
    var sorted = Array.prototype.slice.call(cav).sort(function (x, y) { return x - y; }), hi = sorted[Math.floor(nv * 0.93)] || 1;
    for (k = 0; k < nv; k++) cav[k] = Math.max(0, Math.min(1, cav[k] / hi));
    g.setAttribute('aCav', new THREE.BufferAttribute(cav, 1));
    brain.add(new THREE.Mesh(g, brainMat));
    loaded = true;
  }).catch(function () { fail(); });

  // ---------- soft round sprite for glowing points ----------
  function glowTex() {
    var c = document.createElement('canvas'); c.width = c.height = 64; var x = c.getContext('2d');
    var gr = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(0.18, 'rgba(255,255,255,0.95)'); gr.addColorStop(0.4, 'rgba(255,255,255,0.3)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = gr; x.fillRect(0, 0, 64, 64); var t = new THREE.CanvasTexture(c); return t;
  }
  var SPR = glowTex();
  function pointsMat(size, color, op) { return new THREE.PointsMaterial({ size: size, map: SPR, color: color, transparent: true, opacity: op, blending: THREE.AdditiveBlending, depthWrite: false }); }

  // ---------- hexagonal network behind ----------
  var net = new THREE.Group(), nodes = [], links = [], linePts = [], R = 0.5, key = {};
  function nodeAt(x, y) { var k = x.toFixed(3) + ',' + y.toFixed(3); if (!key[k]) { key[k] = new THREE.Vector3(x, y, -2.4 - 0.05 * (x * x)); nodes.push(key[k]); } return key[k]; }
  for (var col = -8; col <= 8; col++) for (var row = -4; row <= 4; row++) {
    var cx = col * R * 1.5, cy = row * R * Math.sqrt(3) + (col & 1 ? R * Math.sqrt(3) / 2 : 0);
    if (rnd() < 0.1) continue;
    for (var e = 0; e < 6; e++) {
      var a1 = Math.PI / 3 * e, a2 = Math.PI / 3 * (e + 1);
      var p1 = nodeAt(cx + R * Math.cos(a1), cy + R * Math.sin(a1)), p2 = nodeAt(cx + R * Math.cos(a2), cy + R * Math.sin(a2));
      var lk = p1.x < p2.x || (p1.x === p2.x && p1.y < p2.y) ? [p1, p2] : [p2, p1], id = nodes.indexOf(lk[0]) + '-' + nodes.indexOf(lk[1]);
      if (!key[id]) { key[id] = 1; links.push(lk); linePts.push(lk[0], lk[1]); }
    }
  }
  function fadeColors(pts) { var c = new Float32Array(pts.length * 3); pts.forEach(function (v, n) { var f = Math.max(0, Math.min(1, (v.y + 1.9) / 1.6)) * Math.max(0.25, 1 - Math.abs(v.x) / 9); c[n * 3] = c[n * 3 + 1] = c[n * 3 + 2] = f; }); return new THREE.BufferAttribute(c, 3); }
  var lineMat = new THREE.LineBasicMaterial({ color: '#2CC4E6', vertexColors: true, transparent: true, opacity: 0.4, blending: THREE.AdditiveBlending, depthWrite: false });
  var nodeMat = pointsMat(0.36, '#5FE4FA', 0.95), haloMat = pointsMat(1.0, '#1FA9D6', 0.22);
  nodeMat.vertexColors = haloMat.vertexColors = true;
  var nodeGeo = new THREE.BufferGeometry().setFromPoints(nodes); nodeGeo.setAttribute('color', fadeColors(nodes));
  var lineGeo = new THREE.BufferGeometry().setFromPoints(linePts); lineGeo.setAttribute('color', fadeColors(linePts));
  net.add(new THREE.LineSegments(lineGeo, lineMat));
  net.add(new THREE.Points(nodeGeo, haloMat)); net.add(new THREE.Points(nodeGeo, nodeMat));
  var MAXS = 50, pulseGeo = new THREE.BufferGeometry(), pulsePos = new Float32Array(MAXS * 3), pulses = [];
  for (var i = 0; i < MAXS; i++) pulses.push({ l: links[Math.floor(rnd() * links.length)], t: rnd(), s: 0.25 + rnd() * 0.5 });
  pulseGeo.setAttribute('position', new THREE.BufferAttribute(pulsePos, 3));
  var pulseMat = pointsMat(0.22, '#A8F3FF', 0.9);
  net.add(new THREE.Points(pulseGeo, pulseMat));
  scene.add(net);

  // ---------- glowing ring platform under the brain ----------
  var platMat = new THREE.ShaderMaterial({
    uniforms: { uTime: { value: 0 }, uOp: { value: 1 } }, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
    vertexShader: 'varying vec2 vUv; void main(){ vUv=uv*2.0-1.0; gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0); }',
    fragmentShader: [
      'uniform float uTime; uniform float uOp; varying vec2 vUv;',
      'float h(float x){ return fract(sin(x*91.7)*43758.5); }',
      'void main(){ float r=length(vUv); float a=atan(vUv.y,vUv.x);',
      ' float c=0.0;',
      ' for(int i=0;i<7;i++){ float fi=float(i); float rr=0.12+fi*0.125; float w=0.006+0.004*h(fi);',
      '  float ring=smoothstep(w,0.0,abs(r-rr));',
      '  float dir=mod(fi,2.0)<1.0?1.0:-1.0; float seg=step(0.25+0.4*h(fi+3.0),fract(a/6.2832*(6.0+fi*3.0)+uTime*0.04*dir*(1.0+fi*0.3)));',
      '  c+=ring*mix(1.0,seg,step(1.5,fi))*(1.2-fi*0.12); }',
      ' float wave=smoothstep(0.03,0.0,abs(r-fract(uTime*0.18)*0.95))*0.6*(1.0-r);',
      ' float disc=exp(-r*r*12.0)*0.22;',
      ' float fade=smoothstep(1.0,0.6,r);',
      ' vec3 col=vec3(0.28,0.86,1.0)*(c+wave)+vec3(0.5,0.95,1.0)*disc;',
      ' gl_FragColor=vec4(col*fade*uOp,1.0); }'
    ].join('\n')
  });
  var plat = new THREE.Mesh(new THREE.PlaneGeometry(5.2, 5.2), platMat);
  plat.rotation.x = -Math.PI / 2; plat.position.y = -1.45;
  scene.add(plat);

  // rising particles above the platform and dust in the air
  var NP = 260, partGeo = new THREE.BufferGeometry(), partPos = new Float32Array(NP * 3), parts = [];
  for (i = 0; i < NP; i++) { var ang = rnd() * 6.283, rad = Math.sqrt(rnd()) * 2.4; parts.push({ x: Math.cos(ang) * rad, z: Math.sin(ang) * rad, y: rnd() * 2.4, v: 0.08 + rnd() * 0.25 }); }
  partGeo.setAttribute('position', new THREE.BufferAttribute(partPos, 3));
  var partMat = pointsMat(0.07, '#7FEBFF', 0.85);
  scene.add(new THREE.Points(partGeo, partMat));
  var dustPts = [];
  for (i = 0; i < 160; i++) dustPts.push(new THREE.Vector3((rnd() - 0.5) * 12, (rnd() - 0.5) * 6, -1 - rnd() * 2.5));
  var dustMat = pointsMat(0.06, '#9BEFFF', 0.5);
  scene.add(new THREE.Points(new THREE.BufferGeometry().setFromPoints(dustPts), dustMat));

  // ---------- the owner's settings ----------
  var S = { speed: 2.4, glow: 1.6, trace: 1.6, net: 0.8 };
  brainMat.uniforms.uGlow.value = S.glow; brainMat.uniforms.uTrace.value = S.trace;
  if (bloom) bloom.strength = Math.min(0.9, 0.55 + 0.35 * Math.max(S.glow, S.net));   // capped: at glow 1.6 a stronger halo washes the metal out
  lineMat.opacity = 0.5 * S.net; nodeMat.opacity = S.net; haloMat.opacity = 0.28 * S.net; pulseMat.opacity = S.net;
  partMat.opacity = S.net; dustMat.opacity = 0.55 * S.net; platMat.uniforms.uOp.value = 0.35 + 0.65 * S.net;

  // ---------- size, drag, animation ----------
  function resize() {
    var w = stage.clientWidth, h = stage.clientHeight; if (!w || !h) return;
    renderer.setSize(w, h, false); if (composer) composer.setSize(w, h);
    camera.aspect = w / h; camera.position.z = w / h < 1.3 ? 8.0 : 5.7;
    // on a wide screen the layer spans the whole first screen and the brain sits where the picture has it (69% across, right of the text)
    if (w > 860) camera.setViewOffset(w, h, -0.19 * w, 0, w, h); else camera.clearViewOffset();
    camera.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize); resize();

  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var base = reduce ? 0.0012 : 0.004, dragging = false, lastX = 0, vel = 0, tilt = 0.06;
  canvas.addEventListener('pointerdown', function (e) { if (e.pointerType !== 'mouse') return; dragging = true; lastX = e.clientX; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', function (e) {
    if (!dragging) return; var dx = e.clientX - lastX; lastX = e.clientX;
    brain.rotation.y += dx * 0.01; vel = dx * 0.0006;
    tilt = Math.max(-0.4, Math.min(0.5, tilt + e.movementY * 0.004));
  });
  canvas.addEventListener('pointerup', function () { dragging = false; });

  // render only while the hero is on screen and the tab is open
  var clock = new THREE.Clock(), visible = true, onScreen = true, shown = false;
  document.addEventListener('visibilitychange', function () { visible = !document.hidden; });
  if ('IntersectionObserver' in window) new IntersectionObserver(function (es) { onScreen = es[0].isIntersecting; }).observe(stage);
  function frame() {
    requestAnimationFrame(frame);
    if (!visible || !onScreen || !loaded) { clock.getDelta(); return; }
    var dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
    brainMat.uniforms.uTime.value = t; platMat.uniforms.uTime.value = t;
    if (!dragging) { brain.rotation.y += base * S.speed + vel; vel *= 0.95; }
    brain.rotation.x += (tilt - brain.rotation.x) * 0.08;
    brain.position.y = 0.05 + Math.sin(t * 0.8) * 0.05;
    net.rotation.y = Math.sin(t * 0.12) * 0.05;
    for (var k = 0; k < MAXS; k++) {
      var s = pulses[k]; s.t += s.s * dt; if (s.t > 1) { s.t = 0; s.l = links[Math.floor(Math.random() * links.length)]; }
      var a = s.l[0], b = s.l[1];
      pulsePos[k * 3] = a.x + (b.x - a.x) * s.t; pulsePos[k * 3 + 1] = a.y + (b.y - a.y) * s.t; pulsePos[k * 3 + 2] = a.z + (b.z - a.z) * s.t;
    }
    pulseGeo.attributes.position.needsUpdate = true;
    for (k = 0; k < NP; k++) {
      var p = parts[k]; p.y += p.v * dt; if (p.y > 2.4) p.y = 0;
      var spread = 1 - p.y / 3.2;
      partPos[k * 3] = p.x * spread; partPos[k * 3 + 1] = -1.43 + p.y * 0.55; partPos[k * 3 + 2] = p.z * spread;
    }
    partGeo.attributes.position.needsUpdate = true;
    if (composer) composer.render(); else renderer.render(scene, camera);
    if (!shown) { shown = true; stage.classList.add('on'); }      // fade in over the picture once the first frame is drawn
  }
  frame();
})();
