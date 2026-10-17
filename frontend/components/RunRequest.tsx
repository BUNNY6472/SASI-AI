import { ReactNode } from "react";
import { researchTitle } from "@/lib/research-title";

export function RunQuestion({ text }: { text: string }) {
  return (
    <div className="run-question">
      <h1>{researchTitle(text)}</h1>
    </div>
  );
}

export function OriginalRequest({
  text,
  action,
}: {
  text: string;
  action?: ReactNode;
}) {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return (
    <details className="run-request">
      <summary>
        <span>Original request</span>
        <span className="request-length">
          {words.toLocaleString()} {words === 1 ? "word" : "words"}
        </span>
      </summary>
      <div className="request-body">
        <div className="request-toolbar">
          <p>Your complete request, preserved as submitted.</p>
          {action}
        </div>
        <div
          className="request-text"
          role="region"
          aria-label="Complete original request"
          tabIndex={0}
        >
          {text}
        </div>
      </div>
    </details>
  );
}
