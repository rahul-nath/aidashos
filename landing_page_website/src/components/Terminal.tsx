import { BOOT_COMMAND, CLONE_COMMAND } from "../content";
import { CopyButton } from "./CopyButton";

export function Terminal() {
  return (
    <figure className="terminal" aria-label="The two commands that install aidashos">
      <figcaption className="terminal-bar">
        <span className="dot red" />
        <span className="dot yellow" />
        <span className="dot green" />
        <span className="terminal-title">install aidashos</span>
      </figcaption>
      <div className="terminal-body">
        <div className="terminal-line">
          <code>
            <span className="prompt-char">$ </span>
            {CLONE_COMMAND}
          </code>
          <CopyButton text={CLONE_COMMAND} event="copy_clone" />
        </div>
        <div className="terminal-line">
          <code>
            <span className="prompt-char">$ </span>
            {BOOT_COMMAND}
          </code>
          <CopyButton text={BOOT_COMMAND} event="copy_boot" />
        </div>
        <p className="terminal-note">
          <code>make</code> prepares the toolchain and local ledger.
          The boot lane then walks through llama.cpp, model weights, interactive subscription
          sign-ins, configuration, and a final readiness check.
        </p>
      </div>
    </figure>
  );
}
