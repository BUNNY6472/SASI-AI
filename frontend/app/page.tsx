"use client";

import { useCallback, useEffect, useState } from "react";
import Connections from "@/components/Connections";
import Login from "@/components/Login";
import RunView from "@/components/RunView";
import Scene3D from "@/components/Scene3D";
import Workspace from "@/components/Workspace";
import { activeProviders, api, ApiError, ProviderStatus, Run, VertexInfo } from "@/lib/api";

type Route = { view: "home" } | { view: "run"; id: string } | { view: "settings" };

function parseHash(): Route {
  const h = typeof window === "undefined" ? "" : window.location.hash.replace(/^#\/?/, "");
  if (h.startsWith("run/")) return { view: "run", id: h.slice(4) };
  if (h === "settings") return { view: "settings" };
  return { view: "home" };
}

export default function Page() {
  const [route, setRoute] = useState<Route>({ view: "home" });
  const [auth, setAuth] = useState<"unknown" | "in" | "out">("unknown");
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [needsKeys, setNeedsKeys] = useState(false);
  const [vertex, setVertex] = useState<VertexInfo | null>(null);
  const [liveRun, setLiveRun] = useState<Run | null>(null);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    onHash();
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const loadProviders = useCallback(async (refresh = false) => {
    try {
      const p = await api<{ providers: ProviderStatus[]; needs_keys: boolean; vertex?: VertexInfo }>(`/api/providers${refresh ? "?refresh=true" : ""}`);
      setProviders(p.providers);
      setNeedsKeys(p.needs_keys);
      setVertex(p.vertex ?? null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setAuth("out");
    }
  }, []);

  useEffect(() => {
    api<{ authenticated: boolean }>("/api/session")
      .then((s) => setAuth(s.authenticated ? "in" : "out"))
      .catch(() => setAuth("out"));
  }, []);

  useEffect(() => {
    if (auth === "in") loadProviders();
  }, [auth, loadProviders]);

  const go = (hash: string) => {
    window.location.hash = hash;
  };

  return (
    <>
      <div className="backdrop" />
      <Scene3D active={activeProviders(liveRun)} hero={route.view === "home"} team={liveRun?.team} />
      {auth === "out" && <Login onDone={() => setAuth("in")} />}
      {auth === "in" && (
        <div className="shell">
          <header className="topbar">
            <button className="brand" onClick={() => go("/")}>
              <img src="/logo.svg" alt="" width={34} height={34} />
              <span>SASI</span>
            </button>
            <nav>
              <button className={route.view !== "settings" ? "nav on" : "nav"} onClick={() => go("/")}>Workspace</button>
              <button className={route.view === "settings" ? "nav on" : "nav"} onClick={() => go("/settings")}>Connections</button>
              {needsKeys && route.view !== "settings" && <button className="badge warnbadge hide-sm" onClick={() => go("/settings")}>Add API keys</button>}
            </nav>
          </header>
          <main>
            {route.view === "home" && <Workspace open={(id) => go(`/run/${id}`)} needsKeys={needsKeys} providers={providers} />}
            {route.view === "run" && <RunView key={route.id} id={route.id} onRun={setLiveRun} back={() => go("/")} />}
            {route.view === "settings" && <Connections providers={providers} vertex={vertex} refresh={() => loadProviders(true)} reload={() => loadProviders(false)} />}
          </main>
          <footer className="foot">Results are reviewed by independent models, not guaranteed correct. Check anything important.</footer>
        </div>
      )}
    </>
  );
}
