-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- Deterministic dispatch identity.
--
-- `submit_dispatch_intent` minted `uuid.uuid4()` per call, so an intent's
-- identity was a function of when it was submitted rather than of what was
-- being asked for. A milestone executor that submitted, waited, and died before
-- its DBOS step checkpointed re-ran the step from the top on recovery and
-- created a second intent for the same milestone attempt: two agents doing the
-- same work at full cost, either of which could land a branch.
--
-- The key is supplied by the caller, because only the caller knows what makes
-- two requests the same request. The uniqueness is enforced here, because a
-- caller that merely checked first would still race a concurrent dispatcher
-- between the check and the insert.

ALTER TABLE dispatch_intents ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- Unique only where present: NULLs do not collide in a unique index, so intents
-- submitted before this column existed, and intents from producers with no
-- natural identity, are both unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_intents_idempotency_key
    ON dispatch_intents(idempotency_key);
