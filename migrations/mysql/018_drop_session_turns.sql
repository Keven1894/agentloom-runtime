-- Retire `session_turns`. Eighteen days of production use, zero rows.
--
-- The table was created in 004 to hold short per-turn summaries alongside the
-- checkpoint. Nothing ever wrote one. The only path to it was
-- `agentloom-session turn`, which required a hand-written summary at the exact
-- moment somebody is trying to stop working, and the archive already answers
-- the question it was meant to answer -- at full fidelity, redacted, indexed
-- and searchable, rather than as a lossy line somebody typed from memory.
--
-- Two records for one fact is a maintenance cost with no reader. Removing the
-- one that was never populated is the cheaper direction.
--
-- Destructive and deliberate: this drops a table. It is safe here only because
-- the table is empty in every known deployment; an adopter who did wire
-- `add_turn` to something should not apply it. There is no down migration,
-- because 004 can recreate the table verbatim if one is ever needed.
--
-- Ordering requirement: apply this only once every host runs code without
-- `add_turn` and without the `turns` field in the resume pack. A host on older
-- code reaching for a dropped table fails its whole resume, not just the turn
-- lookup -- which is the one command it runs before doing anything else.

SET NAMES utf8mb4;

DROP TABLE IF EXISTS `session_turns`;
