"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";
import Icon from "./Icon";

export default function Login({ onDone }: { onDone: () => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <section className="login-story">
        <div className="brand">
          <span className="brand-mark">S</span>
          <span>
            SASI<span className="brand-sub">RESEARCH WORKSPACE</span>
          </span>
        </div>
        <span className="eyebrow">
          Sasidhar’s Artificial Super Intelligence
        </span>
        <h1>
          A space for
          <br />
          your most
          <br />
          <span>ambitious ideas.</span>
        </h1>
        <p>
          Explore difficult questions. Bring together independent perspectives.
          Follow the evidence to your next step.
        </p>
      </section>
      <div className="login-panel">
        <form className="glass login-card" onSubmit={submit}>
          <span className="eyebrow">YOUR PRIVATE WORKSPACE</span>
          <h2>Welcome to SASI.</h2>
          <p className="sub">
            Enter your access code to pick up where curiosity left off.
          </p>
          <label className="field">
            <span>Access code</span>
            <input
              type="password"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
              autoComplete="current-password"
              placeholder="Enter access code"
              required
            />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button className="btn primary" disabled={busy || !code}>
            {busy ? "Checking…" : "Open workspace"}{" "}
            <Icon name="arrow" size={17} />
          </button>
          <p className="login-note">
            Use the access code supplied by your workspace owner.
          </p>
        </form>
      </div>
    </div>
  );
}
