"""Weak labeling to expand the golden set (Day 2).

1. Answer-key mining: every historical message a setter replied to with the
   booking/reschedule template is a free positive label (the deck's
   "labelling shortcut").
2. LLM-judge the remainder; human spot-checks 10%.

Report accuracy on the hand-labeled slice only — that's the honest number.
"""


def mine_answer_key(sent_messages: list[dict]) -> list[dict]:
    """Historical setter replies -> (message, intent) labels."""
    raise NotImplementedError("Day 2")


def judge_batch(messages: list[dict]) -> list[dict]:
    """LLM-judge labels for the unlabeled remainder."""
    raise NotImplementedError("Day 2")
