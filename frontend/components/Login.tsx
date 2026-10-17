"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";

export default function Login({ onDone }: { onDone: () => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ code }) });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="glass login-card" onSubmit={submit}>
        <img src="/logo.svg" alt="" width={72} height={72} className="login-logo" />
        <h1>SASI</h1>
        <p className="sub">Claude, ChatGPT and Gemini, working as one team.</p>
        <label className="field">
          <span>Access code</span>
          <input type="password" value={code} onChange={(e) => setCode(e.target.value)} autoFocus autoComplete="current-password" />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="btn primary" disabled={busy || !code}>
          {busy ? "Checking…" : "Enter"}
        </button>
      </form>
    </div>
  );
}
