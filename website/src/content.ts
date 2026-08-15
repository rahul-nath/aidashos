// Single-source content: the prompt sequence and clone command come from the
// repo's own docs/onboarding/prompts.json, which tests/test_onboarding_prompts.py
// pins against the scripts it names. The page cannot describe a funnel the
// repo does not ship.

import promptsDocument from "../../docs/onboarding/prompts.json";

export interface PromptEntry {
  id: string;
  title: string;
  summary: string;
  prompt: string;
}

export const CLONE_COMMAND: string = promptsDocument.clone_command;
export const PROMPTS: PromptEntry[] = promptsDocument.prompts;

export const GITHUB_URL = "https://github.com/rahul-nath/aidashos";
export const BOOT_COMMAND = "./scripts/boot/boot.sh";
