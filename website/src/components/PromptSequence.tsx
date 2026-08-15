import { PROMPTS } from "../content";
import { CopyButton } from "./CopyButton";

// The prompt cards sit directly under the terminal on purpose: the clone
// command is the human's step, and these are the agent's. Each copy button is
// a funnel event, which is the only measurement this page takes.
export function PromptSequence() {
  return (
    <div className="prompt-sequence">
      {PROMPTS.map((entry, index) => (
        <article className="prompt-card" key={entry.id}>
          <header className="prompt-head">
            <span className="prompt-index">{index + 1}</span>
            <div>
              <h3>{entry.title}</h3>
              <p className="prompt-summary">{entry.summary}</p>
            </div>
            <CopyButton text={entry.prompt} event={`copy_prompt_${entry.id}`} label="Copy prompt" />
          </header>
          <details>
            <summary>Read the prompt</summary>
            <pre className="prompt-text">{entry.prompt}</pre>
          </details>
        </article>
      ))}
    </div>
  );
}
