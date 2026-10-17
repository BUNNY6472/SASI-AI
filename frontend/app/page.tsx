"use client";

import { useCallback, useEffect, useState } from "react";
import Connections from "@/components/Connections";
import Login from "@/components/Login";
import RunView from "@/components/RunView";
import Workspace from "@/components/Workspace";
import Icon from "@/components/Icon";
import {
  api,
  ApiError,
  ProviderStatus,
  ProvidersResponse,
  RunSummary,
  VertexInfo,
} from "@/lib/api";

type Route =
  | { view: "home" }
  | { view: "run"; id: string }
  | { view: "settings" };
function parseHash(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  if (h.startsWith("run/")) return { view: "run", id: h.slice(4) };
  return h === "settings" ? { view: "settings" } : { view: "home" };
}

export default function Page() {
  const [route, setRoute] = useState<Route>({ view: "home" });
  const [auth, setAuth] = useState<"unknown" | "in" | "out">("unknown");
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [needsKeys, setNeedsKeys] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const [trialCreditsOnly, setTrialCreditsOnly] = useState(false);
  const [trialCreditExpiresAt, setTrialCreditExpiresAt] = useState<
    string | null
  >(null);
  const [trialCreditWindowError, setTrialCreditWindowError] = useState<
    string | null
  >(null);
  const [vertex, setVertex] = useState<VertexInfo | null>(null);
  const [recent, setRecent] = useState<RunSummary[]>([]);
  const [menu, setMenu] = useState(false);
  const [networkError, setNetworkError] = useState("");
  const [workspaceKey, setWorkspaceKey] = useState(0);

  useEffect(() => {
    const onHash = () => {
      setRoute(parseHash());
      setMenu(false);
    };
    onHash();
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const session = useCallback(() => {
    setNetworkError("");
    api<{ authenticated: boolean }>("/api/session")
      .then((s) => setAuth(s.authenticated ? "in" : "out"))
      .catch(() =>
        setNetworkError(
          "We couldn’t connect to your workspace. Please try again.",
        ),
      );
  }, []);
  useEffect(session, [session]);
  useEffect(() => {
    if (!menu) return;
    const close = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenu(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menu]);
  const loadProviders = useCallback(async (refresh = false) => {
    try {
      const p = await api<ProvidersResponse>(
        "/api/providers" + (refresh ? "?refresh=true" : ""),
      );
      setProviders(p.providers);
      setNeedsKeys(p.needs_keys);
      setDemoMode(!!p.demo_mode);
      setTrialCreditsOnly(!!p.trial_credits_only);
      setTrialCreditExpiresAt(p.trial_credit_expires_at ?? null);
      setTrialCreditWindowError(p.trial_credit_window_error ?? null);
      setVertex(p.vertex ?? null);
      setNetworkError("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setAuth("out");
      else
        setNetworkError(
          "Connection status is unavailable. Your saved work is still accessible.",
        );
    }
  }, []);
  useEffect(() => {
    if (auth === "in") loadProviders();
  }, [auth, loadProviders]);
  useEffect(() => {
    if (auth !== "in") return;
    const load = () =>
      api<RunSummary[]>("/api/runs")
        .then(setRecent)
        .catch(() => {});
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [auth, route]);

  const go = (hash: string) => {
    window.location.hash = hash;
    setMenu(false);
  };
  const newRun = () => {
    setWorkspaceKey((k) => k + 1);
    go("/");
  };
  const available = providers.filter(
    (p) => p.connected && p.ok !== false && !p.policy_blocked,
  ).length;
  async function logout() {
    try {
      await api("/api/logout", { method: "POST" });
      setAuth("out");
    } catch {
      setNetworkError("Sign out failed. Please try again.");
    }
  }

  if (auth === "unknown")
    return (
      <div className="app-loading">
        <div className="brand-mark">S</div>
        <p>{networkError || "Opening your workspace…"}</p>
        {networkError && (
          <button className="btn" onClick={session}>
            Try again
          </button>
        )}
      </div>
    );
  if (auth === "out") return <Login onDone={() => setAuth("in")} />;
  return (
    <div className="shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to content
      </a>
      {menu && (
        <button
          className="sidebar-overlay"
          aria-label="Close navigation"
          onClick={() => setMenu(false)}
        />
      )}
      <aside
        className={"sidebar " + (menu ? "is-open" : "")}
        aria-label="Workspace navigation"
      >
        <button className="brand" onClick={newRun}>
          <span className="brand-mark">S</span>
          <span>
            SASI<span className="brand-sub">RESEARCH WORKSPACE</span>
          </span>
        </button>
        <button className="btn new-run" onClick={newRun}>
          <Icon name="plus" /> New research <span>↗</span>
        </button>
        <nav className="main-nav" aria-label="Main navigation">
          <button
            className={"nav " + (route.view === "home" ? "on" : "")}
            onClick={() => go("/")}
          >
            <Icon name="grid" /> Workspace
          </button>
          <button
            className={"nav " + (route.view === "settings" ? "on" : "")}
            onClick={() => go("/settings")}
          >
            <Icon name="settings" /> Connections{" "}
            <span className={"health-dot " + (available ? "healthy" : "")} />
          </button>
        </nav>
        <div className="sidebar-history">
          <p className="nav-caption">RECENT RESEARCH</p>
          {recent.length === 0 ? (
            <p className="sidebar-empty">Your research will appear here.</p>
          ) : (
            recent.slice(0, 7).map((r) => (
              <button
                key={r.id}
                className={
                  "history-link " +
                  (route.view === "run" && route.id === r.id ? "on" : "")
                }
                onClick={() => go("/run/" + r.id)}
                title={r.request}
              >
                <Icon name="file" size={15} />
                <span>{r.request}</span>
              </button>
            ))
          )}
        </div>
        <div className="sidebar-bottom">
          <div className="system-status">
            <span className={"health-dot " + (available ? "healthy" : "")} />
            <span>
              {providers.length
                ? available +
                  (available === 1 ? " model available" : " models available")
                : "Checking connections"}
            </span>
          </div>
          <p>
            Independent perspectives.
            <br />
            Evidence you can inspect.
          </p>
          <button className="nav signout" onClick={logout}>
            <Icon name="logout" size={16} /> Sign out
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu icon-button"
              onClick={() => setMenu(!menu)}
              aria-label="Toggle navigation"
              aria-expanded={menu}
            >
              <Icon name="menu" />
            </button>
            <span>SASI</span>
            <span className="breadcrumb-divider">/</span>
            <b>
              {route.view === "settings"
                ? "Connections"
                : route.view === "run"
                  ? "Research session"
                  : "Workspace"}
            </b>
          </div>
          <div className="topbar-note">
            <span
              className={
                "health-dot " +
                (trialCreditsOnly && trialCreditWindowError ? "" : "healthy")
              }
            />
            {trialCreditsOnly
              ? trialCreditWindowError
                ? "Trial access paused"
                : "Trial credits only"
              : "Your private workspace"}
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          {trialCreditsOnly &&
            trialCreditWindowError &&
            route.view !== "settings" && (
              <div className="notice warn" role="status">
                <div>
                  <strong>Trial access paused</strong>
                  <p>{trialCreditWindowError}</p>
                  <button className="link-btn" onClick={() => go("/settings")}>
                    View connections
                  </button>
                </div>
              </div>
            )}
          {networkError && (
            <div className="notice warn" role="status">
              {networkError}{" "}
              <button className="link-btn" onClick={() => loadProviders()}>
                Retry
              </button>
            </div>
          )}
          {route.view === "home" && (
            <Workspace
              key={workspaceKey}
              open={(id) => go("/run/" + id)}
              needsKeys={needsKeys}
              providers={providers}
              demoMode={demoMode}
            />
          )}
          {route.view === "run" && (
            <RunView
              key={route.id}
              id={route.id}
              onRun={() => {}}
              back={() => go("/")}
            />
          )}
          {route.view === "settings" && (
            <Connections
              providers={providers}
              vertex={vertex}
              trialCreditsOnly={trialCreditsOnly}
              trialCreditExpiresAt={trialCreditExpiresAt}
              trialCreditWindowError={trialCreditWindowError}
              refresh={() => loadProviders(true)}
              reload={() => loadProviders(false)}
            />
          )}
        </main>
        <footer className="foot">
          <span>Sasidhar’s Artificial Super Intelligence</span>
          <span>Model reviews are opinions. Check the evidence.</span>
        </footer>
      </div>
    </div>
  );
}
