"use client";

import { useEffect, useRef } from "react";
import * as THREE from "three";
import { PROVIDER_META, Provider } from "@/lib/api";

const ORDER: Provider[] = ["claude", "chatgpt", "gemini"];

/**
 * The SASI core: three model "agents" orbit a shared core and send energy into it
 * when they are working. Pure three.js, no external assets.
 */
export default function Scene3D({ active = [], hero = false }: { active?: string[]; hero?: boolean }) {
  const mountRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<string[]>(active);
  activeRef.current = active;
  const heroRef = useRef(hero);
  heroRef.current = hero;

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      return; // No WebGL: the CSS gradient background still looks fine.
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(mount.clientWidth, mount.clientHeight);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    mount.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x0a0820, 0.035);
    const camera = new THREE.PerspectiveCamera(45, mount.clientWidth / mount.clientHeight, 0.1, 200);
    camera.position.set(0, 1.6, 11);

    scene.add(new THREE.AmbientLight(0x6060a0, 0.6));
    const key = new THREE.PointLight(0xffffff, 40, 40);
    key.position.set(4, 6, 6);
    scene.add(key);

    // Everything that moves together; shifted aside on wide screens so text stays readable.
    const root = new THREE.Group();
    scene.add(root);

    // Core: faceted crystal inside a slowly turning wireframe shell.
    const core = new THREE.Group();
    root.add(core);
    const crystal = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1.05, 1),
      new THREE.MeshPhysicalMaterial({
        color: 0xb9a8ff, emissive: 0x3b2a9a, emissiveIntensity: 0.9, roughness: 0.15,
        metalness: 0.2, clearcoat: 1, flatShading: true,
      }),
    );
    core.add(crystal);
    const shell = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(1.75, 1)),
      new THREE.LineBasicMaterial({ color: 0x8f7dff, transparent: true, opacity: 0.35 }),
    );
    core.add(shell);
    const coreLight = new THREE.PointLight(0x9b8cff, 25, 12);
    core.add(coreLight);

    // Halo sprite behind the core.
    const haloTex = (() => {
      const c = document.createElement("canvas");
      c.width = c.height = 128;
      const g = c.getContext("2d")!;
      const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
      grd.addColorStop(0, "rgba(255,255,255,1)");
      grd.addColorStop(0.25, "rgba(170,150,255,0.5)");
      grd.addColorStop(1, "rgba(0,0,0,0)");
      g.fillStyle = grd;
      g.fillRect(0, 0, 128, 128);
      return new THREE.CanvasTexture(c);
    })();
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTex, color: 0x9b8cff, transparent: true, opacity: 0.55, depthWrite: false, blending: THREE.AdditiveBlending }));
    halo.scale.set(6, 6, 1);
    core.add(halo);

    // Agents.
    type Agent = { id: Provider; mesh: THREE.Mesh; glow: THREE.Sprite; light: THREE.PointLight; beam: THREE.Line; ring: THREE.Mesh; phase: number; level: number; pulses: THREE.Mesh[] };
    const agents: Agent[] = ORDER.map((id, i) => {
      const color = new THREE.Color(PROVIDER_META[id].color);
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(0.42, 48, 48),
        new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.5, roughness: 0.3, metalness: 0.1 }),
      );
      const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTex, color, transparent: true, opacity: 0.5, depthWrite: false, blending: THREE.AdditiveBlending }));
      glow.scale.set(2.4, 2.4, 1);
      mesh.add(glow);
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(0.68, 0.015, 8, 64),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.6 }),
      );
      mesh.add(ring);
      const light = new THREE.PointLight(color, 6, 8);
      mesh.add(light);
      root.add(mesh);
      const beamGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
      const beam = new THREE.Line(beamGeo, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.15, blending: THREE.AdditiveBlending }));
      root.add(beam);
      const pulses = Array.from({ length: 4 }, () => {
        const p = new THREE.Mesh(new THREE.SphereGeometry(0.06, 12, 12), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0 }));
        root.add(p);
        return p;
      });
      return { id, mesh, glow, light, beam, ring, phase: (i / ORDER.length) * Math.PI * 2, level: 0, pulses };
    });

    // Orbit guide.
    const orbit = new THREE.Mesh(
      new THREE.TorusGeometry(4.2, 0.008, 6, 200),
      new THREE.MeshBasicMaterial({ color: 0x5a4fb0, transparent: true, opacity: 0.35 }),
    );
    orbit.rotation.x = Math.PI / 2 - 0.25;
    root.add(orbit);

    // Star field.
    const starGeo = new THREE.BufferGeometry();
    const N = 1400;
    const pos = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) {
      const r = 18 + Math.random() * 40;
      const t = Math.random() * Math.PI * 2;
      const p = Math.acos(2 * Math.random() - 1);
      pos[i * 3] = r * Math.sin(p) * Math.cos(t);
      pos[i * 3 + 1] = r * Math.sin(p) * Math.sin(t);
      pos[i * 3 + 2] = r * Math.cos(p);
    }
    starGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0xc8c0ff, size: 0.08, transparent: true, opacity: 0.8 }));
    scene.add(stars);

    const mouse = { x: 0, y: 0 };
    const onMove = (e: PointerEvent) => {
      mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
      mouse.y = (e.clientY / window.innerHeight) * 2 - 1;
    };
    window.addEventListener("pointermove", onMove);
    const onResize = () => {
      if (!mount.clientWidth) return;
      camera.aspect = mount.clientWidth / mount.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(mount.clientWidth, mount.clientHeight);
    };
    window.addEventListener("resize", onResize);

    const clock = new THREE.Clock();
    let frame = 0;
    const tmp = new THREE.Vector3();
    const render = () => {
      frame = requestAnimationFrame(render);
      if (document.hidden) return;
      const dt = Math.min(clock.getDelta(), 0.05);
      const t = clock.elapsedTime;
      const speed = reduceMotion ? 0.15 : 1;
      const live = activeRef.current;
      const anyActive = live.length > 0;

      core.rotation.y += dt * 0.25 * speed * (anyActive ? 2.2 : 1);
      core.rotation.x = Math.sin(t * 0.3) * 0.15;
      shell.rotation.y -= dt * 0.4 * speed;
      const breathe = 1 + Math.sin(t * 2) * (anyActive ? 0.06 : 0.025);
      crystal.scale.setScalar(breathe);
      (halo.material as THREE.SpriteMaterial).opacity = 0.45 + (anyActive ? 0.25 : 0) + Math.sin(t * 2) * 0.05;
      coreLight.intensity = 20 + (anyActive ? 20 : 0);

      agents.forEach((a) => {
        const on = live.includes(a.id);
        a.level += ((on ? 1 : 0) - a.level) * Math.min(1, dt * 4);
        const ang = a.phase + t * 0.22 * speed;
        const r = 4.2 - a.level * 0.6;
        a.mesh.position.set(Math.cos(ang) * r, Math.sin(ang * 2) * 0.35 + Math.sin(ang) * r * Math.sin(0.25), Math.sin(ang) * r * Math.cos(0.25));
        a.mesh.scale.setScalar(1 + a.level * 0.25 + Math.sin(t * 5) * 0.03 * a.level);
        (a.mesh.material as THREE.MeshStandardMaterial).emissiveIntensity = 0.45 + a.level * 1.2;
        (a.glow.material as THREE.SpriteMaterial).opacity = 0.35 + a.level * 0.5;
        a.light.intensity = 4 + a.level * 14;
        a.ring.rotation.x = t * 1.2;
        a.ring.rotation.y = t * 0.8;
        const beamPos = a.beam.geometry.attributes.position as THREE.BufferAttribute;
        beamPos.setXYZ(0, a.mesh.position.x, a.mesh.position.y, a.mesh.position.z);
        beamPos.setXYZ(1, 0, 0, 0);
        beamPos.needsUpdate = true;
        (a.beam.material as THREE.LineBasicMaterial).opacity = 0.12 + a.level * 0.6;
        a.pulses.forEach((p, k) => {
          const f = (t * 0.6 + k / a.pulses.length) % 1;
          tmp.copy(a.mesh.position).multiplyScalar(1 - f);
          p.position.copy(tmp);
          (p.material as THREE.MeshBasicMaterial).opacity = a.level * Math.sin(f * Math.PI) * 0.9;
        });
      });

      // Layout: beside the hero text on wide screens, lower and smaller on phones, centred and calm elsewhere.
      const wide = camera.aspect > 1.15;
      const tx = heroRef.current && wide ? 3.6 : 0;
      const ty = heroRef.current && !wide ? -3.2 : 0;
      const ts = heroRef.current ? (wide ? 0.85 : 0.7) : 0.9;
      root.position.x += (tx - root.position.x) * 0.05;
      root.position.y += (ty - root.position.y) * 0.05;
      root.scale.setScalar(root.scale.x + (ts - root.scale.x) * 0.05);

      stars.rotation.y += dt * 0.01 * speed;
      camera.position.x += (mouse.x * 1.2 - camera.position.x) * 0.03;
      camera.position.y += (1.6 - mouse.y * 0.8 - camera.position.y) * 0.03;
      camera.lookAt(0, 0, 0);
      renderer.render(scene, camera);
    };
    render();

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("resize", onResize);
      scene.traverse((o) => {
        const m = o as THREE.Mesh;
        m.geometry?.dispose?.();
        const mat = m.material as THREE.Material | THREE.Material[] | undefined;
        if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
        else mat?.dispose?.();
      });
      haloTex.dispose();
      renderer.dispose();
      mount.removeChild(renderer.domElement);
    };
  }, []);

  return <div ref={mountRef} className={hero ? "scene" : "scene dim"} aria-hidden="true" />;
}
