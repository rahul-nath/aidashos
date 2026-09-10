import { CopyButton } from "./components/CopyButton";
import { PromptSequence } from "./components/PromptSequence";
import { Terminal } from "./components/Terminal";
import {
  GITHUB_URL,
  ROUTING_RULE,
  SITE_URL,
  repoFileUrl,
} from "./content";
import { track } from "./telemetry";

export type PagePath = "/" | "/quickstart/" | "/docs/" | "/about/";

export interface PageMetadata {
  path: PagePath;
  title: string;
  description: string;
}

export const STATIC_ROUTES: readonly PageMetadata[] = [
  {
    path: "/",
    title: "Make Plans, Not Prompts - aidashos",
    description:
      "Give your coding agent a local path from design document to independently reviewed, approval-gated change.",
  },
  {
    path: "/quickstart/",
    title: "Quickstart - aidashos",
    description:
      "Install aidashos, boot its local runtime, and attach Claude Code, Codex, or another MCP client.",
  },
  {
    path: "/docs/",
    title: "Docs - aidashos",
    description:
      "Start, operate, and understand aidashos through its focused guides and architecture references.",
  },
  {
    path: "/about/",
    title: "About Rahul Nath - aidashos",
    description:
      "Why Rahul Nath is building aidashos: local models, durable human supervision, and useful personal interaction history.",
  },
] as const;

export function normalizePagePath(pathname: string): PagePath {
  const barePath = pathname.split(/[?#]/, 1)[0] || "/";
  if (barePath === "/quickstart" || barePath === "/quickstart/") {
    return "/quickstart/";
  }
  if (barePath === "/docs" || barePath === "/docs/") {
    return "/docs/";
  }
  if (barePath === "/about" || barePath === "/about/") {
    return "/about/";
  }
  return "/";
}

export function metadataForPath(pathname: string): PageMetadata {
  const path = normalizePagePath(pathname);
  const metadata = STATIC_ROUTES.find((candidate) => candidate.path === path);
  if (!metadata) {
    throw new Error(`No metadata for static path ${path}`);
  }
  return metadata;
}

const VALUE_PROOFS = [
  {
    title: "Your AI drives it",
    body: "Claude Code discovers the local ledger from the repository. Codex and other stdio MCP clients attach to that same record.",
  },
  {
    title: "Use your subscriptions",
    body: "Codex and Claude Code run through their installed CLIs. The default setup needs no per-token model API integration. Provider limits still apply.",
  },
  {
    title: "Give local models real work",
    body: "A local junior model handles its assigned routine judgment. Plans and approvals remain in the local ledger.",
  },
  {
    title: "Resume from evidence",
    body: "Plans, attempts, artifacts, reviews, and approvals live in local Postgres. A lost agent context does not become lost system state.",
  },
] as const;

const PLAN_PATH = [
  ["01", "Compile", "Turn a design document into an immutable, hashed plan."],
  ["02", "Implement", "Run bounded work in an isolated Git worktree."],
  ["03", "Review", "Use a separate review session to inspect the exact diff."],
  ["04", "Approve", "Stop at the operator gate before integration."],
] as const;

const DOC_GROUPS = [
  {
    eyebrow: "Start",
    title: "Get a working system",
    links: [
      ["Hosted quickstart", "/quickstart/", "Two commands, three agent prompts, and MCP attachment."],
      ["Full onboarding", repoFileUrl("docs/onboarding/ONBOARDING.md"), "Every boot stage, requirement, and recovery step."],
    ],
  },
  {
    eyebrow: "Operate",
    title: "Drive work from your agent",
    links: [
      ["Operator skill", repoFileUrl("skills/operate-agent-os/SKILL.md"), "Attach your AI tool and use the governed commands."],
      ["Agent manual", repoFileUrl("docs/AGENT_MANUAL.md"), "Take a project from intent to an approval-gated change."],
      ["WorkUnit walkthrough", repoFileUrl("docs/work_unit_operator_walkthrough.md"), "Inspect a complete durable execution path."],
      ["Cockpit runbook", repoFileUrl("docs/cockpit_e2e_runbook.md"), "Read status, evidence, and approval requests."],
    ],
  },
  {
    eyebrow: "Understand",
    title: "Read the design",
    links: [
      ["Design tradeoffs", repoFileUrl("docs/design_tradeoffs.md"), "What the architecture buys and what it costs."],
      ["Code structure", repoFileUrl("docs/code_structure.md"), "A map of the packages and their ownership boundaries."],
      ["Configuration", repoFileUrl("docs/configuration.md"), "Generated settings reference for the current source."],
      ["Dispatch design", repoFileUrl("docs/decomposition_dispatch.md"), "How work becomes bounded agent tasks."],
    ],
  },
] as const;

function SiteHeader({ path }: { path: PagePath }) {
  const current = (candidate: PagePath) => (candidate === path ? "page" : undefined);
  return (
    <header className="site-header">
      <a className="wordmark wordmark-link" href="/" aria-label="aidashos home">
        aidash<span className="wordmark-accent">os</span>
      </a>
      <nav aria-label="Site">
        <a href="/about/" aria-current={current("/about/")}>About</a>
        <a href="/#how">How it works</a>
        <a href="/#faq">FAQ</a>
        <a href="/docs/" aria-current={current("/docs/")}>Docs</a>
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
  );
}

function RoutingRule() {
  return (
    <div className="routing-rule">
      <div className="routing-rule-head">
        <p className="routing-rule-label">Paste into AGENTS.md, CLAUDE.md, or your equivalent project instructions</p>
        <CopyButton text={ROUTING_RULE} event="copy_routing_rule" label="Copy rule" />
      </div>
      <pre>{ROUTING_RULE}</pre>
    </div>
  );
}

function HomePage() {
  return (
    <>
      <section className="hero">
        <p className="kicker">A local-first agent OS</p>
        <h1>Make Plans, Not Prompts</h1>
        <p className="lede">
          aidashos turns a design document into governed agent work on your own machine:
          compiled plans, isolated worktrees, your project's own test commands, cross-vendor
          review, and an approval gate before anything merges. Closing your laptop never
          loses aidashos system state.
        </p>
        <ul className="badges" aria-label="Properties">
          <li>Local-first</li>
          <li>Durable</li>
          <li>No cloud backend</li>
          <li>AGPL-3.0</li>
        </ul>
        <div className="hero-actions">
          <a
            className="cta"
            href={GITHUB_URL}
            rel="noopener"
            onClick={() => track("click_github", { where: "hero" })}
          >
            View Source
          </a>
        </div>
        <p className="release-note">
          Public developer preview. No signup and no hosted account. The repository is the product.
        </p>
      </section>

      <section className="agent-install" aria-labelledby="agent-install-title">
        {/*
          <h2>Install: one lane, end to end</h2>
          <Terminal />
        */}
        <h2 id="agent-install-title">Hand your local agent these prompts to install aidashos</h2>
        <p className="section-lede">
          Three prompts, in order, for any AI tool with shell access (Claude Code, Codex, or
          anything else). They drive the same scripts and leave the sign-ins and big-download
          confirmations to you.
        </p>
        <PromptSequence />
      </section>

      <section className="proofs" aria-labelledby="proofs-title">
        <p className="kicker">One governed path</p>
        <h2 id="proofs-title">Give the agent the task. Let the system remember the work.</h2>
        <div className="feature-grid proof-grid">
          {VALUE_PROOFS.map((proof) => (
            <article key={proof.title}>
              <h3>{proof.title}</h3>
              <p>{proof.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="how" className="plan-path" aria-labelledby="plan-path-title">
        <p className="kicker">Design to decision</p>
        <h2 id="plan-path-title">Four steps, one durable record</h2>
        <ol className="lane-steps plan-steps">
          {PLAN_PATH.map(([step, title, body]) => (
            <li key={step}>
              <span className="lane-step-number">{step}</span>
              <h3>{title}</h3>
              <p>{body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="routing" aria-labelledby="routing-title">
        <p className="kicker">Beside your current cockpit</p>
        <h2 id="routing-title">Teach your coding agent when to hand work over</h2>
        <p className="section-lede">
          Your interactive agent follows the operator skill. Workers dispatched by AiDashOS
          follow a smaller internal execution skill.
        </p>
        <RoutingRule />
        <p className="inline-links">
          <a href={repoFileUrl("skills/operate-agent-os/SKILL.md")} rel="noopener">Operator skill</a>
          <span aria-hidden="true">·</span>
          <a href={repoFileUrl("skills/agent-startup/SKILL.md")} rel="noopener">Dispatched-worker skill</a>
        </p>
      </section>

      <section id="faq" className="faq" aria-labelledby="faq-title">
        <h2 id="faq-title">Questions with real answers</h2>
        <details>
          <summary>Does it run in the cloud?</summary>
          <p>
            The control plane runs on your machine with a local Postgres ledger.
            Local models run on your hardware. Configured frontier agents connect to
            their providers through your installed CLIs and accounts.
          </p>
        </details>
        <details>
          <summary>What happens if I close the laptop?</summary>
          <p>
            Plans, attempts, artifacts, reviews, and approvals are stored in Postgres.
            A sleeping laptop pauses local computation. When you return, the system can
            use its retained state to recover work; an interrupted agent may need a new attempt.
          </p>
        </details>
        <details>
          <summary>What do I need to run it?</summary>
          <p>
            macOS is the supported platform today. The <a href="/quickstart/">quickstart</a>
            {" "}covers the local runtime, model setup, and attaching your AI tool.
            Large downloads and interactive sign-ins stay under your control.
          </p>
        </details>
        <details>
          <summary>Is aidashos finished?</summary>
          <p>
            No. This is a public developer preview, and bugs and incomplete features remain.
            Your coding agent can help inspect failures and debug setup or milestones.
            Read the <a href={`${GITHUB_URL}/issues`}>open issues</a> or
            {" "}<a href="/about/">get involved</a>.
          </p>
        </details>
      </section>

      <section className="closing compact-closing">
        <h2>Give the next hard task a plan.</h2>
        <a className="cta" href={GITHUB_URL}>View Source</a>
      </section>
    </>
  );
}

function QuickstartPage() {
  return (
    <>
      <section className="page-intro">
        <p className="kicker">Quickstart</p>
        <h1>Set up aidashos on your Mac.</h1>
        <p className="lede">
          Both paths run the same checked-in scripts. Large model downloads and interactive
          subscription sign-ins stay under your control.
        </p>
      </section>

      <section className="install" aria-labelledby="install-title">
        <h2 id="install-title">Two commands</h2>
        <Terminal />
      </section>

      <section className="agent-install" aria-labelledby="agent-install-title">
        <h2 id="agent-install-title">Hand your local agent these prompts to install aidashos</h2>
        <p className="section-lede">
          Copy these prompts in order. They inspect first, ask before large downloads, and
          hand interactive sign-ins back to you.
        </p>
        <PromptSequence />
      </section>

      <section className="attach" aria-labelledby="attach-title">
        <p className="kicker">Attach your cockpit</p>
        <h2 id="attach-title">Use the AI tool you already reach for</h2>
        <div className="feature-grid attach-grid">
          <article>
            <h3>Claude Code</h3>
            <p>Open it at the repository root. The checked-in <code>.mcp.json</code> offers the local <code>agent-os</code> server.</p>
          </article>
          <article>
            <h3>Codex or another MCP client</h3>
            <p>Add the stdio configuration from the operator skill, using the absolute path to your AiDashOS checkout.</p>
          </article>
        </div>
        <RoutingRule />
        <p className="inline-links">
          <a href={repoFileUrl("skills/operate-agent-os/SKILL.md")} rel="noopener">Read the operator skill</a>
          <span aria-hidden="true">·</span>
          <a href={repoFileUrl("docs/onboarding/ONBOARDING.md")} rel="noopener">Read every setup stage</a>
        </p>
      </section>

      <section className="preview-boundary" aria-labelledby="preview-title">
        <h2 id="preview-title">Developer preview boundaries</h2>
        <ul className="plain-list">
          <li>macOS is supported. Linux is expected to work but is not exercised on a schedule. Windows is not supported.</li>
          <li>The default frontier path uses logged-in Codex and Claude Code CLIs. Their subscription limits still apply.</li>
          <li>A local model is required for the junior tier. Automatic all-local senior and staff fallback is not shipped yet.</li>
          <li>Durable state survives a stopped process. An agent can still lose its uncommitted context and need a new attempt.</li>
        </ul>
      </section>
    </>
  );
}

function DocsPage() {
  return (
    <>
      <section className="page-intro">
        <p className="kicker">Docs</p>
        <h1>Read only as deep as the task requires.</h1>
        <p className="lede">
          Start with the quickstart. Open the operating and architecture references when you
          need their exact contracts.
        </p>
      </section>

      <section className="doc-groups" aria-label="Documentation index">
        {DOC_GROUPS.map((group) => (
          <article className="doc-group" key={group.eyebrow}>
            <p className="kicker">{group.eyebrow}</p>
            <h2>{group.title}</h2>
            <ul className="doc-links">
              {group.links.map(([label, href, body]) => (
                <li key={label}>
                  <a href={href} rel={href.startsWith("http") ? "noopener" : undefined}>{label}</a>
                  <p>{body}</p>
                </li>
              ))}
            </ul>
          </article>
        ))}
      </section>

      <section className="closing compact-closing">
        <h2>Ready to get started?</h2>
        <a className="cta" href="/quickstart/">Open the quickstart</a>
      </section>
    </>
  );
}

function AboutPage() {
  return (
    <>
      <section className="page-intro about-intro">
        <p className="kicker">About</p>
        <img className="about-portrait" src="/rahul-nath-linkedin.jpg" width="80" height="80" alt="Rahul Nath" />
        <h1>I'm Rahul Nath.</h1>
        <p className="lede">
          I worked on identity and authentication infrastructure at Meta and founded a
          profitable livestreaming startup. Now I'm building aidashos around a bet on local intelligence.
        </p>
      </section>

      <section className="about-copy" aria-labelledby="about-bet-title">
        <h2 id="about-bet-title">My bet: local models will dominate.</h2>
        <p>
          I think local models will eventually dominate, even at the level of AGI.
          As more intelligence runs on our own machines, we'll need a durable way for
          humans to interact with it, supervise it, and decide what it can do.
        </p>
        <p>
          That's the direction behind aidashos: plans, decisions, work, and feedback that
          outlive any one agent session. The history of those interactions could become
          useful in many ways to each person running the system, from remembering why a
          decision was made to learning from past work and shaping how their agents help them.
        </p>
        <p>
          There's a lot of work to do on this. If you're interested, DM me on
          {" "}<a href="https://www.instagram.com/rah_juul/" rel="noopener">Instagram (@rah_juul)</a>,
          {" "}<a href="https://twitter.com/rahulkindarules" rel="noopener">Twitter (@rahulkindarules)</a>,
          {" "}or <a href="https://www.linkedin.com/in/rahul-nath-753a3052/" rel="noopener">LinkedIn</a>.
        </p>
        <a className="cta" href={GITHUB_URL}>View Source</a>
      </section>
    </>
  );
}

function SiteFooter() {
  return (
    <footer className="site-footer">
      <p>
        <a href={GITHUB_URL} rel="noopener" onClick={() => track("click_github", { where: "footer" })}>
          github.com/rahul-nath/aidashos
        </a>{" "}
        · AGPL-3.0-or-later
      </p>
      <p className="footer-honesty">
        The control plane has no AiDashOS hosted backend. Frontier CLIs still connect to their
        providers under your accounts.
      </p>
    </footer>
  );
}

export function App({ pathname = "/" }: { pathname?: string }) {
  const path = normalizePagePath(pathname);
  const page = path === "/quickstart/" ? <QuickstartPage /> : path === "/docs/" ? <DocsPage /> : path === "/about/" ? <AboutPage /> : <HomePage />;

  return (
    <>
      <SiteHeader path={path} />
      <main>{page}</main>
      <SiteFooter />
    </>
  );
}

export const canonicalUrlForPath = (pathname: string): string =>
  `${SITE_URL}${metadataForPath(pathname).path}`;
