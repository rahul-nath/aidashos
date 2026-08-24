// Generate the Open Graph card from the repo's own onboarding JSON.
//
// The card is the first thing a person sees when the link is shared, and it
// renders a command a reader may retype. Before this script existed the PNG was
// hand-made, so it drifted from docs/onboarding/prompts.json silently: it showed
// a clone command with no scheme and no .git suffix, which fails when run.
//
// The SVG is the artifact under version control and the one a test can pin;
// the PNG is a build product of it. Regenerate both after any change here:
//
//   node scripts/og-card.mjs
//
// PNG rendering needs librsvg (`brew install librsvg`). The SVG is written even
// when rsvg-convert is absent, and the script says so rather than failing quietly.

import { execFile } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const websiteRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repoRoot = path.dirname(websiteRoot);
const promptsPath = path.join(repoRoot, "docs", "onboarding", "prompts.json");

// Design tokens, copied from src/styles.css. The card is a static image and
// cannot read CSS custom properties, so these are the one duplication the card
// carries; test_og_card.py pins them back to the stylesheet.
const BG = "#0b0e14";
const BG_INSET = "#0d1119";
const BORDER = "#202939";
const TEXT = "#e7eaf0";
const MUTED = "#9aa4b3";
const ACCENT = "#ff7a1a";
const GREEN = "#46d17c";

const MONO = "Menlo, 'SF Mono', SFMono-Regular, Consolas, monospace";
const SANS = "'Helvetica Neue', Helvetica, Arial, system-ui, sans-serif";

const HEADLINE = "A local-first agent OS.";
const SUBLINE = "Governed coding agents, a durable Postgres ledger, your machine.";

const WIDTH = 1200;
const HEIGHT = 630;
const MARGIN = 80;
const BOX_PADDING = 28;

// Menlo's advance width is 0.6em. The prompt adds two columns to the command.
const MONO_ADVANCE = 0.6;
const MAX_COMMAND_SIZE = 30;
const MIN_COMMAND_SIZE = 15;

const escapeXml = (value) =>
  value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/**
 * Pick the largest font size at which `$ <command>` still fits on one line.
 *
 * A wrapped command is what made the old card wrong to begin with, so the card
 * shrinks its type rather than breaking the line. This is the property that
 * keeps a longer command from silently overflowing the box later.
 */
export function commandFontSize(command, boxInnerWidth = WIDTH - 2 * MARGIN - 2 * BOX_PADDING) {
  const columns = command.length + 2; // the "$ " prompt
  const fitted = Math.floor(boxInnerWidth / (columns * MONO_ADVANCE));
  return Math.max(MIN_COMMAND_SIZE, Math.min(MAX_COMMAND_SIZE, fitted));
}

export function renderCard(cloneCommand) {
  const size = commandFontSize(cloneCommand);
  const boxTop = 444;
  const boxHeight = 110;
  const baseline = boxTop + boxHeight / 2 + size * 0.36;
  const commandX = MARGIN + BOX_PADDING + size * MONO_ADVANCE * 2;

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}" role="img" aria-label="aidashos: ${escapeXml(HEADLINE)}">
  <defs>
    <radialGradient id="glow" cx="0.82" cy="0.12" r="0.62">
      <stop offset="0%" stop-color="${ACCENT}" stop-opacity="0.20" />
      <stop offset="55%" stop-color="${ACCENT}" stop-opacity="0.05" />
      <stop offset="100%" stop-color="${ACCENT}" stop-opacity="0" />
    </radialGradient>
  </defs>

  <rect width="${WIDTH}" height="${HEIGHT}" fill="${BG}" />
  <rect width="${WIDTH}" height="${HEIGHT}" fill="url(#glow)" />

  <text x="${MARGIN}" y="112" font-family="${MONO}" font-size="42" font-weight="700" fill="${TEXT}" letter-spacing="-0.5"
    >aidash<tspan fill="${ACCENT}">os</tspan></text>

  <text x="${MARGIN}" y="286" font-family="${SANS}" font-size="76" font-weight="700" fill="${TEXT}" letter-spacing="-2"
    >${escapeXml(HEADLINE)}</text>

  <text x="${MARGIN}" y="346" font-family="${SANS}" font-size="30" fill="${MUTED}"
    >${escapeXml(SUBLINE)}</text>

  <rect x="${MARGIN}" y="${boxTop}" width="${WIDTH - 2 * MARGIN}" height="${boxHeight}" rx="14"
    fill="${BG_INSET}" stroke="${BORDER}" stroke-width="1.5" />
  <text x="${MARGIN + BOX_PADDING}" y="${baseline}" font-family="${MONO}" font-size="${size}" fill="${GREEN}">$</text>
  <text x="${commandX}" y="${baseline}" font-family="${MONO}" font-size="${size}" fill="${TEXT}"
    >${escapeXml(cloneCommand)}</text>
</svg>
`;
}

export async function cloneCommandFromRepo() {
  const document = JSON.parse(await readFile(promptsPath, "utf8"));
  return document.clone_command;
}

async function main() {
  const cloneCommand = await cloneCommandFromRepo();
  const svg = renderCard(cloneCommand);
  const svgPath = path.join(websiteRoot, "public", "og.svg");
  const pngPath = path.join(websiteRoot, "public", "og.png");

  await writeFile(svgPath, svg, "utf8");
  console.log(`wrote public/og.svg (command at ${commandFontSize(cloneCommand)}px)`);

  try {
    await execFileAsync("rsvg-convert", [
      "--width", String(WIDTH),
      "--height", String(HEIGHT),
      "--format", "png",
      "--output", pngPath,
      svgPath,
    ]);
    console.log("wrote public/og.png");
  } catch (error) {
    if (error.code === "ENOENT") {
      console.error("public/og.png NOT regenerated: rsvg-convert is not installed (brew install librsvg).");
      process.exitCode = 1;
      return;
    }
    throw error;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
