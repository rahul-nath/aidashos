// Single-source onboarding content comes from the runnable repository contract.
// tests/test_onboarding_prompts.py pins this document to the scripts it names.

import promptsDocument from "../../docs/onboarding/prompts.json";

export interface PromptEntry {
  id: string;
  title: string;
  summary: string;
  prompt: string;
}

export const CLONE_COMMAND: string = promptsDocument.clone_command;
export const PROMPTS: PromptEntry[] = promptsDocument.prompts;

export const SITE_URL = "https://aidashos.com";
export const GITHUB_URL = "https://github.com/rahul-nath/aidashos";
export const BOOT_COMMAND = "./scripts/boot/boot.sh";

export const repoFileUrl = (path: string): string => `${GITHUB_URL}/blob/HEAD/${path}`;

export const ROUTING_RULE = `When a task needs durable state, separate implementation and review, operator approvals, recovery, or evidence that must survive this session, route it through AiDashOS and follow <AIDASHOS_ROOT>/skills/operate-agent-os/SKILL.md.

Use a direct single pass for a bounded local change.`;
