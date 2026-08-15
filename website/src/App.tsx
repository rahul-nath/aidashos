import { GITHUB_URL } from "./content";
import { PromptSequence } from "./components/PromptSequence";
import { Terminal } from "./components/Terminal";
import { track } from "./telemetry";

const LANE = [
  {
    step: "1",
    title: "Reach",
    body: "This page, one clone command, one repo. Nothing to sign up for.",
  },
  {
    step: "2",
    title: "Base install",
    body: "make runs bootstrap: uv, Python 3.13, Node, Docker, Postgres, schemas.",
  },
  {
    step: "3",
    title: "Boot",
    body: "scripts/boot fetches llama.cpp and the models, signs your subscriptions in, sets the default stack.",
  },
  {
    step: "4",
    title: "Run",
    body: "start-agent-runtime brings up Postgres, the llama router, whisper, and the resident pi daemon, supervised.",
  },
  {
    step: "5",
    title: "Drive",
    body: "pi commands from the terminal, or your own AI tool attached over MCP.",
  },
];

const FEATURES = [
  {
    title: "A document, not a prompt",
    body: "Work starts from a design document with milestones, dependencies, and acceptance. It compiles into an immutable, hashed plan before any agent moves.",
  },
  {
    title: "A tiered bench",
    body: "A local junior model makes the cheap judgment calls, a frontier senior writes code in an isolated worktree, and a different vendor's staff model reviews the diff.",
  },
  {
    title: "Durable by construction",
    body: "Every step writes a row to a local Postgres ledger as it happens. Close the laptop mid-run; the next dispatch resumes from what the ledger says.",
  },
  {
    title: "Gates it cannot cross",
    body: "Merge, deploy, spend, and external comms stop at approval requests only you resolve. Staff approval ends at a pending CODE_MERGE, never at a merge.",
  },
  {
    title: "Local models, your GPU",
    body: "llama.cpp serves gemma-4, Qwen3.8-27B, and optionally Muse-Glimmer-30B from your own disk. The control plane runs with the network unplugged.",
  },
  {
    title: "Your AI tool drives it",
    body: "The coordination ledger is an MCP server. Claude Code picks it up from the repo's .mcp.json; Codex and any stdio client attach with one config block.",
  },
];

const STACK = [
  ["junior", "gemma-4-E4B-it, local via llama.cpp", "the model the system will not run without"],
  ["heavy local", "Qwen3.8-27B UD-Q5_K_XL + MTP draft", "default deliberation-class local model"],
  ["optional", "Muse-Glimmer-30B + DFlash draft", "second heavyweight, fetched only on request"],
  ["senior", "Codex CLI under your ChatGPT subscription", "implements, in an isolated worktree"],
  ["staff", "Claude Code under your Claude subscription", "reviews, from a different vendor than senior"],
];

const FAQS = [
  {
    q: "Does it run in the cloud?",
    a: "No. It runs on your machine, on purpose. Postgres runs in local Docker, local models run through llama.cpp on your hardware, and the frontier agents run through their own CLIs under your existing subscriptions. There is no hosted service and nothing reports home.",
  },
  {
    q: "What happens if I close the laptop mid-run?",
    a: "Work state lives in Postgres rows, not in context windows. A crashed agent process loses only its own context window; the crash reconciler reaps its dead lease and the next dispatch resumes from what the ledger says.",
  },
  {
    q: "Can it merge or deploy on its own?",
    a: "No. Merge, deploy, spend, and external communications all stop at ledger approval gates that only the operator resolves.",
  },
  {
    q: "What do I need to run it?",
    a: "macOS or Linux (Windows via WSL2), Git, Python 3.13 via uv, Node, Docker, and one local model you can serve. Budget tens of GB of disk for the model weights; the boot check measures the exact figure from the pinned files. One local model with every tier staffed locally also works; the frontier seats are your own subscriptions.",
  },
  {
    q: "Is there telemetry?",
    a: "The OS reports nothing to anyone; its observability stack points at your own machine. This website counts only its own button clicks, and only when analytics are enabled.",
  },
];

export function App() {
  return (
    <>
      <header className="site-header">
        <span className="wordmark">
          aidash<span className="wordmark-accent">os</span>
        </span>
        <nav aria-label="Site">
          <a href="#install">Install</a>
          <a href="#how">How it works</a>
          <a href="#faq">FAQ</a>
          <a
            className="github-link"
            href={GITHUB_URL}
            rel="noopener"
            onClick={() => track("click_github", { where: "header" })}
          >
            GitHub
          </a>
        </nav>
      </header>

      <main>
        <section className="hero">
          <p className="kicker">A local-first agent OS</p>
          <h1>Agents that keep working when nobody is watching the screen.</h1>
          <p className="lede">
            aidashos turns a design document into governed agent work on your own machine:
            compiled plans, isolated worktrees, your project's own test commands, cross-vendor
            review, and an approval gate before anything merges. Every step lands in a local
            Postgres ledger, so closing the laptop never loses the system's state.
          </p>
          <ul className="badges" aria-label="Properties">
            <li>Local-first</li>
            <li>Durable</li>
            <li>No cloud backend</li>
            <li>Nothing reports home</li>
            <li>AGPL-3.0</li>
          </ul>
        </section>

        <section id="install" className="install">
          <h2>Install: one lane, end to end</h2>
          <Terminal />
          <h3 className="prompts-heading">Or hand it to your agent</h3>
          <p className="prompts-lede">
            Three prompts, in order, for any AI tool with shell access (Claude Code, Codex, or
            anything else). They drive the same scripts, stage by stage, and leave the sign-ins
            and big-download confirmations to you.
          </p>
          <PromptSequence />
        </section>

        <section id="how" className="lane">
          <h2>The lane you just copied</h2>
          <ol className="lane-steps">
            {LANE.map((item) => (
              <li key={item.step}>
                <span className="lane-step-number">{item.step}</span>
                <h3>{item.title}</h3>
                <p>{item.body}</p>
              </li>
            ))}
          </ol>
          <p className="lane-note">
            The whole path is a checked-in DAG:{" "}
            <a href={`${GITHUB_URL}/blob/main/docs/diagrams/aidashos-onboarding-dag.png`} rel="noopener">
              docs/diagrams/aidashos-onboarding-dag.png
            </a>
            , with the boot stages numbered in topological order.
          </p>
        </section>

        <section className="features">
          <h2>What you get</h2>
          <div className="feature-grid">
            {FEATURES.map((feature) => (
              <article key={feature.title}>
                <h3>{feature.title}</h3>
                <p>{feature.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="stack">
          <h2>The default stack</h2>
          <p>
            Opinionated defaults, swappable in one TOML line each. A seat names a role, not a
            model; <code>configs/staffing.toml</code> decides who sits where.
          </p>
          <table>
            <thead>
              <tr>
                <th scope="col">Seat</th>
                <th scope="col">Default</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {STACK.map(([seat, what, why]) => (
                <tr key={seat}>
                  <th scope="row">{seat}</th>
                  <td>{what}</td>
                  <td>{why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section id="faq" className="faq">
          <h2>Questions with real answers</h2>
          {FAQS.map((faq) => (
            <details key={faq.q}>
              <summary>{faq.q}</summary>
              <p>{faq.a}</p>
            </details>
          ))}
        </section>

        <section className="closing">
          <h2>Start now</h2>
          <p>
            The repo is the product. Clone it, run <code>make</code>, and boot.
          </p>
          <a
            className="cta"
            href={GITHUB_URL}
            rel="noopener"
            onClick={() => track("click_github", { where: "closing" })}
          >
            View on GitHub
          </a>
        </section>
      </main>

      <footer className="site-footer">
        <p>
          <a href={GITHUB_URL} rel="noopener" onClick={() => track("click_github", { where: "footer" })}>
            github.com/rahul-nath/aidashos
          </a>{" "}
          - AGPL-3.0 - built and operated by one engineer, in the open.
        </p>
        <p className="footer-honesty">
          This page counts its own button clicks and nothing else. The OS it describes reports
          nothing, ever.
        </p>
      </footer>
    </>
  );
}
