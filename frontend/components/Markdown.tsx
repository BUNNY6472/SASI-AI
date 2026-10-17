"use client";

import DOMPurify from "dompurify";
import { marked } from "marked";
import { useMemo } from "react";

/** Model output is untrusted: render Markdown, then sanitize before it reaches the DOM. */
export default function Markdown({ text }: { text: string }) {
  const html = useMemo(() => {
    const raw = marked.parse(text || "", { async: false, gfm: true, breaks: false }) as string;
    return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
  }, [text]);
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}
