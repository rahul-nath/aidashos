import { useCallback, useEffect, useRef, useState } from "react";

import { track } from "../telemetry";

interface CopyButtonProps {
  text: string;
  event: string;
  label?: string;
}

export function CopyButton({ text, event, label = "Copy" }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard API needs a secure context; fall back to a transient textarea.
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "absolute";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }
    track(event);
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1800);
  }, [text, event]);

  return (
    <button type="button" className={copied ? "copy copied" : "copy"} onClick={onCopy}>
      {copied ? "Copied" : label}
    </button>
  );
}
