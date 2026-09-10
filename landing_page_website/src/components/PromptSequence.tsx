import { PROMPTS } from "../content";
import { CopyButton } from "./CopyButton";

// Both the homepage and quickstart render this canonical prompt sequence.
// Copy controls use the same source text as the expandable preview.
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
